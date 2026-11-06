"""
Question bank for exam modules.

For modules assessed by exam, revision is doing questions, not recalling facts. Every tutorial sheet,
past paper and lecturer worked example the app tracks is split into its questions (once per file
version, by Claude), each question becomes a practice card, and each card is linked to the lecture
whose notes explain it. Marking a card Right / Half / Wrong feeds the same tutorial tracker as the
Today tab (engine.mark), so priorities and the Vault see it, and schedules the question to come back.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import engine
import materials as mat
from promptlib import P

BANK_VERSION = "q1"
BANK_KINDS = ("tutorial", "past_paper", "worked")
RECALL_KINDS = ("flashcard", "formula", "question")
# days until a question comes back, by result; correct answers step through the ladder
RETRY_DAYS = {"wrong": 2, "partial": 4, "skipped": 1}
CORRECT_LADDER = [10, 25, 45]



def init(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("pragma table_info(cards)")}
    for col, typ in (("work_id", "text"), ("qnum", "text"), ("week", "integer")):
        if col not in have:
            conn.execute(f"alter table cards add column {col} {typ}")
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(*paths: Path | None) -> str:
    h = hashlib.sha1(BANK_VERSION.encode())
    for p in paths:
        if p and p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()


def solution_for(path: Path, files: list[Path]) -> Path | None:
    """The solutions file that goes with a sheet: same name plus 'solutions' / 'answers'."""
    base = re.sub(r"[\W_]+", "", path.stem.lower())
    for f in files:
        if f == path or not re.search(r"solution|answers|mark ?scheme", f.stem, re.I):
            continue
        fb = re.sub(r"[\W_]+|solutions?|answers|markscheme", "", f.stem.lower())
        if fb and (fb == base or base.startswith(fb) or fb.startswith(base)):
            return f
    return None


ANSWERS_ONLY_RE = re.compile(r"answers?\b(?!.*question)|numerical answers", re.I)


def kind_of(work: dict, path: Path) -> str:
    if re.search(r"example", path.stem, re.I):
        return "worked"
    return "past_paper" if work["work_type"] == "past_paper" else "tutorial"


def answers_only(path: Path) -> bool:
    """'Numerical Past Paper Answers': answers without the questions, not something to attempt."""
    return bool(ANSWERS_ONLY_RE.search(path.stem.replace("_", " "))) and not re.search(r"tutorial|sheet|example", path.stem, re.I)


def duplicate_scan(path: Path, files: list[Path]) -> bool:
    """'Example 01_Scan' when 'Example 01_Digital' exists: same example, keep the typed copy."""
    if not re.search(r"scan", path.stem, re.I):
        return False
    base = re.sub(r"[\W_]*scan[\W_]*", "", path.stem, flags=re.I).lower()
    return any(f != path and re.search(r"digital", f.stem, re.I)
               and re.sub(r"[\W_]*digital[\W_]*", "", f.stem, flags=re.I).lower() == base for f in files)


def _text(path: Path, limit: int = 60000) -> str:
    pages = mat.extract_pages(path)
    unit = mat.unit_name(path)
    out = "\n\n".join(f"[{unit}.{i}] {t.strip()}" for i, t in enumerate(pages, 1) if t.strip())
    return out[:limit]


def extract(path: Path, solution: Path | None, code: str, kind: str, client, model: str) -> list[dict]:
    """Questions on a sheet, cached per file contents (sheet + solutions)."""
    cache = mat.CACHE_DIR / f"{_sha(path, solution)}.{BANK_VERSION}.questions.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    text = _text(path)
    if len(text.strip()) < 80:
        return []
    sol_block, sol_note = "", ""
    if solution:
        sol_block = f"\n=== Solutions: {solution.name} ===\n{_text(solution)}\n"
        sol_note = f", with its solutions \"{solution.name}\""
    prompt = P.EXTRACT_PROMPT.format(kind=kind.replace("_", " "), code=code, name=path.name, text=text,
                                   sol_note=sol_note, sol_block=sol_block)
    with client.messages.stream(model=model, max_tokens=32000, tools=[P.EXTRACT_TOOL], tool_choice={"type": "auto"},
                                messages=[{"role": "user", "content": prompt}]) as st:
        resp = st.get_final_message()
    qs = []
    for b in resp.content:
        if b.type == "tool_use" and b.name == "file_questions":
            raw = b.input.get("questions") or []
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    raw = []
            for q in raw:
                if isinstance(q, dict) and str(q.get("text", "")).strip():
                    qs.append({k: str(q.get(k) or "").strip() for k in ("number", "text", "topic", "final_answer", "method")})
    mat.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(qs), encoding="utf-8")
    return qs


# ---------------------------------------------------------------- linking questions to lectures

def _guidance(md: str) -> dict[str, str]:
    """'### Tutorial 2 Q3' -> its guidance text, from a lecture's 'Tutorial sheet guidance' section."""
    m = re.search(r"^## Tutorial sheet guidance\s*$(.*?)(?=^## |\Z)", md, re.M | re.S)
    if not m:
        return {}
    out = {}
    for part in re.split(r"^### ", m.group(1), flags=re.M)[1:]:
        head, _, body = part.partition("\n")
        out[head.strip()] = body.strip()
    return out


def lecture_index(lectures: list[dict]) -> list[dict]:
    """lectures: [{delivery_id, title, date, notes_path}] for one module."""
    out = []
    for l in lectures:
        p = l.get("notes_path")
        if not p or not Path(p).exists():
            continue
        md = Path(p).read_text(encoding="utf-8")
        if md.startswith("---"):
            md = md.split("\n---", 1)[-1]
        out.append({**l, "guidance": _guidance(md), "tokens": mat.tokens(f"{l.get('title', '')} {md[:12000]}")})
    return out


def _sheet_tag(path: Path) -> tuple[str, str | None]:
    """('tutorial', '2') for 'ME32029_Tutorial 2.pdf'; used to match guidance headings."""
    stem = path.stem.lower().replace("_", " ")
    m = re.search(r"(tutorial|sheet|example|paper|revision|mechanical ?testing|plasticity)[^\d]{0,12}(\d{1,2})?", stem)
    return (m.group(1), m.group(2)) if m else (re.sub(r"me\d{5}", "", stem).strip().split(" ")[0], None)


def link(question: dict, path: Path, index: list[dict]) -> tuple[dict | None, str]:
    """Best lecture for a question and that lecture's guidance for it (may be empty)."""
    if not index:
        return None, ""
    word, num = _sheet_tag(path)
    qn = re.sub(r"\D", "", question.get("number") or "")
    for l in index:  # 1. the notes explicitly give guidance for this question
        for head, body in l["guidance"].items():
            h = head.lower()
            if qn and re.search(rf"\bq(?:uestion)?\s*0?{qn}\b", h) and word.split()[0] in h and (not num or re.search(rf"\b0?{num}\b", h)):
                return l, body
    docs = [l["tokens"] for l in index]  # 2. closest lecture by content
    qt = mat.tokens(f"{question.get('topic', '')} {question.get('topic', '')} {question.get('text', '')}")
    tf = mat.Tfidf(docs + [qt])
    qv = tf.vec(qt)

    def score(l):
        session = re.search(r"\b(tutorial|revision|q ?& ?a|quiz|summary)\b", l.get("title") or "", re.I)
        return mat.cosine(tf.vec(l["tokens"]), qv) * (0.55 if session else 1.0)

    best = max(index, key=score)
    return best, ""


# ---------------------------------------------------------------- cards

def _card_id(work_id: str, qnum: str) -> str:
    return "q" + hashlib.sha1(f"{work_id}|{qnum}".encode()).hexdigest()[:19]


def _front(work: dict, q: dict) -> str:
    wk = f" · week {work['week']}" if work.get("week") else ""
    label = "Worked example" if work["_kind"] == "worked" else "Q" + (q.get("number") or "?")
    return f"**{work['title']} · {label}**{wk}  \n*{q.get('topic') or ''}*\n\n{q['text']}"


def _back(q: dict, lecture: dict | None, guidance: str) -> str:
    parts = []
    if q.get("final_answer"):
        parts.append(f"**Final answer:** {q['final_answer']}")
    if q.get("method"):
        parts.append(f"**Method (from the solutions)**\n\n{q['method']}")
    if not q.get("final_answer") and not q.get("method"):
        parts.append("*No official solution posted yet. Check your working against the lecture method below, then ask in the tutorial.*")
    if lecture:
        head = f"**From the lecture:** {lecture.get('title')} ({lecture.get('date')})"
        parts.append(head + (f"\n\n{guidance}" if guidance else ""))
    return "\n\n".join(parts)


def sync_bank(conn: sqlite3.Connection, code: str, lectures: list[dict], client=None, model: str | None = None,
              log=print) -> int:
    """Create or refresh the practice cards for one exam module. Returns questions in the bank."""
    init(conn)
    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            return 0
        import anthropic
        client = anthropic.Anthropic()
    model = model or os.getenv("NOTES_MODEL", "claude-sonnet-5-5")
    files = mat.scan_module(code)["questions"]
    index = lecture_index(lectures)
    total = 0
    today = date.today().isoformat()
    for w in engine.work_list(conn, code):
        if w["work_type"] not in ("tutorial", "past_paper"):
            continue
        try:
            path = mat.resolve(w["file"])
        except Exception:
            continue
        if not path.exists() or duplicate_scan(path, files) or answers_only(path):
            conn.execute("delete from cards where work_id=?", (w["id"],))
            continue
        kind = kind_of(w, path)
        try:
            qs = extract(path, solution_for(path, files), code, kind, client, model)
        except Exception as e:
            log(f"qbank: {path.name}: {e}")
            continue
        if not qs:
            continue
        nums = []
        for i, q in enumerate(qs, 1):
            qnum = re.sub(r"[^\w]", "", q.get("number") or "") or str(i)
            if qnum in nums:
                qnum = f"{qnum}_{i}"
            nums.append(qnum)
            lec, guidance = link(q, path, index)
            front, back = _front({**w, "_kind": kind}, q), _back(q, lec, guidance)
            cid = _card_id(w["id"], qnum)
            unlock = max(today, w.get("set_date") or today) if kind == "tutorial" else today
            if conn.execute("select 1 from cards where id=?", (cid,)).fetchone():
                conn.execute("update cards set front=?, back=?, delivery_id=?, lecture_title=?, lecture_date=?, kind=?, "
                             "week=?, updated_at=? where id=?",
                             (front, back, lec and lec["delivery_id"], lec and lec.get("title"), lec and lec.get("date"),
                              kind, w.get("week"), _now(), cid))
            else:
                conn.execute("insert into cards (id, delivery_id, module_code, lecture_title, lecture_date, kind, front, back,"
                             " due, created_at, updated_at, work_id, qnum, week) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (cid, lec and lec["delivery_id"], code, lec and lec.get("title"), lec and lec.get("date"), kind,
                              front, back, unlock, _now(), _now(), w["id"], qnum, w.get("week")))
        # the tracker's question grid follows the real question numbers
        conn.execute("update work set total_questions=? where id=?", (len(nums), w["id"]))
        total += len(nums)
    # fact-recall flashcards are off for exam modules: the questions replace them
    conn.execute(f"update cards set suspended=1 where module_code=? and kind in ({','.join('?' * len(RECALL_KINDS))})",
                 (code, *RECALL_KINDS))
    conn.commit()
    return total


def mark_card(conn: sqlite3.Connection, card_id: str, result: str, tag: str | None = None, note: str | None = None) -> dict:
    """Right / Half / Wrong / Skip on a practice card: logs it on the tracker and schedules the retry."""
    r = conn.execute("select work_id, qnum, reps, lapses from cards where id=?", (card_id,)).fetchone()
    if not r or not r[0]:
        raise KeyError(card_id)
    work_id, qnum, reps, lapses = r
    tracked = engine.mark(conn, work_id, qnum, result, tag, note)
    if result == "correct":
        days = CORRECT_LADDER[min(reps, len(CORRECT_LADDER) - 1)]
        reps += 1
    else:
        days = RETRY_DAYS.get(result, 2)
        reps = 0
        lapses += 1 if result == "wrong" else 0
    due = (date.today() + timedelta(days=days)).isoformat()
    g = {"correct": 3, "partial": 2, "wrong": 1, "skipped": 1}[result]
    conn.execute("update cards set reps=?, lapses=?, last_grade=?, interval_days=?, due=?, updated_at=? where id=?",
                 (reps, lapses, g, days, due, _now(), card_id))
    conn.commit()
    return {"due": due, "interval": days, **tracked}
