#!/usr/bin/env python3
"""
Panopto -> The Vault lecture ingester (core pipeline + CLI).

For each lecture: captions (or local Whisper), matched lecture materials
(per-lecture file or the right section of a module pack), Claude notes that
compare what was said with what is in the materials, filed to academic_notes
and logged to memory_entities. The web app (app.py) drives the same pipeline.

Commands:
  python panopto_vault.py login                 sign in once with Bath SSO
  python panopto_vault.py ingest <url> [...]    process specific lectures
  python panopto_vault.py folder <url|id>       process a whole Panopto folder
  python panopto_vault.py watch                 process new lectures in config.json folders
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import materials  # noqa: E402  (needs .env loaded for MATERIALS_DIR)

from promptlib import P  # prompts.py is private, see promptlib.py

STUDENT = os.getenv("STUDENT_PROFILE", "").strip() or "university engineering student"  # set by setup_wizard.py
PANOPTO_HOST = os.getenv("PANOPTO_HOST", "https://uniofbath.cloud.panopto.eu").rstrip("/")
PROFILE_DIR = ROOT / ".browser-profile"
COOKIE_FILE = ROOT / ".panopto-cookies.json"
NOTES_DIR = Path(os.getenv("NOTES_DIR", ROOT / "notes"))
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.json"
NOTES_MODEL = os.getenv("NOTES_MODEL", "claude-sonnet-5-5")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small.en")
CAPTION_GRACE_HOURS = float(os.getenv("CAPTION_GRACE_HOURS", "6"))

GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


CONFIG = load_json(CONFIG_FILE, {"modules": {}, "watch_folders": []})


def reload_config() -> dict:
    global CONFIG
    CONFIG.clear()
    CONFIG.update(load_json(CONFIG_FILE, {"modules": {}, "watch_folders": []}))
    return CONFIG


def save_config() -> None:
    CONFIG_FILE.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")


def watch_folders() -> list[tuple[str, str | None]]:
    """[(folder_id, module_code)] from config.json, ignoring placeholders."""
    out = []
    for f in CONFIG.get("watch_folders", []):
        raw = (f.get("id", "") if isinstance(f, dict) else str(f)).replace("%22", "")
        m = GUID_RE.search(raw)
        if m:
            out.append((m.group(0), f.get("module") if isinstance(f, dict) else None))
    return out


# ---------------------------------------------------------------- helpers

def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def ts_to_seconds(ts: str) -> int:
    m, s = ts.split(":")
    return int(m) * 60 + int(s)


def parse_panopto_wallclock(value) -> datetime | None:
    """Panopto's Data.svc session list encodes London wall-clock time as if it were UTC
    (a 09:15 BST lecture comes back as 09:15Z). Re-read the clock time as Europe/London."""
    dt = parse_panopto_date(value)
    if dt is None:
        return None
    from zoneinfo import ZoneInfo
    return dt.replace(tzinfo=None).replace(tzinfo=ZoneInfo("Europe/London")).astimezone(timezone.utc)


def parse_panopto_date(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    m = re.search(r"/Date\((-?\d+)", str(value))
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_srt(srt: str) -> list[tuple[float, str]]:
    segments = []
    for block in re.split(r"\r?\n\s*\r?\n", srt.strip()):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        m = re.match(r"\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[idx])
        if not m:
            continue
        h, mi, s, ms = (int(x) for x in m.groups())
        text = re.sub(r"<[^>]+>", "", " ".join(lines[idx + 1:])).strip()
        if text:
            segments.append((h * 3600 + mi * 60 + s + ms / 1000, text))
    return segments


def segments_to_transcript(segments: list[tuple[float, str]], window: float = 45.0) -> str:
    """Merge caption fragments into timestamped paragraphs."""
    paras, start, buf = [], None, []
    for t, text in segments:
        if start is None:
            start = t
        buf.append(text)
        if t - start >= window and text.rstrip().endswith((".", "?", "!")):
            paras.append(f"[{fmt_ts(start)}] {' '.join(buf)}")
            start, buf = None, []
    if buf:
        paras.append(f"[{fmt_ts(start or 0)}] {' '.join(buf)}")
    return "\n\n".join(paras)


def viewer_url(delivery_id: str, seconds: int | None = None) -> str:
    url = f"{PANOPTO_HOST}/Panopto/Pages/Viewer.aspx?id={delivery_id}"
    return f"{url}&start={seconds}" if seconds else url


def linkify_timestamps(markdown: str, delivery_id: str) -> str:
    return re.sub(
        r"\[(\d+:\d{2})\](?!\()",
        lambda m: f"[{m.group(1)}]({viewer_url(delivery_id, ts_to_seconds(m.group(1)))})",
        markdown,
    )


def slugify(text: str) -> str:
    return re.sub(r"[^\w\- ]+", "", text).strip()[:80] or "lecture"


def detect_module(*texts: str) -> str | None:
    blob = " ".join(t for t in texts if t).lower()
    for code in CONFIG.get("modules", {}):
        if code.lower() in blob:
            return code
    for code, info in CONFIG.get("modules", {}).items():
        for alias in [info.get("name", "")] + info.get("aliases", []):
            if alias and alias.lower() in blob:
                return code
    return None


# ---------------------------------------------------------------- Panopto

class PanoptoError(RuntimeError):
    pass


class Panopto:
    """Persistent Chromium profile so Bath SSO cookies survive between runs.
    Playwright's sync API is thread bound: create and use an instance from one thread only."""

    def __init__(self, headless: bool = True):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # On Windows use Edge (always installed, self-updating); BROWSER_CHANNEL= (empty) forces Playwright's Chromium.
        channel = os.getenv("BROWSER_CHANNEL", "msedge" if sys.platform.startswith("win") else "")
        try:
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(
                    str(PROFILE_DIR), headless=headless, **({"channel": channel} if channel else {}))
            except Exception:
                if not channel:
                    raise
                self.ctx = self._pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=headless)
        except Exception as e:
            # A half-started Playwright leaves an event loop on this thread, and every later attempt then fails
            # with "Sync API inside the asyncio loop". Always stop it so the next attempt starts clean.
            try:
                self._pw.stop()
            except Exception:
                pass
            msg = str(e).splitlines()[0]
            if "Executable doesn't exist" in msg or "is not found" in msg.lower():
                raise RuntimeError("No browser for sign-in found. On Windows install Microsoft Edge; on a Mac run "
                                   "'playwright install chromium' in the app folder.") from None
            if "user data directory is already in use" in msg.lower() or "profile" in msg.lower():
                raise RuntimeError("The sign-in browser is still open from before. Close any Lecture Vault browser "
                                   "windows (or restart the laptop) and try again.") from None
            raise
        self.req = self.ctx.request
        self._restore_cookies()

    def _restore_cookies(self):
        """Bath SSO issues session cookies, which the browser drops when it closes. Put them back."""
        try:
            saved = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
            if saved:
                self.ctx.add_cookies(saved)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception as e:
            log(f"Couldn't restore Panopto cookies: {e}")

    def save_cookies(self):
        try:
            COOKIE_FILE.write_text(json.dumps(self.ctx.cookies()), encoding="utf-8")
        except Exception as e:
            log(f"Couldn't save Panopto cookies: {e}")

    def close(self):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def _on_panopto(self, url: str) -> bool:
        from urllib.parse import urlparse

        here, home = urlparse(url), urlparse(PANOPTO_HOST)
        path = here.path.lower()
        return here.netloc == home.netloc and "login" not in path and "/auth/" not in path

    def login(self):
        """CLI login: user presses Enter when done."""
        page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        page.goto(f"{PANOPTO_HOST}/Panopto/Pages/Home.aspx")
        print("\nSign in with your university account in the browser window (tick 'stay signed in' if offered).")
        input("Press Enter here once your Panopto home page has loaded... ")
        if not self.is_authenticated():
            raise PanoptoError("Still not signed in. Run login again.")
        self.save_cookies()
        log("Signed in. Session saved to .browser-profile")

    def wait_for_login(self, timeout: int = 300) -> bool:
        """App login: watch the visible browser until the user lands on Panopto signed in."""
        page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        try:
            page.goto(f"{PANOPTO_HOST}/Panopto/Pages/Auth/Login.aspx?ReturnUrl=%2fPanopto%2fPages%2fHome.aspx")
            deadline = time.time() + timeout
            while time.time() < deadline:
                if self._on_panopto(page.url) and self._signed_in():
                    page.wait_for_timeout(1500)
                    self.save_cookies()
                    return True
                page.wait_for_timeout(2000)
        except Exception:
            return False
        return False

    def _signed_in(self) -> bool:
        """Panopto shows public content to anonymous visitors, so check for the auth cookie, not the page."""
        return any(c["name"].upper() == ".ASPXAUTH" and c["value"] for c in self.ctx.cookies(PANOPTO_HOST))

    def is_authenticated(self) -> bool:
        page = self.ctx.new_page()
        try:
            page.goto(f"{PANOPTO_HOST}/Panopto/Pages/Home.aspx", wait_until="domcontentloaded")
            ok = self._on_panopto(page.url) and self._signed_in()
            if ok:
                self.save_cookies()  # keep refreshed cookies for the next run
            return ok
        finally:
            page.close()

    def delivery_info(self, delivery_id: str) -> dict:
        r = self.req.post(
            f"{PANOPTO_HOST}/Panopto/Pages/Viewer/DeliveryInfo.aspx", timeout=90000,
            form={
                "deliveryId": delivery_id, "invocationId": "", "isLiveNotes": "false",
                "refreshAuthCookie": "true", "isActiveBroadcast": "false", "isEditing": "false",
                "isKollectiveAgentInstalled": "false", "isEmbed": "false", "responseType": "json",
            },
        )
        if not r.ok:
            raise PanoptoError(f"DeliveryInfo HTTP {r.status}")
        data = r.json()
        if data.get("ErrorCode"):
            raise PanoptoError(f"Panopto error {data.get('ErrorCode')}: {data.get('ErrorMessage')}")
        return data

    def captions(self, delivery_id: str) -> list[tuple[float, str]] | None:
        for lang in ("0", "English_GBR", "English_USA"):
            r = self.req.get(
                f"{PANOPTO_HOST}/Panopto/Pages/Transcription/GenerateSRT.ashx",
                params={"id": delivery_id, "language": lang},
            )
            if r.ok and "-->" in r.text():
                segs = parse_srt(r.text())
                if segs:
                    return segs
        r = self.req.post(
            f"{PANOPTO_HOST}/Panopto/Pages/Viewer/DeliveryInfo.aspx",
            form={"deliveryId": delivery_id, "getCaptions": "true", "language": "0", "responseType": "json"},
        )
        if r.ok:
            try:
                data = r.json()
            except Exception:
                data = None
            if isinstance(data, list):
                segs = [(float(e.get("Time", 0)), e.get("Caption", "").strip()) for e in data if e.get("Caption")]
                if segs:
                    return segs
        return None

    def folder_sessions(self, folder_id: str | None, max_results: int = 100) -> list[dict]:
        """Sessions in a folder. folder_id=None lists everything you can see."""
        body = {
            "queryParameters": {
                "query": None, "sortColumn": 1, "sortAscending": False,
                "maxResults": max_results, "page": 0, "startDate": None, "endDate": None,
                "folderID": folder_id, "bookmarked": False, "getFolderData": True,
                "isSharedWithMe": False, "isSubscriptionsPage": False,
                "includeArchived": True, "includeArchivedStateCount": True,
                "sessionListOnlyArchived": False, "includePlaylists": True,
            }
        }
        r = None
        for attempt in range(3):  # Panopto is slow under load; retry before giving up
            try:
                r = self.req.post(f"{PANOPTO_HOST}/Panopto/Services/Data.svc/GetSessions", data=body, timeout=90000)
                break
            except Exception as e:
                if attempt == 2:
                    raise PanoptoError(f"Panopto took too long to list recordings (tried 3 times): {str(e).splitlines()[0][:120]}")
                time.sleep(5 * (attempt + 1))
        if not r.ok:
            raise PanoptoError(f"GetSessions HTTP {r.status}")
        results = (r.json().get("d") or {}).get("Results") or []
        return [
            {
                "delivery_id": s.get("DeliveryID"),
                "name": s.get("SessionName"),
                "start": parse_panopto_wallclock(s.get("StartTime")),
                "duration": s.get("Duration"),
                "folder": s.get("FolderName"),
                "folder_id": s.get("FolderID"),
            }
            for s in results if s.get("DeliveryID")
        ]

    def download_audio(self, stream_url: str, out_wav: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                import imageio_ffmpeg  # bundled ffmpeg, no system install needed
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                raise PanoptoError("ffmpeg not found (pip install imageio-ffmpeg)")
        cookies = self.ctx.cookies([stream_url, PANOPTO_HOST])
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        cmd = [ffmpeg, "-y", "-loglevel", "error"]
        if cookie_header:
            cmd += ["-headers", f"Cookie: {cookie_header}\r\n"]
        cmd += ["-i", stream_url, "-vn", "-ac", "1", "-ar", "16000", str(out_wav)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            # never surface the command line: it carries the session cookie
            raise PanoptoError(f"audio download failed: {(r.stderr or '').strip()[-300:] or r.returncode}")


def whisper_transcribe(wav: Path) -> list[tuple[float, str]]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise PanoptoError("faster-whisper not installed: pip install faster-whisper") from e
    import wave
    import numpy as np
    # decode the 16 kHz mono WAV ourselves: newer PyAV breaks faster-whisper's decoder
    with wave.open(str(wav), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != 16000:
            raise PanoptoError("unexpected audio format for Whisper")
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    # CPU by default: "auto" picks CUDA whenever the DLLs look present and then crashes without an NVIDIA GPU
    model = WhisperModel(WHISPER_MODEL, device=os.getenv("WHISPER_DEVICE", "cpu"), compute_type="int8")
    segments, _ = model.transcribe(audio, vad_filter=True, beam_size=1)
    return [(s.start, s.text.strip()) for s in segments if s.text.strip()]



def _stream_call(client, **kw):
    """Long note-writing calls must stream (the SDK refuses >10 minute non-streaming requests)."""
    with client.messages.stream(**kw) as st:
        return st.get_final_message()

# ---------------------------------------------------------------- notes (Claude)


def notes_system() -> str:
    return P.NOTES_SYSTEM.replace("__STUDENT__", STUDENT)


def assessment(code: str | None) -> dict:
    """How a module is assessed (config.json modules.<code>.assessment)."""
    a = (CONFIG.get("modules", {}).get(code or "", {}) or {}).get("assessment") or {}
    return {"exam": a.get("exam", True), "coursework": a.get("coursework", False),
            "weekly_tutorials": a.get("weekly_tutorials", False)}


def assessment_text(code: str | None) -> str:
    a = assessment(code)
    parts = [x for x, on in (("exam", a["exam"]), ("coursework", a["coursework"]),
                             ("weekly tutorial sheets", a["weekly_tutorials"])) if on]
    return ", ".join(parts) or "unknown"


def assessment_instructions(code: str | None) -> str:
    return P.assessment_instructions(assessment(code))


def generate_notes(meta: dict, transcript: str, slides: str, material: dict | None = None,
                   questions: list[dict] | None = None, extras: dict | None = None) -> dict:
    extras = extras or {}
    profile = materials.module_profile(meta.get("module_code"))
    import anthropic

    prompt, tut, cw = P.notes_request(meta, transcript, slides, material, questions, extras, profile, CONFIG,
                                      assessment_text, assessment_instructions(meta.get("module_code")))
    client = anthropic.Anthropic()
    system = notes_system() + P.TOOL_ONCE
    # The lecture prompt is large; cache it so the second (notes body) call is cheap.
    user_block = {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}
    notes = _structured_notes(client, system, user_block)
    if not material:
        notes.pop("material_comparison", None)
    if not questions:
        notes.pop("exam_links", None)
    if not tut:
        notes.pop("tutorial_guidance", None)
    if not cw and not assessment(meta.get("module_code"))["coursework"]:
        notes.pop("coursework_links", None)
        notes.pop("coursework_actions", None)
    notes["notes_markdown"] = _notes_body(client, user_block)
    return notes


# The structured fields and the written notes used to come back in one tool call. With scanned worked
# examples in the materials that response outgrew the output limit, the notes body (last field) was
# cut off, and blank notes were saved. Now: structured fields in one call, the body as plain Markdown
# in a second call that continues if it hits the limit.

def structured_tool() -> dict:
    t = P.NOTES_TOOL
    return {
        **t,
        "input_schema": {
            **t["input_schema"],
            "properties": {k: v for k, v in t["input_schema"]["properties"].items() if k != "notes_markdown"},
            "required": [k for k in t["input_schema"].get("required", []) if k != "notes_markdown"],
        },
    }



def _structured_notes(client, system: str, user_block: dict) -> dict:
    messages = [{"role": "user", "content": [user_block, {"type": "text", "text":
                 P.STRUCTURED_CALL}]}]
    for attempt in range(2):
        resp = _stream_call(client, model=NOTES_MODEL, max_tokens=32000, system=system,
                            tools=[structured_tool()], tool_choice={"type": "auto"}, messages=messages)
        for block in resp.content:
            if block.type == "tool_use" and block.name == "file_lecture_notes":
                if resp.stop_reason == "max_tokens" and attempt == 0:
                    log("structured notes hit the output limit; retrying more concisely")
                    break
                return normalise_notes(dict(block.input))
        else:
            messages = messages + [{"role": "assistant", "content": resp.content},
                                   {"role": "user", "content": P.STRUCTURED_NUDGE}]
            continue
        messages = [{"role": "user", "content": [user_block, {"type": "text", "text":
                     P.STRUCTURED_CONCISE}]}]
    raise RuntimeError("Claude did not return notes")


def _notes_body(client, user_block: dict) -> str:
    messages = [{"role": "user", "content": [user_block, {"type": "text", "text": P.BODY_INSTRUCTION}]}]
    parts = []
    for _ in range(3):  # first pass plus up to two continuations
        resp = _stream_call(client, model=NOTES_MODEL, max_tokens=32000, system=notes_system(), messages=messages)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        parts.append(text)
        if resp.stop_reason != "max_tokens":
            break
        messages = messages + [{"role": "assistant", "content": text},
                               {"role": "user", "content": P.BODY_CONTINUE}]
    body = "".join(parts).strip()
    if len(body) < 1500:
        raise RuntimeError(f"notes body came back too short ({len(body)} chars)")
    return body



ITEM_KEYS = {
    "key_concepts": "name", "equations": "latex", "definitions": "term", "worked_examples": "problem",
    "exam_signals": "point", "review_questions": "question", "exam_links": "question_ref",
    "tutorial_guidance": "question_ref", "coursework_links": "brief", "formula_sheet": "latex", "flashcards": "front",
}
COMPARISON_KEYS = {"covered": "topic", "lecture_only": "point", "material_only": "topic", "discrepancies": "issue"}


def _loads(v):
    if isinstance(v, str) and v.strip()[:1] in "[{":
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def _items(v, key):
    v = _loads(v)
    if v is None:
        return []
    if isinstance(v, dict):
        v = [v]
    if not isinstance(v, list):
        v = [v]
    out = []
    for x in v:
        x = _loads(x)
        out.append(x if isinstance(x, dict) else {key: str(x)})
    return out


def normalise_notes(notes: dict) -> dict:
    """Without a forced tool call the model sometimes sends arrays as JSON strings or plain strings."""
    for f, key in ITEM_KEYS.items():
        if f in notes:
            notes[f] = _items(notes[f], key)
    for f in ("admin", "gaps", "prerequisites", "builds_on", "common_mistakes", "coursework_actions"):
        if f in notes:
            v = _loads(notes[f])
            notes[f] = [str(x) for x in (v if isinstance(v, list) else [v] if v else [])]
    if "material_comparison" in notes:
        comp = _loads(notes["material_comparison"])
        if isinstance(comp, dict):
            for f, key in COMPARISON_KEYS.items():
                comp[f] = _items(comp.get(f), key)
            try:
                comp["coverage_pct"] = float(comp.get("coverage_pct") or 0)
            except (TypeError, ValueError):
                comp["coverage_pct"] = 0.0
            notes["material_comparison"] = comp
        else:
            notes.pop("material_comparison")
    for f in ("title", "topic", "summary", "notes_markdown"):
        if f in notes and not isinstance(notes[f], str):
            notes[f] = str(notes[f])
    notes.setdefault("title", "Lecture notes")
    notes.setdefault("summary", "")
    notes.setdefault("notes_markdown", "")
    return notes

# ---------------------------------------------------------------- Vault

def _iso_date(v) -> str | None:
    """'2026-12-04', '04/12/2026', '4th December 2026', 'Friday 4 Dec 2026' -> ISO, else None."""
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


class VaultDB:
    """Direct PostgREST access to the Vault Supabase project."""

    def __init__(self):
        self.url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
        key = os.environ["SUPABASE_SERVICE_KEY"]
        self.user_id = os.getenv("VAULT_USER_ID") or "11111111-1111-1111-1111-111111111111"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.http = httpx.Client(timeout=60)

    @staticmethod
    def configured() -> bool:
        return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"))

    def note_exists(self, delivery_id: str) -> bool:
        r = self.http.get(f"{self.url}/academic_notes", headers=self.headers,
                          params={"panopto_delivery_id": f"eq.{delivery_id}", "select": "id"})
        r.raise_for_status()
        return bool(r.json())

    def upsert_note(self, row: dict) -> dict:
        if self.user_id:
            row["user_id"] = self.user_id
        r = self.http.post(
            f"{self.url}/academic_notes", params={"on_conflict": "panopto_delivery_id"},
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=representation"},
            json=row,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"academic_notes insert failed ({r.status_code}): {r.text}")
        return r.json()[0]

    def insert_memory(self, entity: dict) -> None:
        """Same row shape as the Vault MCP's vault_write, so Claude chats see it like any other memory."""
        now = datetime.now(timezone.utc).isoformat()
        row = {"user_id": self.user_id, "confidence": 1.0, "is_core": False, "is_sensitive": False,
               "is_active": True, "version": 1, "created_at": now, "updated_at": now, **entity}
        r = self.http.post(f"{self.url}/memory_entities", headers={**self.headers, "Prefer": "return=minimal"},
                           json=row)
        if r.status_code >= 400:
            raise RuntimeError(f"memory_entities insert failed ({r.status_code}): {r.text}")

    # ---- academic engine tables (scoring / progress)
    def _post(self, table: str, row: dict, prefer: str = "return=representation", params: dict | None = None):
        r = self.http.post(f"{self.url}/{table}", params=params or {}, headers={**self.headers, "Prefer": prefer}, json=row)
        if r.status_code >= 400:
            raise RuntimeError(f"{table} write failed ({r.status_code}): {r.text[:300]}")
        return r.json() if "representation" in prefer and r.text else None

    def _patch(self, table: str, match: dict, row: dict):
        params = {k: f"eq.{v}" for k, v in match.items()}
        r = self.http.patch(f"{self.url}/{table}", params=params, headers={**self.headers, "Prefer": "return=representation"}, json=row)
        if r.status_code >= 400:
            raise RuntimeError(f"{table} update failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    def _get(self, table: str, params: dict):
        r = self.http.get(f"{self.url}/{table}", headers=self.headers, params=params)
        r.raise_for_status()
        return r.json()

    def sync_work(self, w: dict, week_of_due: int | None = None) -> str:
        """Mirror a local work row into academic_work (keyed on file_ref). Returns the vault id."""
        marks = w.get("question_marks") or {}
        attempted = sum(1 for m in marks.values() if m.get("result") != "skipped")
        correct = sum(1 for m in marks.values() if m.get("result") == "correct")
        errors = w.get("errors") or []
        tags = {}
        for e in errors:
            tags[e.get("tag") or "untagged"] = tags.get(e.get("tag") or "untagged", 0) + 1
        row = {
            "user_id": self.user_id, "module_code": w["module_code"],
            "work_type": {"tutorial": "problem_sheet", "past_paper": "exam", "coursework": "coursework"}.get(w["work_type"], "problem_sheet"),
            "title": w["title"], "week_number": w.get("week") or week_of_due or 0, "set_date": w.get("set_date"),
            "due_date": _iso_date(w.get("due_date")),
            "completed_date": w.get("completed_date"), "status": w.get("status"),
            "total_questions": w.get("total_questions"), "questions_attempted": attempted, "questions_correct": correct,
            "score_pct": w.get("score_pct"), "errors": errors, "question_breakdown": marks,
            "topic_breakdown": tags, "coursework_milestones": w.get("milestones") or [],
            "time_spent_minutes": w.get("time_minutes"), "difficulty_rating": w.get("difficulty"),
            "feedback_score": w.get("feedback_score"), "feedback_notes": w.get("feedback_notes"),
            "file_ref": w["file"], "red_alerts": [f"Q{e['question']}: {e.get('tag')}" for e in errors if e.get("tag") in ("concept", "method")][:10],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        row = {k: v for k, v in row.items() if v is not None}
        existing = self._get("academic_work", {"user_id": f"eq.{self.user_id}", "file_ref": f"eq.{w['file']}", "select": "id"})
        if existing:
            self._patch("academic_work", {"id": existing[0]["id"]}, row)
            return existing[0]["id"]
        return self._post("academic_work", row)[0]["id"]

    def sync_priority(self, p: dict, week: int, contact_hours: float | None = None) -> None:
        row = {"user_id": self.user_id, "module_code": p["module_code"], "week_number": week,
               "current_average": p.get("mastery"), "notes_currency": p.get("coverage"),
               "problem_sheet_completion": (p.get("sheet_completion") or 0) * 100 if p.get("sheet_completion") is not None else None,
               "error_density": p.get("error_density"), "upcoming_deadline_days": p.get("deadline_days"),
               "contact_hours_weekly": contact_hours, "priority_score": p["score"], "recommended_hours": p["hours"],
               "priority_rank": p["rank"], "reasoning": p["reasoning"]}
        row = {k: v for k, v in row.items() if v is not None}
        existing = self._get("academic_priority", {"user_id": f"eq.{self.user_id}", "module_code": f"eq.{p['module_code']}",
                                                   "week_number": f"eq.{week}", "select": "id"})
        if existing:
            self._patch("academic_priority", {"id": existing[0]["id"]}, row)
        else:
            self._post("academic_priority", row, prefer="return=minimal")

    def rate_note(self, delivery_id: str, rating: int | None = None, status: str | None = None,
                  questions: list[str] | None = None, minutes: int | None = None) -> None:
        row = {}
        if rating is not None:
            row["understanding_rating"] = rating
        if status:
            row["notes_status"] = status
            if status == "reviewed":
                row["notes_completed_date"] = date.today().isoformat()
        if questions is not None:
            row["questions_for_tutorial"] = questions
        if minutes is not None:
            row["time_spent_minutes"] = minutes
        if row:
            self._patch("academic_notes", {"panopto_delivery_id": delivery_id}, row)

    def ensure_goals(self, modules: dict) -> None:
        """Two standing academic goals, created once, progress updated weekly."""
        have = {g["goal"]: g for g in self._get("memory_goals", {"user_id": f"eq.{self.user_id}", "domain": "eq.academic", "select": "id,goal"})}
        wanted = [
            ("Coursework: first-class marks on every report", "Average coursework milestone progress then mark", 70.0, "%"),
            ("Exams: walk in with every module at 75%+ mastery", "Mean exam-module mastery", 75.0, "%"),
        ]
        for goal, metric, target, unit in wanted:
            if goal not in have:
                self._post("memory_goals", {"user_id": self.user_id, "goal": goal, "domain": "academic", "target_metric": metric,
                                            "target_value": target, "current_value": 0, "unit": unit, "status": "active",
                                            "target_date": "2027-06-30", "progress_history": []}, prefer="return=minimal")

    def update_goals(self, cw_progress: float | None, exam_mastery: float | None) -> None:
        goals = self._get("memory_goals", {"user_id": f"eq.{self.user_id}", "domain": "eq.academic", "select": "id,goal,progress_history"})
        today = date.today().isoformat()
        for g in goals:
            val = cw_progress if "coursework" in g["goal"] else exam_mastery if "exams" in g["goal"] else None
            if val is None:
                continue
            hist = [h for h in (g.get("progress_history") or []) if h.get("date") != today] + [{"date": today, "value": round(val, 1)}]
            self._patch("memory_goals", {"id": g["id"]}, {"current_value": round(val, 1), "progress_history": hist[-60:],
                                                           "updated_at": datetime.now(timezone.utc).isoformat()})

    # lecture materials + question examples, searchable by the module chats
    def replace_resource(self, module_code: str, kind: str, file: str, file_hash: str, chunks: list[dict]) -> None:
        self.delete_resource(file)
        rows = [{"user_id": self.user_id, "module_code": module_code, "kind": kind, "file": file,
                 "file_hash": file_hash, "chunk_index": i, **c} for i, c in enumerate(chunks)]
        for i in range(0, len(rows), 50):
            r = self.http.post(f"{self.url}/academic_resources", headers={**self.headers, "Prefer": "return=minimal"},
                               json=rows[i:i + 50])
            if r.status_code >= 400:
                raise RuntimeError(f"academic_resources upload failed ({r.status_code}): {r.text}")

    def delete_resource(self, file: str) -> None:
        r = self.http.delete(f"{self.url}/academic_resources", headers=self.headers,
                             params={"user_id": f"eq.{self.user_id}", "file": f"eq.{file}"})
        if r.status_code >= 400:
            raise RuntimeError(f"academic_resources delete failed ({r.status_code}): {r.text}")


def log_to_memory(db: VaultDB | None, notes: dict, meta: dict, note_id) -> str:
    code = notes.get("module_code") or meta.get("module_code") or "unassigned"
    module_name = CONFIG.get("modules", {}).get(code, {}).get("name", code)
    concepts = ", ".join(c["name"] for c in notes.get("key_concepts", [])[:8])
    exam = "; ".join(e["point"] for e in notes.get("exam_signals", [])[:5])
    comp = notes.get("material_comparison") or {}
    self_study = "; ".join(m["topic"] for m in comp.get("material_only", [])[:5])
    gaps = notes.get("gaps") or []
    content = (
        f"Lecture: {notes['title']} ({code} {module_name}, {meta.get('date')}).\n"
        f"{notes['summary']}\n"
        f"Key concepts: {concepts}.\n"
        + (f"Exam signals: {exam}.\n" if exam else "")
        + (f"Self-study (in materials, not covered in lecture): {self_study}.\n" if self_study else "")
        + (f"Admin: {'; '.join(notes.get('admin', []))}.\n" if notes.get("admin") else "")
        + f"Full notes: academic_notes {note_id}. Recording: {viewer_url(meta['delivery_id'])}"
    )
    actions = [f"Check: {g}" for g in gaps[:2]] + ([f"Self-study: {self_study}"] if self_study else [])
    entity = {
        "content": content,
        "summary": f"{code} lecture: {notes['title']}",
        "entity_type": "fact",
        "domain": "academic",
        "subdomain": code,
        "project": module_name,
        "source": "panopto",
        "tags": ["lecture", "panopto", code, notes.get("difficulty", "applied")],
        "is_actionable": bool(actions),
    }
    if actions:
        entity["next_action"] = " | ".join(actions)[:500]
    if db is None:
        raise RuntimeError("No Vault connection configured")
    db.insert_memory(entity)
    return "memory_entities"


# ---------------------------------------------------------------- pipeline

NOTES_VERSION = 3


def markdown_body(notes: dict, meta: dict) -> str:
    """Full notes: the written notes plus the structured sections, timestamps linked."""
    body = linkify_timestamps(notes["notes_markdown"], meta["delivery_id"])
    extra = ""
    if notes.get("tutorial_guidance"):
        extra += "\n\n## Tutorial sheet guidance\n"
        for t in notes["tutorial_guidance"]:
            extra += f"\n### {t.get('question_ref', 'Question')}\n\n{t.get('approach', '')}\n"
            if t.get("key_equations"):
                extra += f"\n**Equations:** {t['key_equations']}\n"
            if t.get("pitfalls"):
                extra += f"\n**Watch out:** {t['pitfalls']}\n"
            if t.get("answer_check"):
                extra += f"\n<details><summary>Answer check</summary>\n\n{t['answer_check']}\n</details>\n"
    if notes.get("coursework_links"):
        extra += "\n\n## Coursework links\n\n" + "\n".join(
            f"* **{c.get('brief', 'Coursework')}**: {c.get('relevance', '')}"
            + (f" *How to use it:* {c['how_to_use']}" if c.get("how_to_use") else "")
            for c in notes["coursework_links"]) + "\n"
    if notes.get("coursework_actions"):
        extra += "\n\n## Coursework to-do\n\n" + "\n".join(f"- [ ] {a}" for a in notes["coursework_actions"]) + "\n"
    if notes.get("builds_on"):
        extra += "\n\n## Builds on\n\n" + "\n".join(f"* {b}" for b in notes["builds_on"]) + "\n"
    comp = notes.get("material_comparison")
    if comp:
        def ts(x):
            return f" [{x['timestamp']}]" if x.get("timestamp") else ""

        def ref(x):
            return f" ({x['material_ref']})" if x.get("material_ref") else ""

        extra += f"\n\n## Lecture vs materials\n\nCoverage of matched material: about {round(comp.get('coverage_pct', 0))}%\n"
        if comp.get("lecture_only"):
            extra += "\n### Said in lecture, not in materials\n\n" + "\n".join(
                f"* {x['point']}{ts(x)}" for x in comp["lecture_only"]) + "\n"
        if comp.get("material_only"):
            extra += "\n### In materials, not covered (self-study)\n\n" + "\n".join(
                f"* {x['topic']}{ref(x)}" for x in comp["material_only"]) + "\n"
        if comp.get("discrepancies"):
            extra += "\n### Discrepancies\n\n" + "\n".join(
                f"* {x['issue']}" + (f" (lecture: {x['lecture']}; materials: {x['material']})"
                                     if x.get("lecture") or x.get("material") else "")
                for x in comp["discrepancies"]) + "\n"
        extra = linkify_timestamps(extra, meta["delivery_id"])
    if notes.get("exam_links"):
        extra += "\n\n## Linked exam questions\n\n" + "\n".join(
            f"* **{x['question_ref']}**: {x.get('relation', '')}" for x in notes["exam_links"]) + "\n"
    if notes.get("review_questions"):
        extra += "\n\n## Review questions\n\n" + "\n".join(
            f"**Q{i}.** {q['question']}\n<details><summary>Answer</summary>\n\n{q.get('answer', '')}\n</details>\n"
            for i, q in enumerate(notes["review_questions"], 1))
    if notes.get("gaps"):
        extra += "\n\n## Check against slides\n\n" + "\n".join(f"* {g}" for g in notes["gaps"])
    if notes.get("flashcards"):
        extra += "\n\n## Flashcards\n\n" + "\n".join(
            f"* {c.get('front', '')}  \n  <details><summary>Answer</summary>{c.get('back', '')}</details>"
            for c in notes["flashcards"]) + "\n"
    return body + extra


def save_markdown(notes: dict, meta: dict, material: dict | None = None) -> Path:
    code = notes.get("module_code") or meta.get("module_code") or "unassigned"
    folder = NOTES_DIR / code
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{meta.get('date') or 'undated'} {slugify(notes['title'])}.md"
    mat_line = ""
    if material:
        rng = f" {material['unit']}.{material['pages'][0]}-{material['pages'][1]}" if material.get("pages") else ""
        mat_line = f"materials: \"{material['file']}{rng}\"\n"
    front = (
        "---\n"
        f"title: \"{notes['title']}\"\nmodule: {code}\ndate: {meta.get('date')}\n"
        f"difficulty: {notes.get('difficulty')}\nrecording: {viewer_url(meta['delivery_id'])}\n"
        f"{mat_line}transcript_source: {meta['transcript_source']}\n---\n\n"
    )
    path.write_text(front + markdown_body(notes, meta), encoding="utf-8")
    return path


def process(pan: Panopto, delivery_id: str, db: VaultDB | None = None, listing: dict | None = None,
            allow_defer: bool = False, progress: Callable[[str], None] | None = None,
            material_override: dict | None = None, module_hint: str | None = None,
            claimed: set[str] | None = None, lecture_context: dict | None = None) -> dict:
    """Run the full pipeline for one lecture. Returns {"status": "done"|"deferred", ...}."""
    say = progress or log

    say("Fetching lecture info")
    info = pan.delivery_info(delivery_id)
    d = info.get("Delivery") or {}
    listing = listing or {}
    name = d.get("SessionName") or listing.get("name") or "Untitled lecture"
    folder = d.get("SessionGroupLongName") or listing.get("folder")
    start = parse_panopto_date(listing.get("start")) or parse_panopto_date(
        d.get("SessionStartTime") or info.get("SessionStartTime") or d.get("StartTime"))
    meta = {
        "delivery_id": delivery_id,
        "session_name": name,
        "folder": folder,
        "date": (start.date() if start else date.today()).isoformat(),
        "duration_min": round(d["Duration"] / 60) if d.get("Duration") else None,
        "module_code": module_hint or detect_module(name, folder or ""),
        "week": (lecture_context or {}).get("week"),
        "ordinal": (lecture_context or {}).get("ordinal"),
    }
    slides = "\n".join(
        f"[{fmt_ts(t.get('Time', 0))}] {t.get('Caption', '').strip()}"
        for t in d.get("Timestamps") or [] if (t.get("Caption") or "").strip()
    )

    say("Fetching captions")
    segments = pan.captions(delivery_id)
    meta["transcript_source"] = "panopto captions"
    if not segments:
        if allow_defer and start and datetime.now(timezone.utc) - start < timedelta(hours=CAPTION_GRACE_HOURS):
            return {"status": "deferred", "meta": meta}
        streams = (d.get("PodcastStreams") or []) + (d.get("Streams") or [])
        stream_url = next((s.get("StreamUrl") or s.get("StreamHttpUrl") for s in streams
                           if s.get("StreamUrl") or s.get("StreamHttpUrl")), None)
        if not stream_url:
            raise PanoptoError("No captions and no downloadable stream")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            say("No captions: downloading audio")
            pan.download_audio(stream_url, wav)
            say(f"Transcribing locally with Whisper ({WHISPER_MODEL})")
            segments = whisper_transcribe(wav)
        meta["transcript_source"] = f"whisper {WHISPER_MODEL}"
    transcript = segments_to_transcript(segments)

    material = None
    if meta["module_code"] or (material_override and material_override.get("file")):
        say("Matching lecture materials")
        try:
            mode = CONFIG.get("modules", {}).get(meta["module_code"] or "", {}).get("materials_mode")
            material = materials.match_materials(
                meta["module_code"], {"session_name": name, "date": meta["date"], "week": meta["week"],
                                      "ordinal": meta["ordinal"]}, transcript, slides,
                claimed=claimed, override=material_override, mode=mode)
        except Exception as e:
            log(f"Materials matching failed: {e}")

    questions = []
    try:
        questions = materials.relevant_questions(meta["module_code"], transcript, week=meta["week"])
    except Exception as e:
        log(f"Question matching failed: {e}")
    extras = {"previous": (lecture_context or {}).get("previous") or []}
    try:
        extras["tutorials"] = materials.tutorial_context(meta["module_code"], transcript, meta["week"], meta["ordinal"])
        asm = assessment(meta["module_code"])
        cw_only = asm["coursework"] and not asm["exam"]
        if asm["coursework"]:
            extras["coursework"] = materials.coursework_context(meta["module_code"], transcript,
                                                                limit_chars=22000 if cw_only else 10000)
            if cw_only:
                extras["exemplars"] = materials.exemplar_context(meta["module_code"])
        if not asm["weekly_tutorials"] and not asm["exam"]:
            extras["tutorials"] = []
    except Exception as e:
        log(f"Tutorial/coursework matching failed: {e}")

    parts = [p for p, ok in (("materials", material), ("tutorial sheets", extras.get("tutorials")),
                             ("coursework", extras.get("coursework"))) if ok]
    say("Writing notes with Claude" + (f" (with {', '.join(parts)})" if parts else ""))
    notes = generate_notes(meta, transcript, slides, material, questions, extras)
    md_path = save_markdown(notes, meta, material)
    public_material = {k: v for k, v in material.items() if k != "text"} if material else None
    result = {"status": "done", "notes": notes, "meta": meta, "md_path": str(md_path),
              "material": public_material, "vault": None, "notes_version": NOTES_VERSION,
              "tutorials": [t["name"] for t in extras.get("tutorials") or []],
              "coursework": [c["name"] for c in extras.get("coursework") or []]}

    if db is not None:
        say("Filing to the Vault")
        week = meta.get("week")
        if week is None and CONFIG.get("semester_start"):
            delta = (date.fromisoformat(meta["date"]) - date.fromisoformat(CONFIG["semester_start"])).days
            week = delta // 7 + 1 if delta >= 0 else 0
        row = {
            # columns the academic engine already had (required there)
            "week_number": week or 0,
            "lecture_topic": (notes.get("topic") or notes["title"])[:300],
            "notes_location": viewer_url(delivery_id),
            "panopto_delivery_id": delivery_id,
            "panopto_url": viewer_url(delivery_id),
            "source": "panopto",
            "module_code": notes.get("module_code") or meta["module_code"],
            "title": notes["title"],
            "topic": notes.get("topic"),
            "lecture_date": meta["date"],
            "duration_minutes": meta["duration_min"],
            "difficulty": notes.get("difficulty"),
            "summary": notes["summary"],
            "notes_markdown": markdown_body(notes, meta),
            "key_concepts": notes.get("key_concepts", []),
            "equations": notes.get("equations", []),
            "definitions": notes.get("definitions", []),
            "worked_examples": notes.get("worked_examples", []),
            "exam_signals": notes.get("exam_signals", []),
            "admin": notes.get("admin", []) + [
                f"Coursework ({c.get('brief', '')}): {c.get('relevance', '')} {c.get('how_to_use', '')}".strip()
                for c in notes.get("coursework_links", [])] + [
                f"Coursework to-do: {a}" for a in notes.get("coursework_actions", [])],
            "review_questions": notes.get("review_questions", []) + [
                {"question": c.get("front", ""), "answer": c.get("back", ""), "type": "flashcard"}
                for c in notes.get("flashcards", [])],
            "gaps": notes.get("gaps", []),
            "material_files": public_material,
            "material_comparison": notes.get("material_comparison"),
            "exam_links": notes.get("exam_links", []) + [
                {"question_ref": t.get("question_ref", ""), "relation": t.get("approach", ""), "type": "tutorial",
                 "key_equations": t.get("key_equations"), "answer_check": t.get("answer_check")}
                for t in notes.get("tutorial_guidance", [])],
            "transcript": transcript,
            "transcript_source": meta["transcript_source"],
        }
        saved = db.upsert_note(row)
        say("Logging to memory")
        result["vault"] = log_to_memory(db, notes, meta, saved.get("id"))
    return result


# ---------------------------------------------------------------- CLI

def run_batch(items: list[tuple[str, dict | None]], args, allow_defer=False) -> int:
    state = load_json(STATE_FILE, {"done": {}})
    db = None if args.local_only else VaultDB()
    pan = Panopto(headless=not args.show_browser)
    failures = 0
    try:
        if not pan.is_authenticated():
            log("Panopto session expired. Run: python panopto_vault.py login")
            return 2
        for delivery_id, listing in items:
            try:
                if not args.force and (delivery_id in state["done"] or (db and db.note_exists(delivery_id))):
                    state["done"].setdefault(delivery_id, "already in Vault")
                    log(f"-> {delivery_id}: skipped (already ingested)")
                    continue
                res = process(pan, delivery_id, db, listing, allow_defer=allow_defer)
                if res["status"] == "deferred":
                    log("-> deferred (captions not ready yet)")
                    continue
                state["done"][delivery_id] = datetime.now().isoformat(timespec="seconds")
                log(f"-> {res['notes']['title']}: saved {res['md_path']}"
                    + (f", logged via {res['vault']}" if res["vault"] else ""))
            except Exception as e:
                failures += 1
                log(f"!! {delivery_id}: {e}")
            STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    finally:
        pan.close()
    return 1 if failures else 0


def sessions_from_folders(folders: list[tuple[str | None, str | None]], since_days: int) -> list[tuple[str, dict]]:
    pan = Panopto(headless=True)
    try:
        if not pan.is_authenticated():
            raise SystemExit("Panopto session expired. Run: python panopto_vault.py login")
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        items = []
        for fid, _module in folders:
            for s in pan.folder_sessions(fid):
                if s["start"] is None or s["start"] >= cutoff:
                    items.append((s["delivery_id"], s))
        return items
    finally:
        pan.close()


def main():
    p = argparse.ArgumentParser(description="Panopto lectures -> notes -> The Vault")
    p.add_argument("--local-only", action="store_true", help="save Markdown notes only, skip Supabase")
    p.add_argument("--force", action="store_true", help="re-process even if already ingested")
    p.add_argument("--show-browser", action="store_true", help="run the browser visibly")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    pi = sub.add_parser("ingest")
    pi.add_argument("urls", nargs="+", help="Panopto viewer URLs or delivery IDs")
    pf = sub.add_parser("folder")
    pf.add_argument("folder", help="Panopto folder URL or folder ID")
    pf.add_argument("--since", type=int, default=365, help="only lectures from the last N days")
    pw = sub.add_parser("watch")
    pw.add_argument("--since", type=int, default=14, help="look back N days")
    args = p.parse_args()

    if args.cmd == "login":
        pan = Panopto(headless=False)
        try:
            pan.login()
        finally:
            pan.close()
        return 0

    if args.cmd == "ingest":
        ids = []
        for u in args.urls:
            m = GUID_RE.search(u)
            if not m:
                log(f"No lecture ID found in {u}")
                continue
            ids.append((m.group(0), None))
        return run_batch(ids, args)

    if args.cmd == "folder":
        m = GUID_RE.search(args.folder.replace("%22", ""))
        if not m:
            raise SystemExit("Couldn't find a folder ID in that URL")
        items = sessions_from_folders([(m.group(0), None)], args.since)
        log(f"{len(items)} lectures in folder")
        return run_batch(items, args)

    if args.cmd == "watch":
        folders = watch_folders()
        if not folders:
            raise SystemExit("Add Panopto folder IDs to watch_folders in config.json")
        items = sessions_from_folders(folders, args.since)
        log(f"{len(items)} recent lectures across {len(folders)} folders")
        return run_batch(items, args, allow_defer=True)


if __name__ == "__main__":
    sys.exit(main())
