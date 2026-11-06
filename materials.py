"""
Lecture materials: find the slides or notes that belong to a lecture.

Folder layout (created automatically for every module in config.json):

  materials/
    ME32029/                      per-lecture files (slides, handouts)
      L01 Introduction.pdf
      L02 Transient conduction.pptx
      pack/                       one big notes pack covering the whole module
        Heat Transfer Notes.pdf
      questions/                  past papers, problem sheets, example questions
        2024 exam.pdf
      info/                       handbook, coursework briefs, Moodle structure (never matched)

Rules:
  * Files directly in the module folder (or any subfolder except pack/ and questions/) are per-lecture files.
  * Files inside pack/ are module packs; the matching page range is found for each lecture.
  * Files inside questions/ are never matched as lecture materials. They are linked to
    lectures they relate to and uploaded so the module chats can use them.
  * Auto: a module folder holding a single file of 40+ pages and nothing else is treated as a pack.
  * config.json can force it per module with "materials_mode": "pack" or "per_lecture".
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import date
from pathlib import Path

from promptlib import P

ROOT = Path(__file__).resolve().parent
MATERIALS_DIR = Path(os.getenv("MATERIALS_DIR", ROOT / "materials"))
CACHE_DIR = MATERIALS_DIR / ".cache"
SUPPORTED = {".pdf", ".pptx", ".docx", ".txt", ".md"}
PACK_DIRS = {"pack", "_pack", "packs", "notes pack"}
QUESTION_DIRS = {"questions", "question", "past papers", "past-papers", "past_papers", "exam questions", "examples",
                 "problem sheets", "problem-sheets", "tutorial sheets", "tutorials", "exams"}
INFO_DIRS = {"info", "admin", "coursework", "handbook"}
UPLOAD_CHUNK_CHARS = 6000
MANIFEST = ".moodle.json"
AUTO_PACK_MIN_PAGES = 40
CHUNK_WORDS = 350
MAX_SECTION_PAGES = 40
MAX_MATERIAL_CHARS = 120_000

STOP = set("""
the and for that this with you are have from was were what when which will would there their they them then than
into about just like your can could should our out not but all any been being also its lets okay right now here
one two three get got going see look need want well yeah very much more some these those where how why who over
under only each other such use used using make made does did done take thing things actually basically gonna kind
sort mean know think say said today lecture slide slides week module notes page figure example
""".split())

NUM_RE = re.compile(r"(?<![a-z])(?:lecture|lect|lec|week|wk|session|l|w)[\s_\-.]*0*(\d{1,2})(?!\d)", re.I)


# ---------------------------------------------------------------- folders

def module_dir(code: str) -> Path:
    if MATERIALS_DIR.exists():
        for d in MATERIALS_DIR.iterdir():
            if d.is_dir() and d.name.upper().startswith(code.upper()):
                return d
    return MATERIALS_DIR / code


def ensure_dirs(codes) -> None:
    for code in codes:
        (module_dir(code) / "pack").mkdir(parents=True, exist_ok=True)
        (module_dir(code) / "questions").mkdir(parents=True, exist_ok=True)
        (module_dir(code) / "info").mkdir(parents=True, exist_ok=True)


def rel(path: Path) -> str:
    return path.resolve().relative_to(MATERIALS_DIR.resolve()).as_posix()


def resolve(rel_path: str) -> Path:
    p = (MATERIALS_DIR / rel_path).resolve()
    if MATERIALS_DIR.resolve() not in p.parents:
        raise ValueError("Path outside materials folder")
    return p


# ---------------------------------------------------------------- extraction

def _extract_raw(path: Path) -> list[str]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        return [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
    if ext == ".pptx":
        from pptx import Presentation

        out = []
        for slide in Presentation(str(path)).slides:
            parts = []
            for sh in slide.shapes:
                if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                    parts.append(sh.text_frame.text)
                if getattr(sh, "has_table", False) and sh.has_table:
                    for row in sh.table.rows:
                        parts.append(" | ".join(c.text for c in row.cells))
            if slide.has_notes_slide:
                parts.append(slide.notes_slide.notes_text_frame.text)
            out.append("\n".join(p for p in parts if p.strip()))
        return out
    if ext == ".docx":
        from docx import Document

        text = "\n".join(p.text for p in Document(str(path)).paragraphs)
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    words = text.split()
    return [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)] or [""]


def extract_pages(path: Path) -> list[str]:
    """Text per page (PDF), per slide (PPTX) or per ~350 word chunk (DOCX/TXT). Cached on disk."""
    st = path.stat()
    key = hashlib.sha1(f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
    cache = CACHE_DIR / f"{key}.json"
    if cache.exists():
        pages = json.loads(cache.read_text(encoding="utf-8"))
    else:
        pages = _extract_raw(path)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(pages), encoding="utf-8")
    return _merge_vision(path, pages)


# ---------------------------------------------------------------- image pages (scans, board work)
# Scanned worked examples and image-only slides have no text layer. Claude reads those pages as
# images once (transcribe_images, run by the background worker) and the text is merged in here.

VISION_VERSION = "v1"
VISION_MIN_CHARS = 150          # a page with less extractable text than this is treated as an image
VISION_MAX_PAGES = 90           # skip very long readings
VISION_BATCH = 4
VISION_INFO_RE = re.compile(r"formula|data sheet|data booklet|crib|rubric|brief|coursework|exam", re.I)
VISION_MARK = "[Transcribed from image]"


_SHA: dict = {}
_LEGACY: dict | None = None


def _content_sha(path: Path) -> str:
    st = path.stat()
    k = (str(path.resolve()), st.st_mtime_ns, st.st_size)
    if k not in _SHA:
        _SHA[k] = hashlib.sha1(path.read_bytes()).hexdigest()
    return _SHA[k]


def _vision_cache(path: Path) -> Path:
    """Keyed on file contents: Moodle re-downloads give files a new modified time, which must not
    trigger a second (paid) transcription of identical pages."""
    global _LEGACY
    vc = CACHE_DIR / f"{_content_sha(path)}.{VISION_VERSION}.vision.json"
    if vc.exists():
        return vc
    if _LEGACY is None:  # transcriptions saved under the old mtime-based names
        _LEGACY = {}
        for f in sorted(CACHE_DIR.glob("*.vision.json"), key=lambda x: x.stat().st_mtime):
            if f.name.count(".") == 2:  # <sha>.vision.json (old) vs <sha>.v1.vision.json (new)
                try:
                    _LEGACY[json.loads(f.read_text(encoding="utf-8")).get("file")] = f
                except Exception:
                    pass
    old = _LEGACY.get(rel(path))
    if old and old.exists():
        try:
            vc.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
    return vc


def _merge_vision(path: Path, pages: list[str]) -> list[str]:
    vc = _vision_cache(path)
    if not vc.exists():
        return pages
    try:
        extra = json.loads(vc.read_text(encoding="utf-8")).get("pages", {})
    except Exception:
        return pages
    out = list(pages)
    for k, text in extra.items():
        i = int(k) - 1
        if 0 <= i < len(out) and text.strip():
            out[i] = (out[i].strip() + f"\n\n{VISION_MARK}\n" + text.strip()).strip()
    return out


def has_vision(path: Path) -> bool:
    return _vision_cache(path).exists()


def image_pages(path: Path, kind: str) -> list[int]:
    """1-based page numbers that look like images (scans, handwritten or picture-only slides)."""
    if path.suffix.lower() not in (".pdf", ".pptx"):
        return []
    if kind == "info" and not VISION_INFO_RE.search(path.name):
        return []  # background reading: not worth it
    st = path.stat()
    key = hashlib.sha1(f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
    raw_cache = CACHE_DIR / f"{key}.json"
    pages = json.loads(raw_cache.read_text(encoding="utf-8")) if raw_cache.exists() else extract_pages(path)
    if len(pages) > VISION_MAX_PAGES:
        return []
    return [i for i, t in enumerate(pages, 1) if len(re.sub(r"\s+", " ", t).strip()) < VISION_MIN_CHARS]


def _page_images(path: Path, numbers: list[int]) -> dict[int, list[tuple[str, bytes]]]:
    """PNG/JPEG bytes for each requested page: rendered for PDFs, embedded pictures for PPTX."""
    import io
    out: dict[int, list[tuple[str, bytes]]] = {}
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(path))
        for n in numbers:
            page = doc[n - 1]
            w, h = page.get_size()
            scale = min(2.2, 1600 / max(w, h, 1))
            img = page.render(scale=scale).to_pil().convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82)
            out[n] = [("image/jpeg", buf.getvalue())]
        doc.close()
        return out
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    slides = list(Presentation(str(path)).slides)
    for n in numbers:
        imgs = []
        for sh in slides[n - 1].shapes:
            if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                ct = sh.image.content_type
                if ct in ("image/png", "image/jpeg") and len(sh.image.blob) > 15_000:
                    imgs.append((ct, sh.image.blob))
        if imgs:
            out[n] = imgs[:4]
    return out




def _vision_batch(client, model: str, code: str, path: Path, imgs: dict[int, list[tuple[str, bytes]]]) -> dict[str, str]:
    import base64
    content = []
    for n, items in imgs.items():
        content.append({"type": "text", "text": f"Page {n}:"})
        for ct, data in items:
            content.append({"type": "image", "source": {"type": "base64", "media_type": ct,
                                                        "data": base64.b64encode(data).decode()}})
    content.append({"type": "text", "text": P.VISION_PROMPT.format(module=code, name=path.name)})
    msg = client.messages.create(model=model, max_tokens=12000, messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    parts = re.split(r"<<<PAGE\s*(\d+)>>>", text)
    found = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    if not found and len(imgs) == 1:
        found = {str(next(iter(imgs))): text.strip()}
    return found


def transcribe_images(codes, log=print, budget_pages: int = 400) -> int:
    """Read image-only pages with Claude once per file version. Returns pages transcribed."""
    if not os.getenv("ANTHROPIC_API_KEY") or os.getenv("VISION", "1") != "1":
        return 0
    import anthropic
    client = anthropic.Anthropic()
    model = os.getenv("VISION_MODEL") or os.getenv("NOTES_MODEL", "claude-sonnet-5-5")
    done = 0
    for code in codes:
        for path, kind in module_files(code):
            if done >= budget_pages:
                return done
            vc = _vision_cache(path)
            if vc.exists():
                continue
            try:
                nums = image_pages(path, kind)
            except Exception as e:
                log(f"vision: skip {path.name}: {e}")
                continue
            if not nums:
                continue
            log(f"vision: {code} {path.name}: {len(nums)} image pages")
            result: dict[str, str] = {}
            try:
                for i in range(0, len(nums), VISION_BATCH):
                    batch = nums[i:i + VISION_BATCH]
                    imgs = _page_images(path, batch)
                    if imgs:
                        result.update(_vision_batch(client, model, code, path, imgs))
                    done += len(batch)
            except Exception as e:
                log(f"vision: failed {path.name}: {e}")
                continue  # retried on the next sync
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            vc.write_text(json.dumps({"file": rel(path), "pages": result, "model": model}), encoding="utf-8")
    return done



def unit_name(path: Path) -> str:
    return {".pdf": "p", ".pptx": "slide"}.get(path.suffix.lower(), "part")


# ---------------------------------------------------------------- scanning

def scan_module(code: str, mode: str | None = None) -> dict:
    d = module_dir(code)
    per, packs, questions, info = [], [], [], []
    if d.exists():
        for f in sorted(d.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in SUPPORTED or f.name.startswith(("~$", ".")):
                continue
            if ".cache" in f.parts:
                continue
            parents = [p.lower() for p in f.relative_to(d).parts[:-1]]
            if any(p in INFO_DIRS for p in parents):
                info.append(f)
            elif any(p in QUESTION_DIRS for p in parents):
                questions.append(f)
            elif any(p in PACK_DIRS for p in parents):
                packs.append(f)
            else:
                per.append(f)
    detected = "per_lecture"
    if mode == "pack":
        packs, per, detected = packs + per, [], "pack (forced)"
    elif mode != "per_lecture" and len(per) == 1 and not packs:
        try:
            if len(extract_pages(per[0])) >= AUTO_PACK_MIN_PAGES:
                packs, per, detected = per, [], "pack (auto: single large file)"
        except Exception:
            pass
    if packs and not per and detected == "per_lecture":
        detected = "pack"
    elif packs and per:
        detected = "mixed"
    return {"dir": d, "per_lecture": per, "packs": packs, "questions": questions, "info": info, "mode": detected}


def describe_module(code: str, mode: str | None = None) -> dict:
    scan = scan_module(code, mode)
    out = {"dir": str(scan["dir"]), "mode": scan["mode"], "per_lecture": [], "packs": [],
           "questions": [{"file": rel(f), "name": f.name} for f in scan["questions"]],
           "info": [{"file": rel(f), "name": f.name} for f in scan["info"]]}
    for f in scan["per_lecture"]:
        out["per_lecture"].append({"file": rel(f), "name": f.name})
    for f in scan["packs"]:
        try:
            n = len(extract_pages(f))
        except Exception:
            n = None
        out["packs"].append({"file": rel(f), "name": f.name, "pages": n, "unit": unit_name(f)})
    return out


# ---------------------------------------------------------------- similarity

def tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", text.lower()) if w not in STOP]


class Tfidf:
    def __init__(self, docs: list[list[str]]):
        self.n = len(docs)
        df = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {t: math.log((1 + self.n) / (1 + c)) + 1 for t, c in df.items()}
        self.default = math.log(1 + self.n) + 1

    def vec(self, toks: list[str]) -> dict:
        counts = Counter(toks)
        v = {t: (1 + math.log(c)) * self.idf.get(t, self.default) for t, c in counts.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}


def cosine(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def lecture_number(text: str) -> int | None:
    m = NUM_RE.search(text or "")
    return int(m.group(1)) if m else None


def date_in_name(name: str, lecture_date: str | None) -> bool:
    if not lecture_date:
        return False
    d = date.fromisoformat(lecture_date)
    m = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) == d
        except ValueError:
            return False
    m = re.search(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})(?![-_.]?\d{3})", name)
    if m:
        return int(m.group(1)) == d.day and int(m.group(2)) == d.month
    return False


def find_section(pages: list[str], lec_toks: list[str]) -> tuple[tuple[int, int] | None, float]:
    """Best contiguous page range of a pack for this lecture (1-based, inclusive)."""
    docs = [tokens(p) for p in pages]
    if not docs or not lec_toks:
        return None, 0.0
    tf = Tfidf(docs + [lec_toks])
    lv = tf.vec(lec_toks)
    sims = [cosine(tf.vec(d), lv) if d else 0.0 for d in docs]
    mean = sum(sims) / len(sims)
    std = math.sqrt(sum((s - mean) ** 2 for s in sims) / len(sims)) or 1e-6
    thr = mean + 0.5 * std
    best_sum, best, cur, start = 0.0, None, 0.0, 0
    for i, s in enumerate(sims):
        x = s - thr
        if cur <= 0:
            cur, start = x, i
        else:
            cur += x
        if cur > best_sum:
            best_sum, best = cur, (start, i)
    if not best:
        return None, 0.0
    a, b = best
    if b - a + 1 > MAX_SECTION_PAGES:
        window = MAX_SECTION_PAGES
        sums = [sum(sims[i:i + window]) for i in range(a, b - window + 2)]
        a = a + max(range(len(sums)), key=sums.__getitem__)
        b = a + window - 1
    a = max(0, a - 1)  # include the heading page before the dense section
    score = sum(sims[a:b + 1]) / (b - a + 1)
    return (a + 1, b + 1), score


def _format(path: Path, pages: list[str], rng: tuple[int, int] | None) -> str:
    unit = unit_name(path)
    a, b = rng if rng else (1, len(pages))
    out, total = [], 0
    for i in range(a - 1, b):
        chunk = f"[{unit}.{i + 1}] {pages[i].strip()}"
        total += len(chunk)
        if total > MAX_MATERIAL_CHARS:
            out.append("[materials truncated]")
            break
        out.append(chunk)
    return "\n\n".join(out)


def _result(path: Path, pages: list[str], mode: str, rng, score: float, reason: str) -> dict:
    code = rel(path).split("/", 1)[0]
    fm = moodle_meta(code).get(rel(path))
    return {
        "moodle": {k: fm.get(k) for k in ("section", "week", "lectures", "topic")} if fm else None,
        "mode": mode,
        "file": rel(path),
        "name": path.name,
        "unit": unit_name(path),
        "pages": list(rng) if rng else None,
        "page_count": len(pages),
        "score": round(score, 3),
        "reason": reason,
        "text": _format(path, pages, rng),
    }


# ---------------------------------------------------------------- matching

def module_profile(code: str | None) -> dict:
    """How the module uses Moodle (written by the Moodle sync)."""
    if not code:
        return {}
    try:
        return json.loads((module_dir(code) / MANIFEST).read_text(encoding="utf-8")).get("profile") or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def moodle_meta(code: str | None) -> dict:
    """rel path -> {kind, week, lectures, topic} for files the Moodle sync downloaded."""
    if not code:
        return {}
    try:
        return json.loads((module_dir(code) / MANIFEST).read_text(encoding="utf-8")).get("files") or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def match_materials(code: str | None, lecture: dict, transcript: str, slide_titles: str = "",
                    claimed: set[str] | None = None, override: dict | None = None,
                    mode: str | None = None) -> dict | None:
    """
    lecture: {"session_name": ..., "date": "YYYY-MM-DD"}
    override: {"mode": "none"} | {"mode": "file", "file": rel, "pages": [a, b] | None}
    Returns a match dict (with "text" for the prompt) or None.
    """
    claimed = claimed or set()
    week, ordinal = lecture.get("week"), lecture.get("ordinal")
    meta = moodle_meta(code)
    if override and override.get("mode") == "none":
        return None
    if override and override.get("file"):
        path = resolve(override["file"])
        pages = extract_pages(path)
        rng = tuple(override["pages"]) if override.get("pages") else None
        return _result(path, pages, "pack" if rng else "file", rng, 1.0, "chosen manually")
    if not code:
        return None

    scan = scan_module(code, mode)
    name = lecture.get("session_name") or ""
    lec_toks = tokens(f"{transcript} {slide_titles} {slide_titles} {name}")
    num = lecture_number(name)

    if scan["per_lecture"]:
        files = scan["per_lecture"]
        texts = [extract_pages(f) for f in files]
        docs = [tokens(" ".join(t) + " " + f.stem) for f, t in zip(files, texts)]
        tf = Tfidf(docs + [lec_toks])
        lv = tf.vec(lec_toks)
        best = None
        for f, pages, toks in zip(files, texts, docs):
            sim = cosine(tf.vec(toks), lv)
            score, reasons = sim, [f"content match {sim:.2f}"]
            if num is not None and lecture_number(f.stem) == num:
                score += 0.35
                reasons.append(f"lecture {num} in name")
            if date_in_name(f.stem, lecture.get("date")):
                score += 0.4
                reasons.append("date in name")
            fm = meta.get(rel(f)) or {}
            if ordinal and ordinal in (fm.get("lectures") or []):
                score += 0.45
                reasons.append(f"Moodle says lecture {ordinal}")
            if week and fm.get("week") == week:
                score += 0.3
                reasons.append(f"Moodle week {week}")
            elif week and fm.get("week") and abs(fm["week"] - week) > 2:
                score -= 0.2
            if rel(f) in claimed:
                score -= 0.15
                reasons.append("already used by another lecture")
            if best is None or score > best[0]:
                best = (score, f, pages, reasons)
        if best and best[0] >= 0.2:
            return _result(best[1], best[2], "file", None, best[0], ", ".join(best[3]))

    best_pack = None
    for pack in scan["packs"]:
        pages = extract_pages(pack)
        rng, score = find_section(pages, lec_toks)
        if rng and (best_pack is None or score > best_pack[0]):
            best_pack = (score, pack, pages, rng)
    if best_pack and best_pack[0] >= 0.05:
        score, pack, pages, rng = best_pack
        u = unit_name(pack)
        return _result(pack, pages, "pack", rng, score, f"best matching section {u}.{rng[0]} to {u}.{rng[1]}")
    return None


# ---------------------------------------------------------------- uploads + past questions

def file_hash(path: Path) -> str:
    st = path.stat()
    tag = f"|{VISION_VERSION}" if has_vision(path) else ""
    return hashlib.sha1(f"{rel(path)}|{st.st_mtime_ns}|{st.st_size}{tag}".encode()).hexdigest()


def module_files(code: str, mode: str | None = None) -> list[tuple[Path, str]]:
    """Every material in a module folder with its kind: slides, pack or questions."""
    scan = scan_module(code, mode)
    return ([(f, "slides") for f in scan["per_lecture"]] + [(f, "pack") for f in scan["packs"]]
            + [(f, "questions") for f in scan["questions"]] + [(f, "info") for f in scan["info"]])


def chunk_for_upload(path: Path) -> list[dict]:
    """Group consecutive pages into ~6k character chunks, keeping page numbers."""
    pages = extract_pages(path)
    chunks, buf, start = [], [], 1
    unit = unit_name(path)
    for i, text in enumerate(pages, 1):
        piece = f"[{unit}.{i}] {text.strip()}"
        if buf and sum(len(b) for b in buf) + len(piece) > UPLOAD_CHUNK_CHARS:
            chunks.append({"page_start": start, "page_end": i - 1, "unit": unit, "content": "\n\n".join(buf)})
            buf, start = [], i
        buf.append(piece)
    if buf:
        chunks.append({"page_start": start, "page_end": len(pages), "unit": unit, "content": "\n\n".join(buf)})
    return [c for c in chunks if c["content"].strip()]


def relevant_questions(code: str | None, transcript: str, top: int = 4, min_sim: float = 0.06,
                       week: int | None = None) -> list[dict]:
    """Past/example questions in the module's questions folder that relate to this lecture."""
    if not code:
        return []
    files = scan_module(code)["questions"]
    if not files:
        return []
    pieces = []
    for f in files:
        try:
            pages = extract_pages(f)
        except Exception:
            continue
        for i, text in enumerate(pages, 1):
            if text.strip():
                pieces.append((f, i, text))
    if not pieces:
        return []
    lec = tokens(transcript)
    docs = [tokens(t) for _, _, t in pieces]
    tf = Tfidf(docs + [lec])
    lv = tf.vec(lec)
    scored = sorted(((cosine(tf.vec(d), lv), p) for d, p in zip(docs, pieces) if d), key=lambda x: -x[0])
    out, seen = [], set()
    for sim, (f, i, text) in scored[:top]:
        if sim < min_sim:
            break
        seen.add((rel(f), i))
        out.append({"file": rel(f), "name": f.name, "ref": f"{f.name} {unit_name(f)}.{i}",
                    "score": round(sim, 3), "text": text.strip()[:2500]})
    if week:  # this week's problem sheet, per Moodle, even if wording differs
        meta = moodle_meta(code)
        for sim, (f, i, text) in scored:
            fm = meta.get(rel(f)) or {}
            if fm.get("week") == week and (rel(f), i) not in seen and i <= 3:
                seen.add((rel(f), i))
                out.append({"file": rel(f), "name": f.name, "ref": f"{f.name} {unit_name(f)}.{i}",
                            "score": round(sim, 3), "text": text.strip()[:2500]})
    out = out[:top + 3]
    # The lecturer's handwritten worked examples (scans) always get a slot, kept whole:
    # this week's first, then the closest matches.
    meta = moodle_meta(code) if week else {}
    worked = [(sim, p) for sim, p in scored if VISION_MARK in p[2]]
    worked.sort(key=lambda x: (-(1 if week and (meta.get(rel(x[1][0])) or {}).get("week") == week else 0), -x[0]))
    taken = 0
    for sim, (f, i, text) in worked:
        if taken >= 6:
            break
        in_week = week and (meta.get(rel(f)) or {}).get("week") == week
        if not in_week and sim < min_sim:
            continue
        key = (rel(f), i)
        for o in out:
            if (o["file"], o["ref"]) == (rel(f), f"{f.name} {unit_name(f)}.{i}"):
                o["text"] = text.strip()[:6000]
                o["worked"] = True
                break
        else:
            out.append({"file": rel(f), "name": f.name, "ref": f"{f.name} {unit_name(f)}.{i} (lecturer's worked example)",
                        "score": round(sim, 3), "text": text.strip()[:6000], "worked": True})
        taken += 1
    return out


# ---------------------------------------------------------------- tutorial sheets and coursework

COURSEWORK_RE = re.compile(r"coursework|assignment|brief|\bcw\d*\b|project|report template|marking|rubric|assessment", re.I)
SOLUTION_RE = re.compile(r"solution|answers|worked|mark ?scheme", re.I)


def _file_text(path: Path, limit: int) -> str:
    try:
        pages = extract_pages(path)
    except Exception:
        return ""
    unit = unit_name(path)
    out, total = [], 0
    for i, t in enumerate(pages, 1):
        chunk = f"[{unit}.{i}] {t.strip()}"
        total += len(chunk)
        if total > limit:
            out.append("[truncated]")
            break
        out.append(chunk)
    return "\n".join(out)


def tutorial_context(code: str | None, transcript: str, week: int | None = None, ordinal: int | None = None,
                     limit_chars: int = 18000) -> list[dict]:
    """The tutorial/problem sheets (and their solutions) that go with this lecture."""
    if not code:
        return []
    files = scan_module(code)["questions"]
    if not files:
        return []
    meta = moodle_meta(code)
    lec = tokens(transcript)
    docs = []
    for f in files:
        try:
            docs.append(tokens(" ".join(extract_pages(f)) + " " + f.stem))
        except Exception:
            docs.append([])
    tf = Tfidf(docs + [lec])
    lv = tf.vec(lec)
    scored = []
    for f, d in zip(files, docs):
        fm = meta.get(rel(f)) or {}
        s = cosine(tf.vec(d), lv) if d else 0.0
        if week and fm.get("week") == week:
            s += 0.35
        if ordinal and ordinal in (fm.get("lectures") or []):
            s += 0.35
        if SOLUTION_RE.search(f.stem):
            s -= 0.02  # sheets first, solutions paired below
        scored.append((s, f))
    scored.sort(key=lambda x: -x[0])
    picked, out, budget = [], [], limit_chars
    for s, f in scored:
        if s < 0.08 or len(picked) >= 2:
            break
        if SOLUTION_RE.search(f.stem):
            continue
        picked.append(f)
    if not picked and scored and scored[0][0] >= 0.08:
        picked.append(scored[0][1])
    for f in picked:
        base = re.sub(SOLUTION_RE, "", f.stem).lower()
        sol = next((g for g in files if g != f and SOLUTION_RE.search(g.stem)
                    and (re.sub(SOLUTION_RE, "", g.stem).lower().strip(" _-") == base.strip(" _-")
                         or base.strip(" _-")[:12] in g.stem.lower())), None)
        txt = _file_text(f, budget // 2)
        budget -= len(txt)
        item = {"file": rel(f), "name": f.name, "text": txt,
                "week": (meta.get(rel(f)) or {}).get("week")}
        if sol and budget > 1000:
            st = _file_text(sol, min(budget, 8000))
            budget -= len(st)
            item["solutions_name"] = sol.name
            item["solutions_text"] = st
        out.append(item)
    return out


def coursework_context(code: str | None, transcript: str, limit_chars: int = 9000) -> list[dict]:
    """Coursework or assignment briefs for the module, most relevant first."""
    if not code:
        return []
    meta = moodle_meta(code)
    cands = []
    for f in scan_module(code)["info"]:
        fm = meta.get(rel(f)) or {}
        label = f"{f.stem} {fm.get('topic') or ''} {fm.get('section') or ''} {fm.get('moodle') or ''}"
        if f.suffix.lower() == ".md" and f.stem.lower().startswith("moodle structure"):
            continue
        if COURSEWORK_RE.search(label) and not re.search(r"example|exemplar|team \d|final_report", f.stem, re.I):
            cands.append(f)
    if not cands:
        return []
    lec = tokens(transcript)
    docs = [tokens(" ".join(extract_pages(f))[:60000]) for f in cands]
    tf = Tfidf(docs + [lec])
    lv = tf.vec(lec)
    ranked = sorted(zip(cands, docs), key=lambda x: -cosine(tf.vec(x[1]), lv) if x[1] else 0)
    out, budget = [], limit_chars
    for f, _ in ranked[:2]:
        txt = _file_text(f, budget)
        budget -= len(txt)
        out.append({"file": rel(f), "name": f.name, "text": txt})
        if budget < 1500:
            break
    return out


EXEMPLAR_RE = re.compile(r"example|exemplar|sample|team \\d|final_report|past report|good report", re.I)


def exemplar_context(code: str | None, limit_chars: int = 6000) -> list[dict]:
    """Example or past coursework reports, used to show what high-scoring work looks like."""
    if not code:
        return []
    out, budget = [], limit_chars
    for f in scan_module(code)["info"]:
        if EXEMPLAR_RE.search(f.stem) and f.suffix.lower() in (".pdf", ".docx", ".pptx"):
            txt = _file_text(f, min(budget, 3500))
            budget -= len(txt)
            out.append({"file": rel(f), "name": f.name, "text": txt})
            if budget < 1500 or len(out) >= 2:
                break
    return out
