"""
Academic engine: links Lecture Vault into the scoring and progress tables in The Vault.

  academic_work        one row per tutorial sheet / coursework (auto-created from Moodle),
                       with per-question marks, error tags, scores and milestones
  academic_priority    weekly priority score per module (which module needs hours)
  academic_notes       understanding_rating, notes_status, questions_for_tutorial filled in
  memory_goals         two standing goals: coursework firsts and exam readiness
  memory_entities      weekly progress log

Scoring model (per module):
  mastery   = weighted mix of tutorial marks (recent weighted higher), card retention and
              coursework milestone progress
  error_density = wrong + skipped questions per attempted, last 3 sheets
  priority  = how much this module needs attention this week, 0..100
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import materials as mat

TUTORIAL_RE = re.compile(r"tutorial|problem sheet|problem-sheet|example sheet|exercise|worksheet|questions", re.I)
SOLUTION_RE = re.compile(r"solution|answers|mark ?scheme", re.I)
QUESTION_RE = re.compile(r"(?:^|\n|\s)(?:Q(?:uestion)?\s*)?(\d{1,2})[\.\)]\s", re.I)
ERROR_TAGS = ["concept", "method", "algebra", "units", "sign", "assumption", "misread", "ran out of time", "not attempted"]

SCHEMA = """
create table if not exists work (
  id text primary key, vault_id text, module_code text, work_type text, title text, file text,
  week integer, total_questions integer, status text default 'not_started',
  set_date text, due_date text, completed_date text,
  question_marks text, errors text, score_pct real, time_minutes integer,
  milestones text, feedback_score real, feedback_notes text, difficulty integer,
  created_at text, updated_at text
);
create table if not exists priority (
  module_code text, week_start text, score real, mastery real, error_density real, coverage real,
  retention real, deadline_days integer, hours real, rank integer, reasoning text, created_at text,
  primary key (module_code, week_start)
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _j(v):
    if v is None:
        return None
    try:
        return json.loads(v) if isinstance(v, str) else v
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- work discovery

def _iso(v) -> str | None:
    if not v:
        return None
    t = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return m.group(0)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(20\d{2})", t)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\s+(20\d{2})", t)
    if m:
        try:
            mon = datetime.strptime(m.group(2)[:3], "%b").month
            return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
        except ValueError:
            return None
    return None


def count_questions(path: Path) -> int | None:
    try:
        text = "\n".join(mat.extract_pages(path))
    except Exception:
        return None
    nums = sorted({int(m.group(1)) for m in QUESTION_RE.finditer(text)})
    if not nums:
        return None
    # longest run starting from 1
    n = 0
    for k in nums:
        if k == n + 1:
            n = k
        else:
            break
    return n or None


def discover_work(conn: sqlite3.Connection, code: str, assessment: dict, semester_start: str | None) -> int:
    """Create work rows for each tutorial sheet and coursework brief found in the materials folders."""
    meta = mat.moodle_meta(code)
    scan = mat.scan_module(code)
    created = 0
    seen = set()

    def upsert(file: Path, work_type: str, title: str, week: int | None, total: int | None, due: str | None):
        nonlocal created
        rel = mat.rel(file)
        seen.add(rel)
        row = conn.execute("select id from work where module_code=? and file=?", (code, rel)).fetchone()
        if row:
            conn.execute("update work set title=?, work_type=?, week=coalesce(?, week), total_questions=coalesce(total_questions, ?), "
                         "due_date=coalesce(due_date, ?), updated_at=? where id=?",
                         (title, work_type, week, total, due, _now(), row[0]))
            return
        wid = f"{code}:{abs(hash(rel)) % 10**10}"
        set_date = None
        if week and semester_start:
            set_date = (date.fromisoformat(semester_start) + timedelta(weeks=week - 1)).isoformat()
            if not due and work_type == "tutorial":
                due = (date.fromisoformat(set_date) + timedelta(days=6)).isoformat()
        conn.execute("insert into work (id, module_code, work_type, title, file, week, total_questions, status, set_date, due_date,"
                     " question_marks, errors, milestones, created_at, updated_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (wid, code, work_type, title, rel, week, total, "not_started", set_date, due, "{}", "[]", "[]", _now(), _now()))
        created += 1

    if assessment.get("weekly_tutorials") or scan["questions"]:
        for f in scan["questions"]:
            if SOLUTION_RE.search(f.stem) and not re.search(r"exam|paper", f.stem, re.I):
                continue
            fm = meta.get(mat.rel(f)) or {}
            kind = "past_paper" if re.search(r"\bexams?\b|paper|mock", f.stem.replace("_", " "), re.I) else "tutorial"
            title = re.sub(r"[_\-]+", " ", f.stem).strip()
            upsert(f, kind, title, fm.get("week"), count_questions(f) or 5, None)
    if assessment.get("coursework"):
        for f in scan["info"]:
            fm = meta.get(mat.rel(f)) or {}
            label = f"{f.stem} {fm.get('topic') or ''} {fm.get('moodle') or ''} {fm.get('section') or ''}"
            if (mat.COURSEWORK_RE.search(label) and not mat.EXEMPLAR_RE.search(f.stem) and f.suffix.lower() != ".md"
                    and not re.search(r"handbook|template|style|reading|formula|timetable|study plan|rubric|criteria|group business report|team ?\d", f.stem, re.I)):
                due = None
                try:
                    txt = " ".join(mat.extract_pages(f))[:20000]
                    m = re.search(r"(?:due|deadline|submit(?:ted)? by|submission)[^.\n]{0,60}?(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+20\d{2}|\d{1,2}/\d{1,2}/20\d{2})", txt, re.I)
                    if m:
                        due = m.group(1)
                except Exception:
                    pass
                upsert(f, "coursework", re.sub(r"[_\-]+", " ", f.stem).strip(), fm.get("week"), None, _iso(due))
    conn.commit()
    return created


# ---------------------------------------------------------------- marking

def mark(conn: sqlite3.Connection, work_id: str, question: str, result: str, error_tag: str | None = None,
         note: str | None = None) -> dict:
    """result: correct | partial | wrong | skipped"""
    row = conn.execute("select question_marks, errors, total_questions, work_type from work where id=?", (work_id,)).fetchone()
    if not row:
        raise KeyError(work_id)
    marks = _j(row[0]) or {}
    errors = _j(row[1]) or []
    marks[str(question)] = {"result": result, "at": _now(), "tag": error_tag, "note": note}
    errors = [e for e in errors if str(e.get("question")) != str(question)]
    if result in ("wrong", "partial", "skipped") and (error_tag or result == "skipped"):
        errors.append({"question": str(question), "tag": error_tag or "not attempted", "note": note, "at": _now()})
    total = row[2] or max(len(marks), 1)
    attempted = sum(1 for m in marks.values() if m["result"] != "skipped")
    pts = sum({"correct": 1.0, "partial": 0.5}.get(m["result"], 0.0) for m in marks.values())
    score = round(100 * pts / total, 1)
    status = "completed" if len(marks) >= total else ("in_progress" if marks else "not_started")
    completed = date.today().isoformat() if status == "completed" else None
    conn.execute("update work set question_marks=?, errors=?, score_pct=?, status=?, completed_date=coalesce(completed_date, ?), updated_at=? where id=?",
                 (json.dumps(marks), json.dumps(errors), score, status, completed, _now(), work_id))
    conn.commit()
    return {"score_pct": score, "status": status, "attempted": attempted, "total": total}


def set_milestone(conn: sqlite3.Connection, work_id: str, name: str, done: bool, pct: int | None = None) -> list:
    row = conn.execute("select milestones from work where id=?", (work_id,)).fetchone()
    ms = _j(row[0]) or []
    hit = next((m for m in ms if m["name"] == name), None)
    if hit:
        hit.update({"done": done, "pct": pct if pct is not None else (100 if done else hit.get("pct", 0)), "at": _now()})
    else:
        ms.append({"name": name, "done": done, "pct": pct if pct is not None else (100 if done else 0), "at": _now()})
    prog = round(sum(m.get("pct", 100 if m.get("done") else 0) for m in ms) / max(len(ms), 1))
    status = "completed" if ms and all(m.get("done") for m in ms) else ("in_progress" if any(m.get("pct") for m in ms) else "not_started")
    conn.execute("update work set milestones=?, score_pct=?, status=?, updated_at=? where id=?",
                 (json.dumps(ms), prog, status, _now(), work_id))
    conn.commit()
    return ms


def default_milestones(brief_text: str) -> list[dict]:
    """Seed coursework milestones from section headings in the brief, else a standard report arc."""
    heads = re.findall(r"\n\s*(?:\d+(?:\.\d+)?\s+)?([A-Z][A-Za-z &/]{4,50})\s*\n", brief_text)
    heads = [h.strip() for h in heads if not re.search(r"page|university|module|assessment criteria|appendix", h, re.I)]
    names = heads[:8] if len(heads) >= 3 else ["Understand brief and criteria", "Research and evidence", "Outline",
                                                 "First draft", "Analysis and figures", "Redraft against criteria", "Proofread and submit"]
    return [{"name": n, "done": False, "pct": 0} for n in names]


def work_list(conn: sqlite3.Connection, module: str | None = None) -> list[dict]:
    q = "select id, vault_id, module_code, work_type, title, file, week, total_questions, status, set_date, due_date, " \
        "completed_date, question_marks, errors, score_pct, time_minutes, milestones, feedback_score, difficulty from work"
    args = []
    if module:
        q += " where module_code=?"
        args.append(module)
    q += " order by module_code, week, title"
    cols = ["id", "vault_id", "module_code", "work_type", "title", "file", "week", "total_questions", "status", "set_date",
            "due_date", "completed_date", "question_marks", "errors", "score_pct", "time_minutes", "milestones",
            "feedback_score", "difficulty"]
    out = []
    for r in conn.execute(q, args):
        d = dict(zip(cols, r))
        for k in ("question_marks", "errors", "milestones"):
            d[k] = _j(d[k]) or ({} if k == "question_marks" else [])
        out.append(d)
    return out


# ---------------------------------------------------------------- scoring

def module_metrics(conn: sqlite3.Connection, code: str, assessment: dict, lectures: list[dict], next_deadline_days: int | None) -> dict:
    works = [w for w in work_list(conn, code)]
    tut = [w for w in works if w["work_type"] in ("tutorial", "past_paper")]
    cw = [w for w in works if w["work_type"] == "coursework"]
    # tutorial mastery: recency-weighted mean score of completed/in-progress sheets
    scored = [w for w in tut if w["question_marks"]]
    scored.sort(key=lambda w: w.get("completed_date") or w.get("updated_at") or "")
    if scored:
        ws = [0.6 ** (len(scored) - 1 - i) for i in range(len(scored))]
        tut_mastery = sum(w["score_pct"] * x for w, x in zip(scored, ws)) / sum(ws)
    else:
        tut_mastery = None
    errs = []
    att = 0
    for w in scored[-3:]:
        errs += [m for m in w["question_marks"].values() if m["result"] in ("wrong", "skipped", "partial")]
        att += len(w["question_marks"])
    error_density = round(len(errs) / att, 2) if att else None
    sheet_completion = (sum(1 for w in tut if w["status"] == "completed") / len(tut)) if tut else None
    # retention from cards
    r = conn.execute("select count(*), sum(case when lapses>=1 or last_grade<=2 then 1 else 0 end), sum(case when reps>0 then 1 else 0 end) "
                     "from cards where module_code=?", (code,)).fetchone()
    total_cards, weak, seen = r[0] or 0, r[1] or 0, r[2] or 0
    retention = round(100 * (1 - weak / seen), 1) if seen else None
    # coursework progress
    cw_prog = (sum(w["score_pct"] or 0 for w in cw) / len(cw)) if cw else None
    # notes coverage: lectures noted / lectures delivered
    delivered = [l for l in lectures if l.get("delivered")]
    noted = [l for l in delivered if l.get("noted")]
    coverage = round(100 * len(noted) / len(delivered), 1) if delivered else None
    # mastery blend by assessment
    parts = []
    if assessment.get("exam"):
        if tut_mastery is not None:
            parts.append((tut_mastery, 0.5))
        if retention is not None:
            parts.append((retention, 0.3))
        if coverage is not None:
            parts.append((coverage, 0.2))
    if assessment.get("coursework"):
        if cw_prog is not None:
            parts.append((cw_prog, 0.6 if not assessment.get("exam") else 0.3))
        if coverage is not None:
            parts.append((coverage, 0.4 if not assessment.get("exam") else 0.1))
    evidence = any(x is not None for x in (tut_mastery, retention, cw_prog))
    mastery = round(sum(v * w for v, w in parts) / sum(w for _, w in parts), 1) if parts else None
    if mastery is not None and not evidence:
        mastery = min(mastery, 40.0)  # notes alone are not mastery
    return {"tutorial_mastery": tut_mastery, "error_density": error_density, "sheet_completion": sheet_completion,
            "retention": retention, "coursework_progress": cw_prog, "coverage": coverage, "mastery": mastery,
            "cards_total": total_cards, "cards_weak": weak, "deadline_days": next_deadline_days,
            "open_sheets": sum(1 for w in tut if w["status"] != "completed"),
            "weak_questions": [{"work": w["title"], "q": e["question"], "tag": e["tag"]} for w in scored[-3:] for e in w["errors"]][:12]}


def priority_score(m: dict, assessment: dict) -> tuple[float, str]:
    """0..100: higher = needs more hours this week."""
    s, why = 20.0, []
    if m["mastery"] is not None:
        gap = max(0.0, 75 - m["mastery"])  # distance from first-class territory
        s += gap * 0.8
        why.append(f"mastery {m['mastery']:.0f}%")
    else:
        s += 20
        why.append("no evidence yet")
    if m["error_density"] is not None and m["error_density"] > 0.25:
        s += 15
        why.append(f"error density {m['error_density']:.0%}")
    if m["open_sheets"]:
        s += min(15, 5 * m["open_sheets"])
        why.append(f"{m['open_sheets']} sheets open")
    if m["coverage"] is not None and m["coverage"] < 100:
        s += (100 - m["coverage"]) * 0.15
        why.append(f"notes coverage {m['coverage']:.0f}%")
    d = m.get("deadline_days")
    if d is not None and d >= 0:
        if d <= 7:
            s += 30
        elif d <= 14:
            s += 18
        elif d <= 28:
            s += 8
        why.append(f"deadline in {d}d")
    if assessment.get("coursework") and not assessment.get("exam") and (m["coursework_progress"] or 0) < 30:
        s += 10
        why.append("coursework barely started")
    return round(min(100.0, s), 1), "; ".join(why)


def compute_priorities(conn: sqlite3.Connection, modules: dict, lectures_by_module: dict, assessments: dict,
                       deadlines: dict, weekly_hours: float = 20.0) -> list[dict]:
    rows = []
    for code in modules:
        m = module_metrics(conn, code, assessments.get(code, {}), lectures_by_module.get(code, []), deadlines.get(code))
        score, why = priority_score(m, assessments.get(code, {}))
        rows.append({"module_code": code, "score": score, "reasoning": why, **m})
    total = sum(r["score"] for r in rows) or 1
    rows.sort(key=lambda r: -r["score"])
    ws = week_start()
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["hours"] = round(weekly_hours * r["score"] / total, 1)
        conn.execute("insert into priority (module_code, week_start, score, mastery, error_density, coverage, retention, deadline_days, hours, rank, reasoning, created_at) "
                     "values (?,?,?,?,?,?,?,?,?,?,?,?) on conflict(module_code, week_start) do update set score=excluded.score, mastery=excluded.mastery, "
                     "error_density=excluded.error_density, coverage=excluded.coverage, retention=excluded.retention, deadline_days=excluded.deadline_days, "
                     "hours=excluded.hours, rank=excluded.rank, reasoning=excluded.reasoning, created_at=excluded.created_at",
                     (r["module_code"], ws, r["score"], r["mastery"], r["error_density"], r["coverage"], r["retention"],
                      r["deadline_days"], r["hours"], i, why if False else r["reasoning"], _now()))
    conn.commit()
    return rows


def week_start(d: date | None = None) -> str:
    d = d or date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


def teaching_week(semester_start: str | None, d: date | None = None) -> int:
    if not semester_start:
        return 0
    delta = ((d or date.today()) - date.fromisoformat(semester_start)).days
    return delta // 7 + 1 if delta >= 0 else 0


# ---------------------------------------------------------------- next action

def next_actions(conn: sqlite3.Connection, priorities: list[dict], due_cards: int, plans: dict, assessments: dict) -> list[dict]:
    """A short ordered list of what to do now, lowest friction first."""
    acts = []
    today = date.today()
    for w in work_list(conn):
        if w["status"] == "completed":
            continue
        due = w.get("due_date")
        days = None
        if due:
            try:
                days = (date.fromisoformat(due[:10]) - today).days
            except ValueError:
                pass
        if w["work_type"] in ("tutorial", "past_paper"):
            done = len(w["question_marks"])
            tot = w["total_questions"] or "?"
            acts.append({"kind": "tutorial", "module": w["module_code"], "work_id": w["id"],
                         "label": f"{w['title']}: {done}/{tot} marked", "days": days,
                         "urgency": (3 if days is not None and days <= 2 else 2 if days is not None and days <= 7 else 1)})
        else:
            prog = w["score_pct"] or 0
            acts.append({"kind": "coursework", "module": w["module_code"], "work_id": w["id"],
                         "label": f"{w['title']}: {prog:.0f}% through milestones", "days": days,
                         "urgency": (3 if days is not None and days <= 7 else 2 if days is not None and days <= 21 else 1 if prog < 50 else 0)})
    if due_cards:
        acts.append({"kind": "review", "module": None, "label": f"{due_cards} flashcards due", "days": 0, "urgency": 2 if due_cards > 20 else 1})
    top = priorities[0] if priorities else None
    if top and top["module_code"] not in plans:
        acts.append({"kind": "plan", "module": top["module_code"], "label": f"Build this week's plan for {top['module_code']}", "urgency": 1})
    acts.sort(key=lambda a: (-a["urgency"], a.get("days") if a.get("days") is not None else 99))
    return acts[:6]
