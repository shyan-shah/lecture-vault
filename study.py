"""
Study tools on top of the lecture notes:

  * Spaced repetition: every lecture's flashcards and review questions become cards
    scheduled with a light SM-2 (Again / Hard / Good / Easy).
  * Anki export: one CSV per module (front, back, tags) for anyone who prefers Anki.
  * Weekly plan: per module, Claude turns this week's lectures, self-study topics,
    tutorial sheets, coursework brief, deadlines and weak cards into a concrete plan.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import materials as mat
from promptlib import P

SCHEMA = """
create table if not exists cards (
  id text primary key, delivery_id text, module_code text, lecture_title text, lecture_date text,
  kind text, front text, back text,
  due text, interval_days real default 0, ease real default 2.5, reps integer default 0, lapses integer default 0,
  last_grade integer, created_at text, updated_at text, suspended integer default 0
);
create index if not exists cards_due on cards (module_code, due);
create table if not exists plans (
  module_code text, week_start text, markdown text, created_at text, primary key (module_code, week_start)
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _card_id(delivery_id: str, front: str) -> str:
    return hashlib.sha1(f"{delivery_id}|{front.strip().lower()}".encode()).hexdigest()[:20]


RECALL_OFF: set[str] = set()  # exam modules: practice questions (qbank) replace fact-recall flashcards


def add_cards(conn: sqlite3.Connection, delivery_id: str, code: str | None, title: str, lecture_date: str,
              notes: dict) -> int:
    """Upsert cards for a lecture; keeps scheduling for cards that already exist."""
    if code in RECALL_OFF:
        return 0
    cards = [("flashcard", c.get("front", ""), c.get("back", "")) for c in notes.get("flashcards") or []]
    cards += [("question", q.get("question", ""), q.get("answer", "")) for q in notes.get("review_questions") or []]
    cards += [("formula", f"When do you use: ${f.get('latex', '')}$ ?", f.get("when_to_use", ""))
              for f in notes.get("formula_sheet") or [] if f.get("latex")]
    added = 0
    today = date.today().isoformat()
    for kind, front, back in cards:
        if not front.strip() or not back.strip():
            continue
        cid = _card_id(delivery_id, front)
        exists = conn.execute("select 1 from cards where id=?", (cid,)).fetchone()
        if exists:
            conn.execute("update cards set back=?, kind=?, lecture_title=?, updated_at=? where id=?",
                         (back, kind, title, _now(), cid))
        else:
            conn.execute("insert into cards (id, delivery_id, module_code, lecture_title, lecture_date, kind, front, back,"
                         " due, created_at, updated_at) values (?,?,?,?,?,?,?,?,?,?,?)",
                         (cid, delivery_id, code, title, lecture_date, kind, front, back, today, _now(), _now()))
            added += 1
    conn.commit()
    return added


def grade(conn: sqlite3.Connection, card_id: str, g: int) -> dict:
    """g: 1 again, 2 hard, 3 good, 4 easy."""
    r = conn.execute("select interval_days, ease, reps, lapses from cards where id=?", (card_id,)).fetchone()
    if not r:
        raise KeyError(card_id)
    interval, ease, reps, lapses = r
    if g == 1:
        lapses += 1
        reps = 0
        interval = 0
        ease = max(1.3, ease - 0.2)
    else:
        reps += 1
        if reps == 1:
            interval = 1 if g < 4 else 3
        elif reps == 2:
            interval = 3 if g == 2 else (6 if g == 3 else 8)
        else:
            interval = interval * (1.2 if g == 2 else ease * (1.3 if g == 4 else 1.0))
        ease = max(1.3, ease + {2: -0.15, 3: 0.0, 4: 0.15}[g])
    due = date.today() + timedelta(days=round(interval))
    conn.execute("update cards set interval_days=?, ease=?, reps=?, lapses=?, last_grade=?, due=?, updated_at=? where id=?",
                 (interval, ease, reps, lapses, g, due.isoformat(), _now(), card_id))
    conn.commit()
    return {"due": due.isoformat(), "interval": round(interval, 1)}


def due_cards(conn: sqlite3.Connection, module: str | None = None, limit: int = 60) -> list[dict]:
    q = "select id, module_code, lecture_title, lecture_date, kind, front, back, reps, lapses, delivery_id, " \
        "work_id, qnum, week, last_grade from cards where suspended=0 and due<=?"
    args: list = [date.today().isoformat()]
    if module:
        q += " and module_code=?"
        args.append(module)
    # retries of missed questions first, then the oldest week's questions, then the rest
    q += " order by (work_id is null), (lapses=0), coalesce(week, 99), due, lapses desc limit ?"
    args.append(limit)
    cols = ["id", "module_code", "lecture_title", "lecture_date", "kind", "front", "back", "reps", "lapses",
            "delivery_id", "work_id", "qnum", "week", "last_grade"]
    return [dict(zip(cols, r)) for r in conn.execute(q, args)]


def stats(conn: sqlite3.Connection) -> dict:
    today = date.today().isoformat()
    out = {}
    for code, total, due, new, weak in conn.execute(
            "select module_code, count(*), sum(due<=?), sum(reps=0), sum(lapses>=2) "
            "from cards where suspended=0 group by module_code", (today,)):
        out[code or "unassigned"] = {"total": total, "due": due or 0, "new": new or 0, "weak": weak or 0}
    return out


def weak_cards(conn: sqlite3.Connection, module: str, limit: int = 15) -> list[str]:
    return [r[0] for r in conn.execute(
        "select front from cards where module_code=? and (lapses>=1 or last_grade<=2) order by lapses desc limit ?",
        (module, limit))]


def anki_csv(conn: sqlite3.Connection, module: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for front, back, title, kind in conn.execute(
            "select front, back, lecture_title, kind from cards where module_code=? order by lecture_date, created_at",
            (module,)):
        tag = re.sub(r"[^A-Za-z0-9]+", "_", title or "lecture").strip("_")[:60]
        w.writerow([front, back, f"{module} {kind} {tag}"])
    return buf.getvalue()


# ---------------------------------------------------------------- weekly plan

def plan_system() -> str:
    return P.PLAN_SYSTEM.replace("__STUDENT__", __import__("os").getenv("STUDENT_PROFILE", "").strip() or "university engineering student")


def build_plan(code: str, module_name: str, lectures: list[dict], upcoming: list[dict], card_stats: dict,
               weak: list[str], model: str, assessment: dict | None = None) -> str:
    import anthropic

    lec_lines = []
    for l in lectures:
        lec_lines.append(f"- {l.get('date')} {l.get('title')}: {l.get('summary', '')[:400]}")
        for k in ("self_study", "gaps", "tutorials", "exam_signals"):
            if l.get(k):
                lec_lines.append(f"    {k.replace('_', ' ')}: {'; '.join(map(str, l[k]))[:500]}")
    profile = mat.module_profile(code)
    cw = mat.coursework_context(code, " ".join(l.get("summary", "") for l in lectures),
                                limit_chars=14000 if (assessment or {}).get("coursework") else 4000)
    tut_files = [Path(f).name for f, m in (mat.moodle_meta(code) or {}).items() if m.get("kind") == "questions"]
    tut_files += [f.name for f in mat.scan_module(code)["questions"] if f.name not in tut_files]
    prompt = P.plan_request(code, module_name, assessment, profile, lec_lines, upcoming, tut_files, cw, card_stats, weak)
    client = anthropic.Anthropic()
    with client.messages.stream(model=model, max_tokens=6000, system=plan_system(),
                                messages=[{"role": "user", "content": prompt}]) as st:
        msg = st.get_final_message()
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def week_start(d: date | None = None) -> str:
    d = d or date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


def save_plan(conn: sqlite3.Connection, code: str, markdown: str, notes_dir: Path) -> Path:
    ws = week_start()
    conn.execute("insert into plans (module_code, week_start, markdown, created_at) values (?,?,?,?) "
                 "on conflict(module_code, week_start) do update set markdown=excluded.markdown, created_at=excluded.created_at",
                 (code, ws, markdown, _now()))
    conn.commit()
    folder = notes_dir / code / "_plans"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Week of {ws}.md"
    path.write_text(f"# {code}: plan for week of {ws}\n\n{markdown}\n", encoding="utf-8")
    return path


def get_plan(conn: sqlite3.Connection, code: str) -> dict | None:
    r = conn.execute("select week_start, markdown, created_at from plans where module_code=? order by week_start desc limit 1",
                     (code,)).fetchone()
    return {"week_start": r[0], "markdown": r[1], "created_at": r[2]} if r else None


def boost_weak(conn: sqlite3.Connection, module: str, keywords: list[str], max_cards: int = 20) -> int:
    """After wrong tutorial answers, bring matching cards forward so the gap is drilled."""
    if not keywords:
        return 0
    n = 0
    today = date.today().isoformat()
    for kw in keywords:
        kw = kw.strip().lower()
        if len(kw) < 4:
            continue
        for (cid,) in conn.execute("select id from cards where module_code=? and due>? and (lower(front) like ? or lower(back) like ?) limit ?",
                                   (module, today, f"%{kw}%", f"%{kw}%", max_cards)):
            conn.execute("update cards set due=?, updated_at=? where id=?", (today, _now(), cid))
            n += 1
    conn.commit()
    return n
