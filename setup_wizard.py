"""
Lecture Vault setup wizard. Run it once after installing, and again any time to change modules or keys:

    python setup_wizard.py

It writes two files next to this one, both private to your machine (and ignored by Git):
  .env         your API key and settings
  config.json  your modules and how each is assessed
Nothing is uploaded anywhere.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
CONFIG = ROOT / "config.json"

DEFAULTS = {
    "PANOPTO_HOST": "https://uniofbath.cloud.panopto.eu",
    "MOODLE_HOST": "https://moodle.bath.ac.uk",
    "NOTES_MODEL": "claude-sonnet-5-5",
    "AUTO_TRANSCRIBE": "1",
    "AUTO_SYNC_MINUTES": "20",
    "AUTO_SINCE_DAYS": "14",
    "CAPTION_GRACE_HOURS": "6",
    "NOTIFY": "1",
    "APP_PORT": "8765",
    "WHISPER_MODEL": "small.en",
    "STUDY_HOURS_PER_WEEK": "20",
}
ORDER = ["ANTHROPIC_API_KEY", "NOTES_MODEL", "STUDENT_PROFILE", "PANOPTO_HOST", "MOODLE_HOST", "TIMETABLE_ICS_URL",
         "STUDY_HOURS_PER_WEEK", "AUTO_TRANSCRIBE", "AUTO_SYNC_MINUTES", "AUTO_SINCE_DAYS", "CAPTION_GRACE_HOURS",
         "NOTIFY", "APP_PORT", "WHISPER_MODEL", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "VAULT_USER_ID"]


# ---------------------------------------------------------------- small helpers

def read_env() -> dict:
    out = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.split(" #")[0].strip()
    return out


def write_env(env: dict) -> None:
    keys = ORDER + sorted(k for k in env if k not in ORDER)
    lines = ["# Lecture Vault settings. Written by setup_wizard.py; safe to edit. Keep this file private."]
    for k in keys:
        if env.get(k) not in (None, ""):
            lines.append(f"{k}={env[k]}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ask(prompt: str, default: str = "", secret: bool = False, required: bool = False) -> str:
    shown = ("(saved)" if secret else default) if default else ""
    while True:
        v = input(f"{prompt}{f' [{shown}]' if shown else ''}: ").strip()
        if not v:
            v = default
        if v or not required:
            return v
        print("  This one is needed.")


def yes(prompt: str, default: bool) -> bool:
    v = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not v else v.startswith("y")


def check_key(key: str) -> str | None:
    """None if the key works, else a short reason."""
    try:
        import anthropic
        anthropic.Anthropic(api_key=key).models.list(limit=1)
        return None
    except ImportError:
        return "the anthropic package isn't installed yet (run install first); key saved unchecked"
    except Exception as e:
        return str(e).splitlines()[0][:160]


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------- wizard

def wizard(env: dict, cfg: dict) -> tuple[dict, dict]:
    print(__doc__.strip().split("\n\n")[0])
    print("\nPress Enter to keep the value in [brackets].\n")

    print("1. Claude API key (pays for the notes). Get one at https://console.anthropic.com > API keys.")
    key = ask("   ANTHROPIC_API_KEY", env.get("ANTHROPIC_API_KEY", ""), secret=True, required=True)
    problem = check_key(key)
    print(f"   {'Key works.' if not problem else 'Could not confirm the key: ' + problem}")
    env["ANTHROPIC_API_KEY"] = key

    print("\n2. Who the notes are for. One line, used in every prompt so the notes pitch at the right level.")
    env["STUDENT_PROFILE"] = ask("   e.g. 'Year 3 MEng Aerospace Engineering student at the University of Bath'",
                                 env.get("STUDENT_PROFILE", ""), required=True)

    print("\n3. University systems. Defaults are for the University of Bath.")
    env["PANOPTO_HOST"] = ask("   Panopto address", env.get("PANOPTO_HOST", DEFAULTS["PANOPTO_HOST"])).rstrip("/")
    env["MOODLE_HOST"] = ask("   Moodle address", env.get("MOODLE_HOST", DEFAULTS["MOODLE_HOST"])).rstrip("/")

    print("\n4. Timetable (optional but recommended: it puts lectures on the calendar and pairs them with recordings).")
    print("   Bath: MyTimetable > Subscribe / Export > copy the iCal (webcal or https) link. Leave blank to skip.")
    ics = ask("   iCal link", env.get("TIMETABLE_ICS_URL", ""))
    env["TIMETABLE_ICS_URL"] = ics.replace("webcal://", "https://")

    print("\n5. Semester start: the Monday of teaching week 1 (used to work out week numbers).")
    cur = cfg.get("semester_start") or monday_of(date.today()).isoformat()
    while True:
        v = ask("   YYYY-MM-DD", cur)
        try:
            cfg["semester_start"] = monday_of(date.fromisoformat(v)).isoformat()
            break
        except ValueError:
            print("  Use the form 2026-09-28.")

    print("\n6. Modules. For each one: its code as it appears on Panopto/Moodle, its name, and how it is assessed.")
    mods: dict = cfg.get("modules") or {}
    if mods:
        print("   Current: " + ", ".join(f"{c} {m.get('name', '')}" for c, m in mods.items()))
        if not yes("   Keep these?", True):
            for code in list(mods):
                if not yes(f"   Keep {code} {mods[code].get('name', '')}?", True):
                    mods.pop(code)
    while yes("   Add a module?", not mods):
        code = re.sub(r"\s+", "", ask("   Module code (e.g. ME32029)", required=True)).upper()
        name = ask("   Module name (e.g. Heat Transfer)", mods.get(code, {}).get("name", ""), required=True)
        lecturer = ask("   Lecturer (optional)", mods.get(code, {}).get("lecturer", ""))
        exam = yes("   Assessed by an exam?", True)
        cw = yes("   Has coursework?", not exam)
        tut = yes("   Weekly tutorial / problem sheets?", exam)
        old = mods.get(code, {})
        mods[code] = {**old, "name": name, "lecturer": lecturer,
                      "aliases": old.get("aliases") or [name],
                      "assessment": {"exam": exam, "coursework": cw, "weekly_tutorials": tut}}
        print(f"   Added {code}.")
    cfg["modules"] = mods
    cfg.setdefault("watch_folders", [])

    print("\n7. Weekly study hours you want the planner to work with.")
    env["STUDY_HOURS_PER_WEEK"] = ask("   Hours", env.get("STUDY_HOURS_PER_WEEK", DEFAULTS["STUDY_HOURS_PER_WEEK"]))

    print("\n8. Supabase sync (advanced, optional). Only if you run your own Supabase with the schema in migration.sql.")
    if yes("   Set up Supabase?", bool(env.get("SUPABASE_URL"))):
        env["SUPABASE_URL"] = ask("   SUPABASE_URL", env.get("SUPABASE_URL", ""))
        env["SUPABASE_SERVICE_KEY"] = ask("   SUPABASE_SERVICE_KEY (service_role)", env.get("SUPABASE_SERVICE_KEY", ""), secret=True)
    else:
        for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "VAULT_USER_ID"):
            env.pop(k, None)
    return env, cfg


def main() -> None:
    env = {**DEFAULTS, **read_env()}
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    if len(sys.argv) > 2 and sys.argv[1] == "--from":  # non-interactive: python setup_wizard.py --from answers.json
        answers = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        env.update(answers.get("env", {}))
        cfg.update(answers.get("config", {}))
    else:
        try:
            env, cfg = wizard(env, cfg)
        except (KeyboardInterrupt, EOFError):
            print("\nStopped. Nothing was changed.")
            return
    write_env(env)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    for code in cfg.get("modules", {}):
        for sub in ("", "pack", "questions", "info"):
            (ROOT / "materials" / code / sub).mkdir(parents=True, exist_ok=True)
    print(f"\nSaved {ENV.name} and {CONFIG.name} with {len(cfg.get('modules', {}))} module(s).")
    print("Next: start the app (start.bat, or the desktop shortcut) and press 'Sign in'.")


if __name__ == "__main__":
    main()
