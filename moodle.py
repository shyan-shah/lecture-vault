"""
Moodle sync: works out how each module is laid out on Moodle, then pulls the
files the notes need into materials/<module>/ with week and lecture tags.

Access, in order of preference:
  1. Moodle's web service API with a token from the Moodle app login flow
     (admin/tool/mobile/launch.php). Uses the signed-in Bath session once, then
     the token works on its own. Structured, so it survives theme changes.
  2. Fallback: reading the course pages with the signed-in browser session.

Per module, Claude classifies every item once (re-run only when Moodle changes):
  slides     -> materials/<code>/            matched to single lectures
  pack       -> materials/<code>/pack/       one document covering many lectures
  questions  -> materials/<code>/questions/  problem sheets, solutions, past papers
  info       -> materials/<code>/info/       handbook, coursework briefs, admin
  skip       -> not downloaded (videos, Panopto links, forums, duplicates)
and writes a short profile of how the module uses Moodle, which goes into the
notes prompt and into info/Moodle structure.md for the module chats.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import materials as mat
from promptlib import P

ROOT = Path(__file__).resolve().parent
MOODLE_HOST = os.getenv("MOODLE_HOST", "https://moodle.bath.ac.uk").rstrip("/")
TOKEN_FILE = ROOT / ".moodle-token.json"
MAX_FILE_MB = float(os.getenv("MOODLE_MAX_FILE_MB", "80"))
DOC_EXT = {".pdf", ".pptx", ".docx", ".txt", ".md"}
KIND_DIRS = {"slides": "", "pack": "pack", "questions": "questions", "info": "info"}
MANIFEST = ".moodle.json"


class MoodleError(RuntimeError):
    pass


class NeedMoodleLogin(MoodleError):
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] moodle: {msg}", flush=True)


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name).strip().rstrip(".")
    return re.sub(r"\s+", " ", name)[:120] or "file"


def strip_html(text: str | None, limit: int = 300) -> str:
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"\s+", " ", html.unescape(t)).strip()
    return t[:limit]


# ---------------------------------------------------------------- semester weeks

def teaching_week(day: str | date | None, semester_start: str | None) -> int | None:
    """Week 1 starts on the configured semester start (a Monday)."""
    if not day or not semester_start:
        return None
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    s = date.fromisoformat(semester_start)
    if d < s:
        return None
    return (d - s).days // 7 + 1


# ---------------------------------------------------------------- client

class _RetryingRequest:
    """Moodle's server sometimes drops connections mid-request (ECONNRESET, socket hang up).
    Retry those a few times with a short backoff instead of failing the whole module."""

    TRANSIENT = ("econnreset", "socket hang up", "timeout", "etimedout", "econnrefused", "epipe",
                 "network", "connection closed", "eai_again")

    def __init__(self, req, attempts: int = 4):
        self._req, self._attempts = req, attempts

    def _call(self, fn, *a, **kw):
        import time as _t
        for i in range(self._attempts):
            try:
                return fn(*a, **kw)
            except Exception as e:
                msg = str(e).lower()
                if i == self._attempts - 1 or not any(t in msg for t in self.TRANSIENT):
                    raise
                log(f"moodle request retry {i + 1}: {str(e).splitlines()[0][:120]}")
                _t.sleep(2 * (i + 1))

    def get(self, *a, **kw):
        return self._call(self._req.get, *a, **kw)

    def post(self, *a, **kw):
        return self._call(self._req.post, *a, **kw)


class Moodle:
    """Uses the Panopto worker's browser context (same thread, same Bath session)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.req = _RetryingRequest(ctx.request)
        self.token: str | None = None
        self.userid: int | None = None
        self.mode: str | None = None  # "api" | "html"

    # -- session -------------------------------------------------------

    def _logged_in_page(self, page) -> bool:
        u = urlparse(page.url)
        if u.netloc != urlparse(MOODLE_HOST).netloc or "/login/" in u.path:
            return False
        try:
            return bool(page.evaluate("() => !!(window.M && M.cfg && M.cfg.sesskey) && !document.querySelector('form#login')"))
        except Exception:
            return False

    def has_session(self) -> bool:
        page = self.ctx.new_page()
        try:
            page.goto(f"{MOODLE_HOST}/my/", wait_until="domcontentloaded", timeout=45000)
            # Bath SSO may bounce through the identity provider and straight back
            for _ in range(10):
                if self._logged_in_page(page):
                    return True
                page.wait_for_timeout(1500)
            return False
        except Exception:
            return False
        finally:
            page.close()

    def wait_for_login(self, page, timeout_s: int = 240) -> bool:
        """Interactive: user is looking at this (visible) page."""
        page.goto(f"{MOODLE_HOST}/my/")
        waited = 0
        while waited < timeout_s:
            if self._logged_in_page(page):
                return True
            page.wait_for_timeout(2000)
            waited += 2
        return False

    # -- token (Moodle app login flow) ---------------------------------

    def _load_token(self) -> bool:
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if data.get("host") == MOODLE_HOST and data.get("token"):
                self.token = data["token"]
                return True
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return False

    def _fetch_token(self) -> bool:
        passport = secrets.token_hex(8)
        url = (f"{MOODLE_HOST}/admin/tool/mobile/launch.php?service=moodle_mobile_app"
               f"&passport={passport}&urlscheme=moodlemobile")
        location = None
        try:
            r = self.req.get(url, max_redirects=0, timeout=30000)
            location = r.headers.get("location", "")
        except Exception as e:
            log(f"launch request failed: {e}")
            return False
        if not location.startswith("moodlemobile://token="):
            if "/login/" in (location or ""):
                raise NeedMoodleLogin("Moodle session missing")
            log(f"app login flow not available (status redirect: {location[:80]!r})")
            return False
        try:
            raw = base64.b64decode(unquote(location.split("token=", 1)[1]) + "==").decode("utf-8", "replace")
            parts = raw.split(":::")
            sig, token = parts[0], parts[1]
        except Exception:
            log("couldn't decode app token")
            return False
        site = hashlib.md5((MOODLE_HOST + passport).encode()).hexdigest()
        if sig != site:
            log("token signature mismatch (site URL differs); using it anyway")
        self.token = token
        TOKEN_FILE.write_text(json.dumps({"host": MOODLE_HOST, "token": token,
                                          "created": datetime.now().isoformat()}), encoding="utf-8")
        return True

    def call(self, fn: str, **params):
        if not self.token:
            raise MoodleError("no token")
        form = {"wstoken": self.token, "wsfunction": fn, "moodlewsrestformat": "json"}
        for k, v in params.items():
            if isinstance(v, list):
                for i, item in enumerate(v):
                    if isinstance(item, dict):
                        for kk, vv in item.items():
                            form[f"{k}[{i}][{kk}]"] = str(vv)
                    else:
                        form[f"{k}[{i}]"] = str(item)
            else:
                form[k] = str(v)
        r = self.req.post(f"{MOODLE_HOST}/webservice/rest/server.php", form=form, timeout=60000)
        if not r.ok:
            raise MoodleError(f"{fn} HTTP {r.status}")
        data = r.json()
        if isinstance(data, dict) and data.get("exception"):
            if data.get("errorcode") in ("invalidtoken", "accessexception"):
                self.token = None
                TOKEN_FILE.unlink(missing_ok=True)
            raise MoodleError(f"{fn}: {data.get('message') or data.get('errorcode')}")
        return data

    def connect(self) -> str:
        """Returns "api" or "html". Raises NeedMoodleLogin when the browser session is gone."""
        if self._load_token():
            try:
                self.userid = self.call("core_webservice_get_site_info")["userid"]
                self.mode = "api"
                return self.mode
            except MoodleError as e:
                log(f"saved token rejected: {e}")
                self.token = None
        if not self.has_session():
            raise NeedMoodleLogin("Sign in to Moodle")
        if self._fetch_token():
            try:
                self.userid = self.call("core_webservice_get_site_info")["userid"]
                self.mode = "api"
                return self.mode
            except MoodleError as e:
                log(f"new token rejected: {e}")
        self.mode = "html"
        return self.mode

    # -- courses -------------------------------------------------------

    def courses(self) -> list[dict]:
        if self.mode == "api":
            rows = self.call("core_enrol_get_users_courses", userid=self.userid)
            return [{"id": c["id"], "shortname": c.get("shortname", ""), "fullname": c.get("fullname", ""),
                     "url": f"{MOODLE_HOST}/course/view.php?id={c['id']}"} for c in rows]
        page = self.ctx.new_page()
        try:
            page.goto(f"{MOODLE_HOST}/my/courses.php", wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)
            links = page.eval_on_selector_all(
                "a[href*='/course/view.php?id=']",
                "els => els.map(e => ({href: e.href, text: (e.innerText || e.title || '').trim()}))")
        finally:
            page.close()
        seen, out = set(), []
        for l in links:
            cid = parse_qs(urlparse(l["href"]).query).get("id", [None])[0]
            if cid and cid not in seen and l["text"]:
                seen.add(cid)
                out.append({"id": int(cid), "shortname": l["text"], "fullname": l["text"], "url": l["href"]})
        return out

    # -- inventory -----------------------------------------------------

    def inventory(self, course_id: int) -> list[dict]:
        return self._inventory_api(course_id) if self.mode == "api" else self._inventory_html(course_id)

    def _inventory_api(self, course_id: int) -> list[dict]:
        sections = self.call("core_course_get_contents", courseid=course_id)
        items = []
        for si, sec in enumerate(sections):
            sname = strip_html(sec.get("name"), 120) or f"Section {si}"
            ssum = strip_html(sec.get("summary"), 300)
            for m in sec.get("modules", []):
                base = {"section": si, "section_name": sname, "section_summary": ssum,
                        "activity": strip_html(m.get("name"), 160), "modname": m.get("modname"),
                        "description": strip_html(m.get("description"), 200),
                        "visible": m.get("uservisible", True)}
                files = [c for c in (m.get("contents") or []) if c.get("type") == "file"]
                if m.get("modname") in ("resource", "folder") and files:
                    for c in files:
                        items.append({**base, "filename": c.get("filename"),
                                      "folder_path": c.get("filepath") if m.get("modname") == "folder" else "",
                                      "size": c.get("filesize") or 0, "mtime": c.get("timemodified") or 0,
                                      "url": c.get("fileurl"), "source_id": f"{m.get('id')}:{c.get('filepath','')}{c.get('filename')}"})
                else:
                    items.append({**base, "filename": None, "size": 0, "mtime": 0,
                                  "url": m.get("url"), "source_id": str(m.get("id"))})
        return items

    SECTION_JS = """() => {
      const main = document.querySelector('#region-main') || document;
      return [...main.querySelectorAll('li.course-section, li.section')].map((sec, i) => {
        const a = sec.querySelector('a[href*="section.php?id="]');
        const sid = sec.getAttribute('data-id') || (a ? new URL(a.href).searchParams.get('id') : null);
        return {
          num: Number(sec.getAttribute('data-number') ?? i),
          name: (sec.getAttribute('data-sectionname') || sec.querySelector('.sectionname, h3')?.innerText || '').trim(),
          summary: (sec.querySelector('.summarytext, .summary')?.innerText || '').trim().slice(0, 300),
          sid,
        };
      });
    }"""

    ACTIVITY_JS = """() => {
      const main = document.querySelector('#region-main') || document;
      const out = [];
      main.querySelectorAll('li.activity, [data-for="cmitem"]').forEach(act => {
        if (act.parentElement && act.parentElement.closest('li.activity, [data-for="cmitem"]')) return;
        const a = act.querySelector('a.aalink, a[href*="/mod/"]');
        if (!a) return;
        const mod = (act.className.match(/modtype_(\\w+)/) || [])[1] || (a.href.match(/\\/mod\\/(\\w+)\\//) || [])[1] || '';
        const nm = act.querySelector('.instancename')?.childNodes[0]?.textContent || a.innerText;
        const desc = (act.querySelector('.activity-altcontent, .contentafterlink, .description')?.innerText || '').trim().slice(0, 200);
        out.push({href: a.href, mod, act: (nm || '').trim(), desc});
      });
      const seen = new Set();
      return out.filter(o => !seen.has(o.href) && seen.add(o.href));
    }"""

    def _inventory_html(self, course_id: int) -> list[dict]:
        """Moodle 4 loads each section's activities lazily, so read every section's own page."""
        page = self.ctx.new_page()
        rows = []
        try:
            page.goto(f"{MOODLE_HOST}/course/view.php?id={course_id}", wait_until="networkidle", timeout=60000)
            sections = page.evaluate(self.SECTION_JS)
            for sec in sections:
                acts = []
                if sec.get("sid"):
                    try:
                        page.goto(f"{MOODLE_HOST}/course/section.php?id={sec['sid']}", wait_until="networkidle", timeout=60000)
                        acts = page.evaluate(self.ACTIVITY_JS)
                        if not sec.get("summary"):
                            try:
                                sec["summary"] = (page.inner_text("#region-main .summarytext", timeout=1500) or "")[:300]
                            except Exception:
                                pass
                    except Exception as e:
                        log(f"section {sec.get('name')}: {e}")
                rows.append((sec, acts))
        finally:
            page.close()
        items = []
        for sec, acts in rows:
            for r in acts:
                base = {"section": sec["num"], "section_name": html.unescape(sec["name"]) or f"Section {sec['num']}",
                        "section_summary": html.unescape(sec.get("summary") or ""), "activity": html.unescape(r["act"])[:160], "modname": r["mod"],
                        "description": r.get("desc") or "", "visible": True}
                cmid = parse_qs(urlparse(r["href"]).query).get("id", [""])[0]
                if r["mod"] == "folder":
                    for f in self._folder_files(r["href"]):
                        items.append({**base, **f, "source_id": f"{cmid}:{f['filename']}"})
                elif r["mod"] == "resource":
                    items.append({**base, "filename": None, "size": 0, "mtime": 0,
                                  "url": r["href"] + ("&" if "?" in r["href"] else "?") + "redirect=1",
                                  "source_id": cmid, "resolve": True})
                else:
                    items.append({**base, "filename": None, "size": 0, "mtime": 0, "url": r["href"], "source_id": cmid})
        return items

    def _folder_files(self, url: str) -> list[dict]:
        r = self.req.get(url, timeout=60000)
        if not r.ok:
            return []
        out = []
        for href in set(re.findall(r'href="([^"]*pluginfile\.php[^"]+)"', r.text())):
            href = html.unescape(href)
            name = unquote(urlparse(href).path.rsplit("/", 1)[-1])
            out.append({"filename": name, "folder_path": "", "size": 0, "mtime": 0, "url": href})
        return out

    # -- download ------------------------------------------------------

    def download(self, item: dict, dest: Path) -> Path | None:
        url = item["url"]
        if self.mode == "api" and "pluginfile.php" in url:
            url = url.replace("/pluginfile.php", "/webservice/pluginfile.php") if "/webservice/" not in url else url
            url += ("&" if "?" in url else "?") + f"token={self.token}"
        r = self.req.get(url, timeout=180000)
        if not r.ok:
            raise MoodleError(f"download HTTP {r.status} for {item.get('filename') or item.get('activity')}")
        ctype = r.headers.get("content-type", "")
        if "text/html" in ctype:
            # resource page that didn't redirect: find the file link
            m = re.search(r'href="([^"]*pluginfile\.php[^"]+)"', r.text())
            if not m:
                return None
            href = html.unescape(m.group(1))
            r = self.req.get(href, timeout=180000)
            if not r.ok:
                raise MoodleError(f"download HTTP {r.status}")
            if not item.get("filename"):
                item["filename"] = unquote(urlparse(href).path.rsplit("/", 1)[-1])
        if not item.get("filename"):
            cd = r.headers.get("content-disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
            item["filename"] = unquote(m.group(1)) if m else f"{item.get('activity') or 'file'}.pdf"
        body = r.body()
        if len(body) > MAX_FILE_MB * 1024 * 1024:
            return None
        if Path(item["filename"]).suffix.lower() not in DOC_EXT:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return dest



def _stream_call(client, **kw):
    """Long note-writing calls must stream (the SDK refuses >10 minute non-streaming requests)."""
    with client.messages.stream(**kw) as st:
        return st.get_final_message()

# ---------------------------------------------------------------- classification (Claude)



def classify(code: str, module_name: str, inventory: list[dict], lectures: list[dict], semester_start: str) -> dict:
    import anthropic

    model = os.getenv("NOTES_MODEL", "claude-sonnet-5-5")
    lines = []
    for i, it in enumerate(inventory):
        parts = [f"{i}", f"S{it['section']} {it['section_name']}", f"[{it['modname']}] {it['activity']}"]
        if it.get("filename"):
            parts.append(f"file: {it.get('folder_path') or ''}{it['filename']}"
                         + (f" ({round(it['size'] / 1e6, 1)} MB)" if it.get("size") else ""))
        if it.get("description"):
            parts.append(f"desc: {it['description'][:120]}")
        lines.append(" | ".join(parts))
    summaries = {}
    for it in inventory:
        if it.get("section_summary") and it["section"] not in summaries:
            summaries[it["section"]] = f"S{it['section']} {it['section_name']}: {it['section_summary'][:200]}"
    lec = "\n".join(f"{n}. {l['date']} {l['title']}" for n, l in enumerate(lectures, 1)) or "(none known yet)"
    prompt = P.classify_request(code, module_name, semester_start, lec, summaries, lines)
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        resp = _stream_call(client, model=model, max_tokens=16000,
                                      system=P.CLASSIFY_SYSTEM + P.CLASSIFY_ONCE,
                                      tools=[P.CLASSIFY_TOOL], tool_choice={"type": "auto"}, messages=messages)
        for block in resp.content:
            if block.type == "tool_use" and block.name == "file_moodle_map":
                out = dict(block.input)
                for k in ("profile", "items"):
                    if isinstance(out.get(k), str):
                        try:
                            out[k] = json.loads(out[k])
                        except json.JSONDecodeError:
                            out[k] = {} if k == "profile" else []
                out["items"] = [i for i in (out.get("items") or []) if isinstance(i, dict)]
                return out
        messages = messages + [{"role": "assistant", "content": resp.content},
                               {"role": "user", "content": P.CLASSIFY_NUDGE}]
    raise MoodleError("Classification returned nothing")


# ---------------------------------------------------------------- manifest

def manifest_path(code: str) -> Path:
    return mat.module_dir(code) / MANIFEST


def load_manifest(code: str) -> dict:
    try:
        return json.loads(manifest_path(code).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_manifest(code: str, data: dict) -> None:
    p = manifest_path(code)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")


def inventory_signature(inventory: list[dict]) -> str:
    keys = sorted(f"{it['source_id']}|{it.get('mtime')}|{it['section_name']}|{it['activity']}" for it in inventory)
    return hashlib.sha1("\n".join(keys).encode()).hexdigest()


def write_profile_doc(code: str, module_name: str, man: dict) -> None:
    prof = man.get("profile") or {}
    files = man.get("files") or {}
    by_kind: dict[str, list] = {}
    for rel, meta in sorted(files.items(), key=lambda kv: ((kv[1].get("week") or 99), kv[0])):
        by_kind.setdefault(meta["kind"], []).append((rel, meta))
    out = [f"# {code} {module_name}: Moodle structure", "",
           f"Layout: {prof.get('layout', '?')}. Lecture material: {prof.get('lecture_material', '?')}.", "",
           prof.get("summary", ""), ""]
    names = {"slides": "Lecture files", "pack": "Notes packs", "questions": "Problem sheets, solutions and past papers",
             "info": "Handbook, coursework and admin"}
    for kind in ("slides", "pack", "questions", "info"):
        if by_kind.get(kind):
            out.append(f"## {names[kind]}")
            for rel, meta in by_kind[kind]:
                tag = []
                if meta.get("week"):
                    tag.append(f"week {meta['week']}")
                if meta.get("lectures"):
                    tag.append("lecture " + ", ".join(map(str, meta["lectures"])))
                out.append(f"* {Path(rel).name}" + (f" ({'; '.join(tag)})" if tag else "")
                           + (f": {meta['topic']}" if meta.get("topic") else ""))
            out.append("")
    p = mat.module_dir(code) / "info" / "Moodle structure.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(out), encoding="utf-8")


# ---------------------------------------------------------------- sync

def find_course(code: str, info: dict, courses: list[dict]) -> dict | None:
    if info.get("moodle_id"):
        return next((c for c in courses if c["id"] == int(info["moodle_id"])), {"id": int(info["moodle_id"]),
                                                                                   "shortname": code, "fullname": code})
    hits = [c for c in courses if code.lower() in (c["shortname"] + " " + c["fullname"]).lower()]
    if not hits:
        return None
    # prefer this academic year's course if several
    hits.sort(key=lambda c: ("2026" not in c["fullname"] + c["shortname"], -c["id"]))
    return hits[0]


def sync_module(m: Moodle, code: str, info: dict, course: dict, lectures: list[dict], semester_start: str,
                say=log, force_classify: bool = False) -> dict:
    inv = [it for it in m.inventory(course["id"]) if it.get("visible", True)]
    man = load_manifest(code)
    sig = inventory_signature(inv)
    if force_classify or man.get("signature") != sig or not man.get("profile"):
        say(f"{code}: mapping {len(inv)} Moodle items")
        result = classify(code, info.get("name", code), inv, lectures, semester_start)
        cls = {c["i"]: c for c in result.get("items", []) if isinstance(c.get("i"), int)}
        man["profile"] = result.get("profile", {})
        man["signature"] = sig
        man["classified_at"] = datetime.now(timezone.utc).isoformat()
        man["items"] = {it["source_id"]: {**{k: it.get(k) for k in ("section_name", "activity", "filename", "modname")},
                                          **{k: cls.get(i, {}).get(k) for k in ("kind", "week", "lectures", "topic")}}
                        for i, it in enumerate(inv)}
    man["course"] = {"id": course["id"], "name": course.get("fullname"), "mode": m.mode}
    files = man.setdefault("files", {})
    downloaded = skipped = 0
    for it in inv:
        meta = man["items"].get(it["source_id"]) or {}
        kind = meta.get("kind") or "skip"
        if kind == "skip" or it["modname"] not in ("resource", "folder"):
            continue
        prev = next(((rel, f) for rel, f in files.items() if f.get("source_id") == it["source_id"]), None)
        if prev and prev[1].get("mtime") == it.get("mtime") and (mat.MATERIALS_DIR / prev[0]).exists() and it.get("mtime"):
            prev[1].update({"kind": kind, "week": meta.get("week"), "lectures": meta.get("lectures") or [], "topic": meta.get("topic")})
            continue
        if it.get("filename") and Path(it["filename"]).suffix.lower() not in DOC_EXT:
            skipped += 1
            continue
        if it.get("size") and it["size"] > MAX_FILE_MB * 1024 * 1024:
            skipped += 1
            continue
        try:
            name = it.get("filename")
            sub = KIND_DIRS[kind]
            folder = mat.module_dir(code) / sub if sub else mat.module_dir(code)
            if not name:  # html mode resource: name known only after download
                tmp = folder / safe_name(f".dl-{it['source_id']}")
                got = m.download(it, tmp)
                if not got:
                    skipped += 1
                    continue
                dest = folder / safe_name(it["filename"])
                if dest.exists() and dest.read_bytes() != tmp.read_bytes():
                    dest = folder / safe_name(f"{Path(it['filename']).stem} ({it['source_id']}){Path(it['filename']).suffix}")
                tmp.replace(dest)
            else:
                stem = safe_name(Path(name).stem)
                if it.get("folder_path") and it["folder_path"] not in ("", "/"):
                    stem = safe_name(it["folder_path"].strip("/").replace("/", " ") + " " + stem)
                dest = folder / f"{stem}{Path(name).suffix.lower()}"
                if not m.download(it, dest):
                    skipped += 1
                    continue
            if prev and prev[0] != mat.rel(dest):
                files.pop(prev[0], None)
            files[mat.rel(dest)] = {"source_id": it["source_id"], "mtime": it.get("mtime"), "kind": kind,
                                    "week": meta.get("week"), "lectures": meta.get("lectures") or [],
                                    "topic": meta.get("topic"), "section": it["section_name"],
                                    "moodle": it.get("activity")}
            downloaded += 1
        except MoodleError as e:
            log(f"{code}: {e}")
            skipped += 1
    # files that moved to a different kind folder after reclassification
    for rel, f in list(files.items()):
        want = KIND_DIRS.get(f.get("kind") or "", None)
        p = mat.MATERIALS_DIR / rel
        if not p.exists():
            files.pop(rel)
            continue
        if want is not None:
            target_dir = mat.module_dir(code) / want if want else mat.module_dir(code)
            if p.parent.resolve() != target_dir.resolve():
                target_dir.mkdir(parents=True, exist_ok=True)
                new = target_dir / p.name
                if not new.exists():
                    p.replace(new)
                    files[mat.rel(new)] = files.pop(rel)
    man["synced_at"] = datetime.now(timezone.utc).isoformat()
    save_manifest(code, man)
    write_profile_doc(code, info.get("name", code), man)
    counts = {}
    for f in files.values():
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return {"downloaded": downloaded, "skipped": skipped, "counts": counts,
            "layout": (man.get("profile") or {}).get("layout"), "mode": m.mode}


def module_profile(code: str | None) -> dict:
    if not code:
        return {}
    return load_manifest(code).get("profile") or {}


def file_meta(code: str | None) -> dict:
    """rel path -> {kind, week, lectures, topic} for Moodle-sourced files."""
    if not code:
        return {}
    return load_manifest(code).get("files") or {}
