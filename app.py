#!/usr/bin/env python3
"""
Lecture Vault: local web app for the Panopto -> notes -> Vault pipeline.

  python app.py               start and open http://127.0.0.1:8765
  python app.py --no-browser  start in the background (used at Windows login)

One worker thread owns the Playwright browser and runs jobs in order:
sync (list Panopto sessions), process (transcribe + notes + Vault) and login.
A scheduler queues a sync every AUTO_SYNC_MINUTES; with auto transcribe on,
new recordings are processed as soon as Panopto lists them.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import re
import traceback
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HEARTBEAT = [time.time()]  # last time the running job reported progress (see watchdog)

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import materials as mat
import moodle as md
import promptlib
import qbank
import study
import engine
import panopto_vault as pv
import timetable as tt

ROOT = pv.ROOT
PORT = int(os.getenv("APP_PORT", "8765"))
AUTO_SYNC_MINUTES = float(os.getenv("AUTO_SYNC_MINUTES", "20"))
AUTO_SINCE_DAYS = int(os.getenv("AUTO_SINCE_DAYS", "14"))
AUTO_MAX_ATTEMPTS = 3
MOODLE_SYNC_HOURS = float(os.getenv("MOODLE_SYNC_HOURS", "6"))
AUTH_RECHECK_SECONDS = 600
PALETTE = ["#7c9cff", "#3ddc97", "#f5b544", "#ff7aa8", "#5ad1e6", "#b18cff", "#ff8a5c", "#c6e05a"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def brief(e) -> str:
    """One readable line for the UI: Playwright errors carry full request logs including cookies."""
    msg = str(e).strip()
    for marker in ("\nCall log:", "Call log:", "\n- "):
        if marker in msg:
            msg = msg.split(marker, 1)[0].strip()
    return msg.splitlines()[0][:300] if msg else type(e).__name__


TOAST_PS = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$x = $t.GetElementsByTagName('text');"
    "$x.Item(0).AppendChild($t.CreateTextNode($env:LV_TITLE)) > $null;"
    "$x.Item(1).AppendChild($t.CreateTextNode($env:LV_BODY)) > $null;"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
)


def notify(title: str, body: str) -> None:
    """Windows toast. Text goes through env vars, never into the script itself."""
    if not sys.platform.startswith("win") or os.getenv("NOTIFY", "1") != "1":
        return
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", TOAST_PS],
                         env={**os.environ, "LV_TITLE": title[:120], "LV_BODY": body[:240]},
                         creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception:
        pass


# ---------------------------------------------------------------- store

class Store:
    SCHEMA = """
    create table if not exists lectures (
      delivery_id text primary key, name text, folder text, module_code text,
      start_utc text, duration_min integer, status text default 'new', step text, error text,
      notes_path text, title text, summary text, difficulty text, materials text, comparison text,
      materials_override text, exam_links text, transcript_source text, vault text, attempts integer default 0,
      notes_version integer default 1, tutorials text, coursework text, understanding integer,
      vault_checked integer default 0, updated_at text
    );
    create table if not exists meta (key text primary key, value text);
    """
    JSON_COLS = {"materials", "comparison", "materials_override", "exam_links", "tutorials", "coursework"}
    ADDED_COLS = {"exam_links": "text", "notes_version": "integer default 1", "tutorials": "text",
                  "coursework": "text", "understanding": "integer"}  # columns added after the first release

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(self.SCHEMA)
            # jobs don't survive a restart: anything left mid-queue goes back to waiting
            self.conn.execute("update lectures set status=case when notes_path is not null then 'done' else 'new' end, "
                              "step=null where status in ('queued', 'processing')")
            have = {r[1] for r in self.conn.execute("pragma table_info(lectures)")}
            for col, typ in self.ADDED_COLS.items():
                if col not in have:
                    self.conn.execute(f"alter table lectures add column {col} {typ}")
            self.conn.commit()
            study.init(self.conn)
            qbank.init(self.conn)
            engine.init(self.conn)

    def _row(self, r) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for c in self.JSON_COLS:
            if d.get(c):
                try:
                    d[c] = json.loads(d[c])
                except json.JSONDecodeError:
                    d[c] = None
        return d

    def get(self, did: str) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute("select * from lectures where delivery_id=?", (did,)).fetchone())

    def all(self) -> list[dict]:
        with self.lock:
            return [self._row(r) for r in self.conn.execute("select * from lectures order by start_utc")]

    def upsert_session(self, s: dict, module_code: str | None) -> bool:
        start = s["start"].astimezone(timezone.utc).isoformat() if s.get("start") else None
        dur = round(s["duration"] / 60) if s.get("duration") else None
        with self.lock:
            exists = self.conn.execute("select 1 from lectures where delivery_id=?", (s["delivery_id"],)).fetchone()
            if exists:
                self.conn.execute(
                    "update lectures set name=?, folder=?, start_utc=coalesce(?, start_utc), "
                    "duration_min=coalesce(?, duration_min), module_code=coalesce(module_code, ?) where delivery_id=?",
                    (s.get("name"), s.get("folder"), start, dur, module_code, s["delivery_id"]))
            else:
                self.conn.execute(
                    "insert into lectures (delivery_id, name, folder, module_code, start_utc, duration_min, status, updated_at) "
                    "values (?,?,?,?,?,?, 'new', ?)",
                    (s["delivery_id"], s.get("name"), s.get("folder"), module_code, start, dur, now_utc().isoformat()))
            self.conn.commit()
            return not exists

    def update(self, did: str, **fields) -> None:
        if not fields:
            return
        if "step" in fields or "status" in fields:
            HEARTBEAT[0] = time.time()
        fields["updated_at"] = now_utc().isoformat()
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(json.dumps(v) if k in self.JSON_COLS and v is not None else v)
        with self.lock:
            self.conn.execute(f"update lectures set {', '.join(cols)} where delivery_id=?", (*vals, did))
            self.conn.commit()

    def claimed_files(self, exclude: str) -> set[str]:
        out = set()
        for r in self.all():
            if r["delivery_id"] != exclude and r.get("materials") and r["materials"].get("mode") == "file":
                out.add(r["materials"]["file"])
        return out

    def get_meta(self, key: str, default=None):
        with self.lock:
            r = self.conn.execute("select value from meta where key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, key: str, value) -> None:
        with self.lock:
            self.conn.execute("insert into meta (key, value) values (?, ?) on conflict(key) do update set value=excluded.value",
                              (key, str(value)))
            self.conn.commit()


# ---------------------------------------------------------------- worker

class NeedLogin(Exception):
    pass


class Worker(threading.Thread):
    def __init__(self, store: Store, vault):
        super().__init__(daemon=True, name="panopto-worker")
        self.store, self.vault = store, vault
        self.q: queue.PriorityQueue = queue.PriorityQueue()  # sign-in jumps ahead of the lecture backlog
        self._seq = 0
        self.pending: set[str] = set()
        self.lock = threading.Lock()
        self.current: str | None = None
        self.current_job = None
        self.started = 0.0
        self.pan = None
        self.auth_checked = 0.0

    def enqueue(self, kind: str, key: str, **payload) -> bool:
        with self.lock:
            if key in self.pending:
                return False
            self.pending.add(key)
        if kind == "process":
            row = self.store.get(key)
            payload.setdefault("prev", row["status"] if row else "new")
            self.store.update(key, status="queued", step="In queue", error=None)
        with self.lock:
            self._seq += 1
            seq = self._seq
        self.q.put((0 if kind == "login" else 1, seq, (kind, key, payload)))
        return True

    def snapshot(self) -> dict:
        with self.lock:
            return {"pending": sorted(self.pending), "current": self.current}

    def _reset(self):
        try:
            if self.pan:
                self.pan.close()
        except Exception:
            pass
        self.pan = None

    def _panopto(self):
        if self.pan is None:
            self.pan = pv.Panopto(headless=True)
            self.auth_checked = 0.0
        if time.time() - self.auth_checked > AUTH_RECHECK_SECONDS:
            ok = self.pan.is_authenticated()
            was = self.store.get_meta("auth")
            self.store.set_meta("auth", "ok" if ok else "expired")
            if not ok:
                if was != "expired":
                    notify("Lecture Vault: sign in to Panopto",
                           "Your university login expired, so new lectures are waiting. Open Lecture Vault and press Sign in.")
                raise NeedLogin()
            self.auth_checked = time.time()
        return self.pan

    def run(self):
        while True:
            _prio, _seq, (kind, key, payload) = self.q.get()
            self.current = key
            self.current_job = (kind, key, payload)
            self.started = HEARTBEAT[0] = time.time()
            try:
                getattr(self, f"_job_{kind}")(key, **payload)
            except NeedLogin:
                if kind == "process":
                    self.store.update(key, status=payload.get("prev", "new"), step=None,
                                      error="Sign in to Panopto, then try again")
                self.store.set_meta("last_error", "Panopto session expired: sign in again")
            except Exception as e:
                traceback.print_exc()
                if kind == "process":
                    self.store.update(key, status="error", step=None, error=brief(e))
                else:
                    self.store.set_meta("last_error", f"{kind}: {brief(e)}")
                if "closed" in str(e).lower() or "browser" in str(e).lower():
                    self._reset()
            finally:
                with self.lock:
                    self.pending.discard(key)
                self.current = None

    # jobs -----------------------------------------------------------

    def _job_login(self, key: str):
        self._reset()
        self.store.set_meta("auth", "signing_in")
        pan = pv.Panopto(headless=False)
        try:
            ok = pan.wait_for_login(timeout=300)
            if ok:
                # Same Bath login usually carries straight into Moodle; if not, the user finishes it here
                self.store.set_meta("moodle_auth", "signing_in")
                mo = md.Moodle(pan.ctx)
                page = pan.ctx.pages[0] if pan.ctx.pages else pan.ctx.new_page()
                m_ok = mo.wait_for_login(page, timeout_s=180)
                self.store.set_meta("moodle_auth", "ok" if m_ok else "expired")
                if m_ok:
                    self.store.set_meta("moodle_error", "")
                pan.save_cookies()
        finally:
            pan.close()
        self.store.set_meta("auth", "ok" if ok else "expired")
        if ok:
            self.store.set_meta("last_error", "")
            self.store.set_meta("moodle_last", "")  # force a Moodle pass before the backlog
            self.enqueue("sync", "sync", auto=True)

    def _sync_resources(self):
        """Upload new or changed materials and question files to academic_resources for the module chats."""
        try:
            n = mat.transcribe_images(list(pv.CONFIG.get("modules", {})))
            if n:
                print(f"vision: transcribed {n} image pages", flush=True)
        except Exception:
            traceback.print_exc()
        if self.vault is None:
            return
        uploaded, errors = 0, []
        for code, info in pv.CONFIG.get("modules", {}).items():
            current = {}
            for path, kind in mat.module_files(code, info.get("materials_mode")):
                rel = mat.rel(path)
                h = mat.file_hash(path)
                current[rel] = h
                if self.store.get_meta(f"res:{rel}") == h:
                    continue
                try:
                    self.vault.replace_resource(code, kind, rel, h, mat.chunk_for_upload(path))
                    self.store.set_meta(f"res:{rel}", h)
                    uploaded += 1
                except Exception as e:
                    errors.append(f"{path.name}: {e}")
            previous = json.loads(self.store.get_meta(f"resfiles:{code}", "[]"))
            for rel in set(previous) - set(current):
                try:
                    self.vault.delete_resource(rel)
                    self.store.set_meta(f"res:{rel}", "")
                except Exception as e:
                    errors.append(f"{rel}: {e}")
            self.store.set_meta(f"resfiles:{code}", json.dumps(sorted(current)))
        total = sum(len(json.loads(self.store.get_meta(f"resfiles:{c}", "[]"))) for c in pv.CONFIG.get("modules", {}))
        self.store.set_meta("res_count", total)
        if errors:
            raise RuntimeError("Material upload: " + "; ".join(errors[:3]))

    def _lecture_schedule(self, code: str) -> list[dict]:
        """This module's lectures in date order (timetable first, Panopto recordings as fallback)."""
        out = []
        if tt.configured():
            try:
                start = now_utc() - timedelta(days=160)
                for e in tt.events(start, now_utc() + timedelta(days=120), pv.detect_module):
                    if e["module_code"] == code and e["kind"] == "lecture":
                        out.append({"date": e["start"].date().isoformat(), "start": e["start"], "title": e["title"]})
            except Exception:
                pass
        if not out:
            for r in self.store.all():
                st = pv.parse_panopto_date(r["start_utc"])
                if r["module_code"] == code and st:
                    out.append({"date": st.date().isoformat(), "start": st, "title": r["name"]})
        out.sort(key=lambda x: x["start"])
        return out

    def _lecture_context(self, row: dict) -> dict:
        start = pv.parse_panopto_date(row["start_utc"])
        if not start:
            return {}
        local = start.astimezone(tt.LONDON)
        week = md.teaching_week(local.date(), pv.CONFIG.get("semester_start"))
        ordinal = None
        if row["module_code"]:
            sched = self._lecture_schedule(row["module_code"])
            for i, l in enumerate(sched, 1):
                if abs((l["start"] - start).total_seconds()) <= 50 * 60:
                    ordinal = i
                    break
            if ordinal is None:
                ordinal = sum(1 for l in sched if l["start"] < start) + 1 if sched else None
        previous = []
        for r in self.store.all():
            st = pv.parse_panopto_date(r["start_utc"])
            if (r["module_code"] == row["module_code"] and r["status"] == "done" and st and st < start
                    and r["delivery_id"] != row["delivery_id"]):
                previous.append({"date": st.date().isoformat(), "title": r["title"] or r["name"],
                                 "summary": r["summary"] or ""})
        return {"week": week, "ordinal": ordinal, "previous": previous}

    def _job_moodle(self, key: str, force: bool = False):
        pan = self._panopto()
        mo = md.Moodle(pan.ctx)
        try:
            mode = mo.connect()
        except md.NeedMoodleLogin:
            was = self.store.get_meta("moodle_auth")
            self.store.set_meta("moodle_auth", "expired")
            self.store.set_meta("moodle_error", "Moodle sign-in expired: press Sign in")
            if was != "expired":
                notify("Lecture Vault: sign in to Moodle", "Open Lecture Vault and press Sign in to fetch your module files.")
            raise RuntimeError("Moodle sign-in needed: press Sign in")
        self.store.set_meta("moodle_auth", "ok")
        courses = mo.courses()
        status = {}
        for code, info in pv.CONFIG.get("modules", {}).items():
            course = md.find_course(code, info, courses)
            if not course:
                status[code] = {"error": "No Moodle course found with this code"}
                continue
            try:
                sched = [{"date": l["date"], "title": l["title"]} for l in self._lecture_schedule(code)]
                res = md.sync_module(mo, code, info, course, sched, pv.CONFIG.get("semester_start") or "",
                                     say=lambda m: self.store.set_meta("moodle_step", m), force_classify=force)
                status[code] = {**res, "course": course.get("fullname") or course.get("shortname"),
                                "course_id": course["id"]}
            except Exception as e:
                traceback.print_exc()
                status[code] = {"error": brief(e)}
        self.store.set_meta("moodle_status", json.dumps(status))
        self.store.set_meta("moodle_mode", mode)
        self.store.set_meta("moodle_last", now_utc().isoformat())
        self.store.set_meta("moodle_step", "")
        errs = [f"{c}: {v['error']}" for c, v in status.items() if v.get("error")]
        self.store.set_meta("moodle_error", "; ".join(errs)[:500])
        try:
            self._sync_resources()
        except Exception as e:
            self.store.set_meta("last_error", str(e)[:400])

    def _moodle_due(self) -> bool:
        last = pv.parse_panopto_date(self.store.get_meta("moodle_last") or None)
        return last is None or now_utc() - last > timedelta(hours=MOODLE_SYNC_HOURS)

    def _job_sync(self, key: str, auto: bool = False):
        resource_error = None
        if os.getenv("MOODLE_SYNC", "1") == "1" and self._moodle_due():
            try:
                self._job_moodle("moodle")
            except NeedLogin:
                raise
            except Exception as e:
                self.store.set_meta("moodle_error", brief(e))
        try:
            self._sync_resources()
        except Exception as e:
            resource_error = brief(e)
        pan = self._panopto()
        folders = pv.watch_folders() or [(None, None)]
        seen = 0
        for fid, module in folders:
            sem = pv.CONFIG.get("semester_start")
            for s in pan.folder_sessions(fid):
                if sem and s.get("start") and s["start"].date().isoformat() < sem:
                    continue  # previous years' recordings in the same folder
                code = module or pv.detect_module(s.get("name") or "", s.get("folder") or "")
                self.store.upsert_session(s, code)
                seen += 1
        if not pv.watch_folders():
            learned = {}
            for s in pan.folder_sessions(None):
                code = pv.detect_module(s.get("name") or "", s.get("folder") or "")
                if code and s.get("folder_id"):
                    learned.setdefault(s["folder_id"], code)
            if learned:
                pv.CONFIG["watch_folders"] = [{"module": c, "id": f} for f, c in learned.items()]
                pv.save_config()
                with self.store.lock:
                    self.store.conn.execute("delete from lectures where module_code is null and status = 'new'")
                    self.store.conn.commit()
        self.store.set_meta("last_sync", now_utc().isoformat())
        self.store.set_meta("last_sync_count", seen)
        self.store.set_meta("last_error", resource_error or "")

        # lectures already filed by the CLI or an earlier install
        done_cli = pv.load_json(pv.STATE_FILE, {"done": {}}).get("done", {})
        for r in self.store.all():
            if r["status"] == "new" and r["delivery_id"] in done_cli:
                self.store.update(r["delivery_id"], status="done", vault_checked=1)
            elif r["status"] == "new" and not r["vault_checked"] and self.vault is not None:
                try:
                    exists = self.vault.note_exists(r["delivery_id"])
                    self.store.update(r["delivery_id"], vault_checked=1, **({"status": "done"} if exists else {}))
                except Exception:
                    pass

        if auto and self.store.get_meta("auto_transcribe", os.getenv("AUTO_TRANSCRIBE", "1")) == "1":
            cutoff = now_utc() - timedelta(days=AUTO_SINCE_DAYS)
            for r in self.store.all():
                start = pv.parse_panopto_date(r["start_utc"])
                if not start or start < cutoff or not r["module_code"]:
                    continue  # only auto-process lectures that belong to one of your modules
                retry_error = r["status"] == "error" and (r["attempts"] or 0) < AUTO_MAX_ATTEMPTS
                if r["status"] in ("new", "waiting_captions") or retry_error:
                    self.enqueue("process", r["delivery_id"], allow_defer=True)
            # upgrade notes written by an older version of the notes engine (whole semester)
            sem = pv.CONFIG.get("semester_start")
            for r in self.store.all():
                start = pv.parse_panopto_date(r["start_utc"])
                stale = (r["notes_version"] or 1) < pv.NOTES_VERSION or _notes_blank(r["notes_path"])
                if (r["status"] == "done" and stale and r["module_code"]
                        and start and (not sem or start.date().isoformat() >= sem)):
                    self.enqueue("process", r["delivery_id"], allow_defer=False)

    def _job_process(self, key: str, allow_defer: bool = False, prev: str = "new"):
        row = self.store.get(key)
        if not row:
            raise RuntimeError("Unknown lecture")
        pan = self._panopto()
        self.store.update(key, status="processing", step="Starting", error=None, attempts=(row["attempts"] or 0) + 1)
        res = pv.process(
            pan, key, self.vault,
            listing={"name": row["name"], "folder": row["folder"], "start": row["start_utc"]},
            allow_defer=allow_defer,
            progress=lambda s: self.store.update(key, step=s),
            material_override=row.get("materials_override"),
            module_hint=row["module_code"],
            claimed=self.store.claimed_files(exclude=key),
            lecture_context=self._lecture_context(row),
        )
        if res["status"] == "deferred":
            self.store.update(key, status="waiting_captions", step="Waiting for Panopto captions")
            return
        notes = res["notes"]
        self.store.update(
            key, status="done", step=None, error=None, notes_path=res["md_path"], title=notes["title"],
            summary=notes["summary"], difficulty=notes.get("difficulty"),
            module_code=notes.get("module_code") or row["module_code"],
            materials=res.get("material"), comparison=notes.get("material_comparison"),
            exam_links=notes.get("exam_links"),
            transcript_source=res["meta"]["transcript_source"], vault=res.get("vault"), attempts=0,
            notes_version=res.get("notes_version", 1), tutorials=res.get("tutorials"), coursework=res.get("coursework"),
        )
        try:
            with self.store.lock:
                study.add_cards(self.store.conn, key, notes.get("module_code") or row["module_code"], notes["title"],
                                res["meta"]["date"], notes)
        except Exception as e:
            print(f"cards: {e}")
        code = notes.get("module_code") or row["module_code"] or ""
        comp = notes.get("material_comparison") or {}
        extra = f" · {len(comp.get('material_only') or [])} self-study topics" if comp else ""
        notify(f"Notes ready: {code} {notes['title']}"[:120], (notes.get("summary") or "")[:180] + extra)


def _job_plan(self, key: str, code: str = ""):
    info = pv.CONFIG.get("modules", {}).get(code, {})
    rows = [r for r in self.store.all() if r["module_code"] == code and r["status"] == "done"]
    rows.sort(key=lambda r: r["start_utc"] or "")
    lectures = []
    for r in rows:
        comp = r.get("comparison") or {}
        lectures.append({"date": (r["start_utc"] or "")[:10], "title": r["title"] or r["name"],
                         "summary": r["summary"] or "",
                         "self_study": [x.get("topic") for x in comp.get("material_only") or [] if isinstance(x, dict)],
                         "tutorials": r.get("tutorials") or []})
    upcoming = []
    for l in self._lecture_schedule(code):
        if now_utc() <= l["start"] <= now_utc() + timedelta(days=10):
            upcoming.append({"date": l["date"], "title": l["title"]})
    with self.store.lock:
        cs = study.stats(self.store.conn).get(code, {})
        weak = study.weak_cards(self.store.conn, code)
        works = engine.work_list(self.store.conn, code)
        pri = self.store.conn.execute("select score, reasoning, hours from priority where module_code=? order by week_start desc limit 1", (code,)).fetchone()
    cs = {**cs, "tracked_work": [{"title": w["title"], "type": w["work_type"], "status": w["status"], "score_pct": w["score_pct"],
                                  "due": w["due_date"], "marked": f"{len(w['question_marks'])}/{w['total_questions'] or '?'}" if w["work_type"] != "coursework" else None,
                                  "milestones": [m["name"] + (" done" if m.get("done") else f" {m.get('pct', 0)}%") for m in w["milestones"]] if w["work_type"] == "coursework" else None,
                                  "errors": [f"Q{e['question']} {e.get('tag')}" for e in w["errors"]]} for w in works],
          "priority": {"score": pri[0], "why": pri[1], "hours_this_week": pri[2]} if pri else None}
    md_text = study.build_plan(code, info.get("name", code), lectures, upcoming, cs, weak, pv.NOTES_MODEL,
                               assessment=pv.assessment(code))
    with self.store.lock:
        path = study.save_plan(self.store.conn, code, md_text, pv.NOTES_DIR)
    notify(f"{code}: this week's plan is ready", "Open Lecture Vault, Modules, Weekly plan.")
    if self.vault is not None:
        try:
            first = re.sub(r"\s+", " ", md_text)[:900]
            self.vault.insert_memory({"content": f"{code} weekly study plan (week of {study.week_start()}): {first}",
                                      "summary": f"{code} study plan, week of {study.week_start()}",
                                      "entity_type": "plan", "domain": "academic", "subdomain": code,
                                      "project": info.get("name", code), "source": "lecture-vault",
                                      "tags": ["study-plan", code], "is_actionable": True,
                                      "next_action": f"Work through the {code} plan: {path.name}"})
        except Exception as e:
            print(f"plan memory: {e}")


Worker._job_plan = _job_plan


def _deadline_days(worker: Worker, code: str) -> int | None:
    best = None
    today = date.today()
    for w in engine.work_list(worker.store.conn, code):
        if w["status"] == "completed" or not w.get("due_date"):
            continue
        try:
            d = (date.fromisoformat(str(w["due_date"])[:10]) - today).days
        except ValueError:
            continue
        if d >= 0 and (best is None or d < best):
            best = d
    return best


def _build_question_bank(self):
    """Exam modules: split tutorial sheets, past papers and worked examples into practice cards."""
    mods = pv.CONFIG.get("modules", {})
    exam = [c for c in mods if pv.assessment(c)["exam"]]
    study.RECALL_OFF = set(exam)
    rows = self.store.all()
    for code in exam:
        lectures = [{"delivery_id": r["delivery_id"], "title": r["title"] or r["name"],
                     "date": (r["start_utc"] or "")[:10], "notes_path": r["notes_path"]}
                    for r in rows if r["module_code"] == code and r["status"] == "done" and r["notes_path"]]
        with self.store.lock:
            works = [w for w in engine.work_list(self.store.conn, code) if w["work_type"] in ("tutorial", "past_paper")]
        # extraction calls Claude: do it outside the database lock (results are cached per file)
        files = mat.scan_module(code)["questions"]
        client = None
        for w in works:
            try:
                path = mat.resolve(w["file"])
                if qbank.duplicate_scan(path, files) or qbank.answers_only(path):
                    continue
                if client is None:
                    import anthropic
                    client = anthropic.Anthropic()
                qbank.extract(path, qbank.solution_for(path, files), code, qbank.kind_of(w, path), client, pv.NOTES_MODEL)
            except Exception as e:
                print(f"qbank: {w['file']}: {e}", flush=True)
        try:
            with self.store.lock:
                n = qbank.sync_bank(self.store.conn, code, lectures, client=client or object(), model=pv.NOTES_MODEL)
            print(f"qbank: {code}: {n} questions", flush=True)
        except Exception:
            traceback.print_exc()


def _job_engine(self, key: str):
    """Weekly scoring pass: tutorial sheets and coursework become tracked work, modules get a priority."""
    sem = pv.CONFIG.get("semester_start")
    mods = pv.CONFIG.get("modules", {})
    created = 0
    with self.store.lock:
        for code in mods:
            created += engine.discover_work(self.store.conn, code, pv.assessment(code), sem)
        # seed coursework milestones from the brief
        for w in engine.work_list(self.store.conn):
            if w["work_type"] == "coursework" and not w["milestones"]:
                try:
                    txt = "\n".join(mat.extract_pages(mat.resolve(w["file"])))[:30000]
                except Exception:
                    txt = ""
                self.store.conn.execute("update work set milestones=? where id=?", (json.dumps(engine.default_milestones(txt)), w["id"]))
        self.store.conn.commit()
    _build_question_bank(self)
    lectures_by_module, deadlines = {}, {}
    now = now_utc()
    rows = self.store.all()
    for code in mods:
        lst = []
        for l in self._lecture_schedule(code):
            noted = any(r["module_code"] == code and r["status"] == "done" and r["start_utc"]
                        and abs((pv.parse_panopto_date(r["start_utc"]) - l["start"]).total_seconds()) < 3600 for r in rows)
            lst.append({"delivered": l["start"] < now, "noted": noted})
        lectures_by_module[code] = lst
        deadlines[code] = _deadline_days(self, code)
    with self.store.lock:
        pri = engine.compute_priorities(self.store.conn, mods, lectures_by_module,
                                        {c: pv.assessment(c) for c in mods}, deadlines,
                                        weekly_hours=float(os.getenv("STUDY_HOURS_PER_WEEK", "20")))
        works = engine.work_list(self.store.conn)
    self.store.set_meta("engine_last", now.isoformat())
    if created:
        notify("Lecture Vault: new work tracked", f"{created} tutorial sheets / coursework briefs added to your tracker.")
    if self.vault is None:
        return
    errs = []
    week = engine.teaching_week(sem)
    for w in works:
        try:
            vid = self.vault.sync_work(w, week)
            if vid and not w.get("vault_id"):
                with self.store.lock:
                    self.store.conn.execute("update work set vault_id=? where id=?", (vid, w["id"]))
                    self.store.conn.commit()
        except Exception as e:
            errs.append(f"work {w['title'][:30]}: {e}")
    for p in pri:
        try:
            self.vault.sync_priority(p, week)
        except Exception as e:
            errs.append(f"priority {p['module_code']}: {e}")
    try:
        self.vault.ensure_goals(mods)
        cw = [p["coursework_progress"] for p in pri if pv.assessment(p["module_code"])["coursework"] and p["coursework_progress"] is not None]
        ex = [p["mastery"] for p in pri if pv.assessment(p["module_code"])["exam"] and p["mastery"] is not None]
        self.vault.update_goals(sum(cw) / len(cw) if cw else None, sum(ex) / len(ex) if ex else None)
        top = pri[0] if pri else None
        if top:
            self.vault.insert_memory({
                "content": "Weekly academic priority (week " + str(week) + "): " + "; ".join(
                    f"{p['module_code']} {p['score']:.0f}/100, {p['hours']}h ({p['reasoning']})" for p in pri),
                "summary": f"Study priority week {week}: {top['module_code']} first",
                "entity_type": "insight", "domain": "academic", "source": "lecture-vault",
                "tags": ["priority", "week-" + str(week)], "is_actionable": True,
                "next_action": f"Give {top['module_code']} about {top['hours']}h this week"})
    except Exception as e:
        errs.append(f"goals: {e}")
    self.store.set_meta("engine_error", "; ".join(errs)[:500])


Worker._job_engine = _job_engine


def engine_due(worker: Worker) -> bool:
    last = pv.parse_panopto_date(worker.store.get_meta("engine_last") or None)
    return last is None or now_utc() - last > timedelta(hours=float(os.getenv("ENGINE_HOURS", "12")))


def plans_due(worker: Worker):
    """One plan per module per week, once that module has notes."""
    ws = study.week_start()
    with worker.store.lock:
        have = {r[0] for r in worker.store.conn.execute("select module_code from plans where week_start=?", (ws,))}
    noted = {r["module_code"] for r in worker.store.all() if r["status"] == "done" and r["module_code"]}
    for code in pv.CONFIG.get("modules", {}):
        if code in noted and code not in have:
            worker.enqueue("plan", f"plan:{code}", code=code)


def scheduler(worker: Worker):
    time.sleep(3)
    while True:
        worker.enqueue("sync", "sync", auto=True)
        if engine_due(worker):
            worker.enqueue("engine", "engine")
        if os.getenv("AUTO_PLANS", "1") == "1":
            try:
                plans_due(worker)
            except Exception as e:
                print(f"plans: {e}")
        time.sleep(max(1.0, AUTO_SYNC_MINUTES) * 60)


# ---------------------------------------------------------------- state building

def module_list() -> list[dict]:
    out = []
    for i, (code, info) in enumerate(pv.CONFIG.get("modules", {}).items()):
        out.append({"code": code, "name": info.get("name", code), "lecturer": info.get("lecturer", ""),
                    "color": info.get("color") or PALETTE[i % len(PALETTE)],
                    "materials_mode": info.get("materials_mode", "auto"), "assessment": pv.assessment(code)})
    return out


ROW_STATUS = {"new": "available", "queued": "queued", "processing": "processing",
              "waiting_captions": "waiting_captions", "done": "done", "error": "error"}


def _session_item(r: dict) -> dict:
    start = pv.parse_panopto_date(r["start_utc"])
    comp = r.get("comparison") or {}
    return {
        "delivery_id": r["delivery_id"],
        "module_code": r["module_code"],
        "session_name": r["name"],
        "status": ROW_STATUS.get(r["status"], r["status"]),
        "step": r["step"], "error": r["error"],
        "notes_title": r["title"], "summary": r["summary"], "difficulty": r["difficulty"],
        "notes_version": r.get("notes_version") or 1, "tutorials": r.get("tutorials"), "coursework": r.get("coursework"),
        "understanding": r.get("understanding"),
        "has_notes": bool(r["notes_path"] and Path(r["notes_path"]).exists()),
        "materials": r.get("materials"), "materials_override": r.get("materials_override"),
        "coverage_pct": comp.get("coverage_pct"),
        "comparison_counts": {k: len(comp.get(k) or []) for k in ("covered", "lecture_only", "material_only", "discrepancies")} if comp else None,
        "transcript_source": r["transcript_source"], "vault": r["vault"],
        "recording_url": pv.viewer_url(r["delivery_id"]),
        "_start": start,
        "_end": start + timedelta(minutes=r["duration_min"] or 50) if start else None,
    }


def _engine_state() -> dict:
    with store.lock:
        ws = engine.week_start()
        pri = [dict(zip(["module_code", "score", "mastery", "error_density", "coverage", "retention", "deadline_days", "hours", "rank", "reasoning"], r))
               for r in store.conn.execute("select module_code, score, mastery, error_density, coverage, retention, deadline_days, hours, rank, reasoning "
                                           "from priority where week_start=? order by rank", (ws,))]
        works = engine.work_list(store.conn)
        due = sum(v["due"] for v in study.stats(store.conn).values())
        plans = {m for m in pv.CONFIG.get("modules", {}) if study.get_plan(store.conn, m)}
        acts = engine.next_actions(store.conn, pri, due, plans, {c: pv.assessment(c) for c in pv.CONFIG.get("modules", {})})
    return {"priorities": pri, "work": works, "next": acts, "last": store.get_meta("engine_last"),
            "error": store.get_meta("engine_error", ""), "week": engine.teaching_week(pv.CONFIG.get("semester_start")),
            "error_tags": engine.ERROR_TAGS}


def _review_state() -> dict:
    with store.lock:
        st = study.stats(store.conn)
        plans = {}
        for m in pv.CONFIG.get("modules", {}):
            pl = study.get_plan(store.conn, m)
            if pl:
                plans[m] = {"week_start": pl["week_start"], "created_at": pl["created_at"]}
    return {"stats": st, "due_total": sum(v["due"] for v in st.values()), "plans": plans,
            "pending_plans": [k.split(":", 1)[1] for k in worker.snapshot()["pending"] if k.startswith("plan:")]}


def build_state(range_start: datetime, range_end: datetime) -> dict:
    now = now_utc()
    window_start = min(range_start, now - timedelta(days=150))
    window_end = max(range_end, now + timedelta(days=60))
    rows = store.all()
    sessions = [_session_item(r) for r in rows]

    timetable_error, events = None, []
    if tt.configured():
        try:
            events = tt.events(window_start, window_end, pv.detect_module)
        except Exception as e:
            timetable_error = str(e)[:300]

    links = tt.link(events, [{"delivery_id": s["delivery_id"], "start": s["_start"], "module_code": s["module_code"]}
                             for s in sessions])
    by_id = {s["delivery_id"]: s for s in sessions}
    used, items = set(), []
    for e in events:
        s = by_id.get(links.get(e["uid"]))
        item = {"id": f"tt:{e['uid']}", "title": e["title"], "location": e["location"], "kind": e["kind"],
                "module_code": e["module_code"], "start": e["start"], "end": e["end"], "source": "timetable"}
        if s:
            used.add(s["delivery_id"])
            item.update({k: v for k, v in s.items() if not k.startswith("_")})
            item["id"] = s["delivery_id"]
            item["source"] = "both"
            item["module_code"] = e["module_code"] or s["module_code"]
        elif e["start"] > now:
            item["status"] = "upcoming"
        elif e["end"] >= now:
            item["status"] = "live"
        elif now - e["end"] < timedelta(hours=36) and e["kind"] == "lecture":
            item["status"] = "pending_recording"
        else:
            item["status"] = "no_recording"
        items.append(item)
    for s in sessions:
        if s["delivery_id"] in used or not s["_start"]:
            continue
        item = {k: v for k, v in s.items() if not k.startswith("_")}
        item.update({"id": s["delivery_id"], "title": s["session_name"], "location": "", "kind": "lecture",
                     "start": s["_start"].astimezone(tt.LONDON), "end": s["_end"].astimezone(tt.LONDON),
                     "source": "panopto"})
        items.append(item)

    keys = ["scheduled", "recorded", "noted", "to_note", "active", "no_recording", "upcoming"]
    stats = {m["code"]: dict.fromkeys(keys, 0) for m in module_list()}
    for it in items:
        st = stats.setdefault(it["module_code"] or "unassigned", dict.fromkeys(keys, 0))
        status = it.get("status")
        if status in ("upcoming", "live"):
            st["upcoming"] += 1
            continue
        if it.get("kind") not in ("lecture", None) and not it.get("delivery_id"):
            continue  # tutorials/labs are rarely recorded: don't count as missing
        st["scheduled"] += 1
        if it.get("delivery_id"):
            st["recorded"] += 1
            if status == "done":
                st["noted"] += 1
            elif status in ("queued", "processing", "waiting_captions"):
                st["active"] += 1
            else:
                st["to_note"] += 1
        elif status == "no_recording":
            st["no_recording"] += 1
    visible = [it for it in items if it["end"] >= range_start and it["start"] < range_end]
    for it in visible:
        it["start"] = it["start"].astimezone(tt.LONDON).isoformat()
        it["end"] = it["end"].astimezone(tt.LONDON).isoformat()
    visible.sort(key=lambda x: x["start"])

    files = {}
    for m in module_list():
        try:
            sc = mat.scan_module(m["code"], None if m["materials_mode"] == "auto" else m["materials_mode"])
            files[m["code"]] = {"slides": len(sc["per_lecture"]), "packs": len(sc["packs"]),
                                "questions": len(sc["questions"]), "info": len(sc["info"])}
        except Exception:
            files[m["code"]] = {"slides": 0, "packs": 0, "questions": 0, "info": 0}
    return {
        "lectures": visible,
        "modules": module_list(),
        "module_files": files,
        "review": _review_state(),
        "engine": _engine_state(),
        "moodle": {
            "auth": store.get_meta("moodle_auth", "unknown"),
            "last": store.get_meta("moodle_last") or None,
            "step": store.get_meta("moodle_step", ""),
            "error": store.get_meta("moodle_error", ""),
            "mode": store.get_meta("moodle_mode", ""),
            "modules": {c: {**v, "profile": md.module_profile(c)}
                        for c, v in json.loads(store.get_meta("moodle_status", "{}") or "{}").items()},
        },
        "stats": stats,
        "worker": worker.snapshot(),
        "auth": store.get_meta("auth", "unknown"),
        "last_sync": store.get_meta("last_sync"),
        "last_sync_count": store.get_meta("last_sync_count"),
        "last_error": promptlib.MISSING_MESSAGE if promptlib.MISSING else store.get_meta("last_error", ""),
        "settings": {
            "auto_transcribe": store.get_meta("auto_transcribe", os.getenv("AUTO_TRANSCRIBE", "1")) == "1",
            "auto_sync_minutes": AUTO_SYNC_MINUTES,
            "watch_folders": pv.CONFIG.get("watch_folders", []),
            "timetable_configured": tt.configured(),
            "timetable_error": timetable_error,
            "timetable_events": len(events),
            "vault": "connected" if vault is not None else "local only",
            "resources_uploaded": int(store.get_meta("res_count", "0") or 0),
            "materials_dir": str(mat.MATERIALS_DIR),
            "notes_dir": str(pv.NOTES_DIR),
        },
        "now": now.astimezone(tt.LONDON).isoformat(),
    }


# ---------------------------------------------------------------- app

store = Store(ROOT / "lectures.db")
vault = pv.VaultDB() if pv.VaultDB.configured() else None
worker = Worker(store, vault)
app = FastAPI(title="Lecture Vault")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    # Only this page can trigger actions: browsers can't send custom headers cross-site without CORS.
    if request.method == "POST" and request.headers.get("x-lecture-vault") != "1":
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-store"})


def _parse_day(s: str | None, default: datetime) -> datetime:
    if not s:
        return default
    d = date.fromisoformat(s)
    return datetime(d.year, d.month, d.day, tzinfo=tt.LONDON)


@app.get("/api/state")
def api_state(start: str | None = None, end: str | None = None):
    today = datetime.now(tt.LONDON).replace(hour=0, minute=0, second=0, microsecond=0)
    s = _parse_day(start, today - timedelta(days=today.weekday()))
    e = _parse_day(end, s + timedelta(days=7))
    return build_state(s, e)


def _need(did: str) -> dict:
    row = store.get(did)
    if not row:
        raise HTTPException(404, "Lecture not found in Panopto list yet: press Sync")
    return row


@app.post("/api/lectures/{did}/transcribe")
def api_transcribe(did: str):
    row = _need(did)
    if row["status"] in ("queued", "processing"):
        raise HTTPException(409, "Already in progress")
    worker.enqueue("process", did, allow_defer=False)
    return {"ok": True}


@app.post("/api/transcribe-missing")
def api_transcribe_missing(payload: dict = Body(default={})):
    module = payload.get("module")
    n = 0
    for r in store.all():
        if module and r["module_code"] != module:
            continue
        sem = pv.CONFIG.get("semester_start")
        if r["status"] in ("new", "error", "waiting_captions") and r["start_utc"] and r["module_code"] \
                and (not sem or r["start_utc"][:10] >= sem):
            if pv.parse_panopto_date(r["start_utc"]) <= now_utc():
                n += worker.enqueue("process", r["delivery_id"], allow_defer=False)
    return {"queued": n}


def _notes_text(path: str | None) -> str | None:
    """Notes body without the YAML header, or None if there is no file."""
    if not path or not Path(path).exists():
        return None
    md = Path(path).read_text(encoding="utf-8")
    if md.startswith("---"):
        parts = md.split("\n---", 1)
        md = parts[1].lstrip("-").lstrip() if len(parts) > 1 else ""
    return md


def _notes_blank(path: str | None) -> bool:
    md = _notes_text(path)
    return md is not None and len(md.strip()) < 200


@app.get("/api/lectures/{did}/notes")
def api_notes(did: str):
    row = _need(did)
    md = _notes_text(row["notes_path"])
    reason = None
    if md is not None and len(md.strip()) < 200:
        md, reason = None, "blank"
    elif md is None:
        reason = "missing" if row["notes_path"] else "none"
    return {"markdown": md, "reason": reason, "status": row["status"],
            "comparison": row.get("comparison"), "materials": row.get("materials"),
            "exam_links": row.get("exam_links"),
            "notes_path": row["notes_path"], "recording_url": pv.viewer_url(did)}


@app.get("/api/materials/{code}")
def api_materials(code: str):
    mode = pv.CONFIG.get("modules", {}).get(code, {}).get("materials_mode")
    return mat.describe_module(code, mode)


@app.post("/api/lectures/{did}/materials")
def api_set_materials(did: str, payload: dict = Body(default={})):
    _need(did)
    mode = payload.get("mode", "auto")
    if mode == "auto":
        override = None
    elif mode == "none":
        override = {"mode": "none"}
    else:
        f = payload.get("file")
        if not f:
            raise HTTPException(400, "Pick a file")
        try:
            if not mat.resolve(f).is_file():
                raise HTTPException(400, "File not found in the materials folder")
        except ValueError as e:
            raise HTTPException(400, str(e))
        pages = payload.get("pages")
        if pages:
            a, b = int(pages[0]), int(pages[1])
            if a < 1 or b < a:
                raise HTTPException(400, "Page range must be like 12 to 20")
            pages = [a, b]
        override = {"mode": "file", "file": f, "pages": pages or None}
    store.update(did, materials_override=override)
    return {"ok": True, "override": override}


@app.post("/api/modules/{code}/materials-mode")
def api_materials_mode(code: str, payload: dict = Body(default={})):
    mode = payload.get("mode", "auto")
    if mode not in ("auto", "pack", "per_lecture"):
        raise HTTPException(400, "mode must be auto, pack or per_lecture")
    mods = pv.CONFIG.setdefault("modules", {})
    if code not in mods:
        raise HTTPException(404, "Unknown module")
    if mode == "auto":
        mods[code].pop("materials_mode", None)
    else:
        mods[code]["materials_mode"] = mode
    pv.save_config()
    return {"ok": True}


def _open_path(path: Path):
    path.mkdir(parents=True, exist_ok=True) if path.suffix == "" else None
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


@app.post("/api/open")
def api_open(payload: dict = Body(default={})):
    target = payload.get("target")
    if target == "materials":
        code = payload.get("module")
        path = mat.module_dir(code) if code else mat.MATERIALS_DIR
    elif target == "material_file":
        try:
            path = mat.resolve(payload.get("file", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
    elif target == "notes":
        row = _need(payload.get("delivery_id", ""))
        if not row["notes_path"]:
            raise HTTPException(404, "No notes yet")
        path = Path(row["notes_path"])
    elif target == "notes_dir":
        path = pv.NOTES_DIR
    else:
        raise HTTPException(400, "Unknown target")
    try:
        _open_path(path)
    except Exception as e:
        raise HTTPException(500, f"Couldn't open {path}: {e}")
    return {"ok": True, "path": str(path)}


@app.post("/api/sync")
def api_sync():
    queued = worker.enqueue("sync", "sync", auto=True)
    return {"queued": queued}


@app.get("/api/review/due")
def api_review_due(module: str | None = None, limit: int = 60):
    with store.lock:
        return {"cards": study.due_cards(store.conn, module or None, limit), "stats": study.stats(store.conn)}


@app.post("/api/review/{card_id}")
def api_review_grade(card_id: str, payload: dict = Body(default={})):
    res = payload.get("result")
    if res is not None:  # practice question: Right / Half / Wrong / Skip, logged on the tracker
        if res not in ("correct", "partial", "wrong", "skipped"):
            raise HTTPException(400, "result must be correct, partial, wrong or skipped")
        tag = payload.get("tag") or None
        if tag and tag not in engine.ERROR_TAGS:
            raise HTTPException(400, "unknown error tag")
        try:
            with store.lock:
                return qbank.mark_card(store.conn, card_id, res, tag, (payload.get("note") or "")[:300] or None)
        except KeyError:
            raise HTTPException(404, "Unknown question")
    g = int(payload.get("grade", 0))
    if g not in (1, 2, 3, 4):
        raise HTTPException(400, "grade must be 1 to 4")
    try:
        with store.lock:
            return study.grade(store.conn, card_id, g)
    except KeyError:
        raise HTTPException(404, "Unknown card")


@app.get("/api/export/anki/{code}")
def api_anki(code: str):
    from fastapi.responses import Response
    with store.lock:
        data = study.anki_csv(store.conn, code)
    return Response(data, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{code} flashcards.csv"'})


@app.get("/api/plan/{code}")
def api_plan_get(code: str):
    with store.lock:
        pl = study.get_plan(store.conn, code)
    return pl or {"markdown": None}


@app.post("/api/plan/{code}")
def api_plan_make(code: str):
    if code not in pv.CONFIG.get("modules", {}):
        raise HTTPException(404, "Unknown module")
    return {"queued": worker.enqueue("plan", f"plan:{code}", code=code)}


@app.post("/api/modules/{code}/assessment")
def api_assessment(code: str, payload: dict = Body(default={})):
    mods = pv.CONFIG.setdefault("modules", {})
    if code not in mods:
        raise HTTPException(404, "Unknown module")
    cur = pv.assessment(code)
    for k in ("exam", "coursework", "weekly_tutorials"):
        if k in payload:
            cur[k] = bool(payload[k])
    mods[code]["assessment"] = cur
    pv.save_config()
    return {"ok": True, "assessment": cur}


@app.post("/api/modules/{code}/rerun")
def api_rerun(code: str):
    n = 0
    for r in store.all():
        if r["module_code"] == code and r["status"] in ("done", "error"):
            n += worker.enqueue("process", r["delivery_id"], allow_defer=False)
    return {"queued": n}


@app.post("/api/engine/run")
def api_engine_run():
    return {"queued": worker.enqueue("engine", "engine")}


@app.post("/api/work/{work_id}/mark")
def api_mark(work_id: str, payload: dict = Body(default={})):
    q = str(payload.get("question", "")).strip()
    res = payload.get("result")
    if not q or res not in ("correct", "partial", "wrong", "skipped"):
        raise HTTPException(400, "question and result (correct/partial/wrong/skipped) required")
    tag = payload.get("tag")
    if tag and tag not in engine.ERROR_TAGS:
        raise HTTPException(400, "unknown error tag")
    try:
        with store.lock:
            out = engine.mark(store.conn, work_id, q, res, tag, (payload.get("note") or "")[:300] or None)
    except KeyError:
        raise HTTPException(404, "Unknown work")
    if res in ("wrong", "partial", "skipped") and payload.get("topic"):
        with store.lock:
            w = next((x for x in engine.work_list(store.conn) if x["id"] == work_id), None)
            if w:
                out["cards_boosted"] = study.boost_weak(store.conn, w["module_code"], [payload["topic"]])
    _sync_work_bg(work_id)
    return out


@app.post("/api/work/{work_id}/milestone")
def api_milestone(work_id: str, payload: dict = Body(default={})):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    pct = payload.get("pct")
    with store.lock:
        ms = engine.set_milestone(store.conn, work_id, name, bool(payload.get("done")), int(pct) if pct is not None else None)
    _sync_work_bg(work_id)
    return {"milestones": ms}


@app.post("/api/work/{work_id}/meta")
def api_work_meta(work_id: str, payload: dict = Body(default={})):
    fields = {}
    for k in ("total_questions", "time_minutes", "difficulty", "feedback_score"):
        if k in payload and payload[k] is not None:
            fields[k] = float(payload[k]) if k == "feedback_score" else int(payload[k])
    if "feedback_notes" in payload:
        fields["feedback_notes"] = str(payload["feedback_notes"])[:1000]
    if "due_date" in payload:
        fields["due_date"] = str(payload["due_date"])[:10] or None
    if not fields:
        raise HTTPException(400, "nothing to update")
    with store.lock:
        store.conn.execute(f"update work set {', '.join(k + '=?' for k in fields)}, updated_at=? where id=?",
                           (*fields.values(), engine._now(), work_id))
        store.conn.commit()
    _sync_work_bg(work_id)
    return {"ok": True}


@app.post("/api/work/add")
def api_work_add(payload: dict = Body(default={})):
    code = payload.get("module")
    if code not in pv.CONFIG.get("modules", {}):
        raise HTTPException(404, "Unknown module")
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    wtype = payload.get("work_type", "tutorial")
    wid = f"{code}:manual:{abs(hash(title)) % 10**10}"
    with store.lock:
        store.conn.execute("insert or ignore into work (id, module_code, work_type, title, file, week, total_questions, status, due_date, question_marks, errors, milestones, created_at, updated_at) "
                           "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (wid, code, wtype, title, f"manual/{code}/{title}", payload.get("week"), payload.get("total_questions"), "not_started",
                            payload.get("due_date"), "{}", "[]", json.dumps(engine.default_milestones("")) if wtype == "coursework" else "[]", engine._now(), engine._now()))
        store.conn.commit()
    return {"id": wid}


@app.post("/api/lectures/{did}/rate")
def api_rate(did: str, payload: dict = Body(default={})):
    _need(did)
    rating = payload.get("rating")
    if rating is not None and int(rating) not in (1, 2, 3, 4, 5):
        raise HTTPException(400, "rating 1 to 5")
    store.update(did, understanding=int(rating) if rating is not None else None)
    if vault is not None:
        try:
            vault.rate_note(did, int(rating) if rating is not None else None,
                            payload.get("status"), payload.get("questions"), payload.get("minutes"))
        except Exception as e:
            raise HTTPException(502, f"Saved locally; Vault update failed: {e}")
    return {"ok": True}


def _sync_work_bg(work_id: str):
    if vault is None:
        return
    def go():
        try:
            with store.lock:
                w = next((x for x in engine.work_list(store.conn) if x["id"] == work_id), None)
            if w:
                vault.sync_work(w, engine.teaching_week(pv.CONFIG.get("semester_start")))
        except Exception as e:
            print(f"work sync: {e}")
    threading.Thread(target=go, daemon=True).start()


@app.post("/api/moodle/sync")
def api_moodle_sync(payload: dict = Body(default={})):
    queued = worker.enqueue("moodle", "moodle", force=bool(payload.get("reclassify")))
    return {"queued": queued}


@app.post("/api/modules/{code}/moodle-course")
def api_moodle_course(code: str, payload: dict = Body(default={})):
    mods = pv.CONFIG.setdefault("modules", {})
    if code not in mods:
        raise HTTPException(404, "Unknown module")
    raw = str(payload.get("course") or "")
    import re as _re
    m = _re.search(r"id=(\d+)", raw) or _re.fullmatch(r"\s*(\d+)\s*", raw)
    if raw and not m:
        raise HTTPException(400, "Paste the Moodle course link (…/course/view.php?id=1234)")
    if m:
        mods[code]["moodle_id"] = int(m.group(1))
    else:
        mods[code].pop("moodle_id", None)
    pv.save_config()
    worker.enqueue("moodle", "moodle")
    return {"ok": True}


@app.post("/api/login")
def api_login():
    worker.enqueue("login", "login")
    return {"ok": True}


@app.post("/api/settings")
def api_settings(payload: dict = Body(default={})):
    if "auto_transcribe" in payload:
        store.set_meta("auto_transcribe", "1" if payload["auto_transcribe"] else "0")
        if payload["auto_transcribe"]:
            worker.enqueue("sync", "sync", auto=True)
    return {"ok": True}


@app.post("/api/folders")
def api_add_folder(payload: dict = Body(default={})):
    raw = (payload.get("url") or "").replace("%22", "")
    m = pv.GUID_RE.search(raw)
    if not m:
        raise HTTPException(400, "Couldn't find a folder ID in that link")
    folders = [f for f in pv.CONFIG.get("watch_folders", [])
               if pv.GUID_RE.search(str(f.get("id", "") if isinstance(f, dict) else f).replace("%22", ""))]
    if any(m.group(0) in str(f) for f in folders):
        raise HTTPException(409, "Folder already added")
    folders.append({"module": payload.get("module") or None, "id": m.group(0)})
    pv.CONFIG["watch_folders"] = folders
    pv.save_config()
    worker.enqueue("sync", "sync", auto=True)
    return {"ok": True}


@app.post("/api/folders/remove")
def api_remove_folder(payload: dict = Body(default={})):
    fid = payload.get("id", "")
    pv.CONFIG["watch_folders"] = [f for f in pv.CONFIG.get("watch_folders", []) if fid not in str(f)]
    pv.save_config()
    return {"ok": True}


@app.post("/api/timetable/refresh")
def api_timetable_refresh():
    if not tt.configured():
        raise HTTPException(400, "Set TIMETABLE_ICS_URL in .env or add timetable.ics")
    try:
        n = len(tt.events(now_utc() - timedelta(days=150), now_utc() + timedelta(days=60), pv.detect_module, force=True))
    except Exception as e:
        raise HTTPException(502, f"Timetable fetch failed: {e}")
    return {"events": n}


# ---------------------------------------------------------------- watchdog
# The worker runs one job at a time. A browser call that never returns (typically after the laptop
# sleeps mid-request) used to block every later job, including sign-in, indefinitely. The watchdog
# spots a job that has stopped making progress, saves the queue, and restarts the app cleanly.

JOB_STALL_MIN = int(os.getenv("JOB_STALL_MIN", "40"))
WAKE_GRACE_MIN = int(os.getenv("WAKE_GRACE_MIN", "4"))


def _kill_children() -> None:
    """Close the headless Edge and Playwright driver this app started, so the profile is free."""
    if os.name != "nt":
        return
    prof = str(ROOT / ".browser-profile").replace("'", "''")
    venv = str(ROOT / ".venv").replace("'", "''")
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
          f"($_.CommandLine.Contains('{prof}') -or ($_.Name -eq 'node.exe' -and $_.CommandLine.Contains('{venv}'))) }} "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        traceback.print_exc()


def restart_app(w: "Worker", reason: str) -> None:
    print(f"watchdog: {reason}; restarting", flush=True)
    jobs = []
    try:
        with w.q.mutex:
            queued = list(w.q.queue)
        if w.current_job and w.current_job[0] in ("process", "login", "plan", "moodle", "engine"):
            jobs.append(w.current_job)
        jobs += [job for _p, _s, job in sorted(queued)]
        kind, key, _ = w.current_job or (None, None, None)
        if kind == "process":
            w.store.update(key, step=None, error=f"Stalled ({reason}), retried automatically")
        w.store.set_meta("requeue", json.dumps([[k, key_, {x: v for x, v in pl.items() if x != "prev"}]
                                                for k, key_, pl in jobs if k != "sync"]))
        w.store.set_meta("last_error", "")
    except Exception:
        traceback.print_exc()
    _kill_children()
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--no-browser", "--wait-port"], cwd=str(ROOT),
                     creationflags=flags, close_fds=True)
    os._exit(3)


def watchdog(w: "Worker") -> None:
    last_tick, woke_at, cpu_marks = time.time(), None, []
    while True:
        time.sleep(30)
        now = time.time()
        if now - last_tick > 120:
            woke_at = now  # the laptop slept or hibernated
            print(f"watchdog: resumed after {int(now - last_tick)}s asleep", flush=True)
        last_tick = now
        cpu_marks.append((now, time.process_time()))
        cpu_marks = [m for m in cpu_marks if now - m[0] <= JOB_STALL_MIN * 60 + 60]
        if not w.current_job:
            woke_at = None
            continue

        def cpu_since(t):
            past = [c for ts, c in cpu_marks if ts >= t]
            return (cpu_marks[-1][1] - past[0]) if past else 1e9

        quiet_since = max(HEARTBEAT[0], w.started)
        awake_long = woke_at is None or now - woke_at > JOB_STALL_MIN * 60
        if awake_long and now - quiet_since > JOB_STALL_MIN * 60 and cpu_since(now - JOB_STALL_MIN * 60) < 20:
            restart_app(w, f"{w.current_job[0]} job made no progress for {JOB_STALL_MIN} min")
        if (woke_at and now - woke_at > WAKE_GRACE_MIN * 60 and HEARTBEAT[0] < woke_at
                and cpu_since(woke_at) < 5):
            restart_app(w, f"{w.current_job[0]} job stuck after sleep")


# ---------------------------------------------------------------- main

def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    ap = argparse.ArgumentParser(description="Lecture Vault local app")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--wait-port", action="store_true", help="restart: wait for the previous instance to exit")
    args = ap.parse_args()
    if "pythonw" in sys.executable.lower():
        import faulthandler
        log = open(ROOT / "lecture-vault.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log
        faulthandler.enable(log)
        print(f"--- start {datetime.now():%Y-%m-%d %H:%M:%S}")
    url = f"http://127.0.0.1:{PORT}"
    if args.wait_port:
        for _ in range(60):
            if not port_in_use(PORT):
                break
            time.sleep(1)
    if port_in_use(PORT):
        if not args.no_browser:
            webbrowser.open(url)
        print(f"Lecture Vault already running at {url}")
        return
    mat.ensure_dirs(pv.CONFIG.get("modules", {}).keys())
    pv.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    study.RECALL_OFF = {c for c in pv.CONFIG.get("modules", {}) if pv.assessment(c)["exam"]}
    worker.start()
    try:
        for kind, key, payload in json.loads(worker.store.get_meta("requeue") or "[]"):
            worker.enqueue(kind, key, **payload)
    except Exception:
        traceback.print_exc()
    worker.store.set_meta("requeue", "")
    threading.Thread(target=scheduler, args=(worker,), daemon=True, name="scheduler").start()
    threading.Thread(target=watchdog, args=(worker,), daemon=True, name="watchdog").start()
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Lecture Vault running at {url}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
