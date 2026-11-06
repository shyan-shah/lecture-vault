# Lecture Vault

Go to lectures, pay attention, and let this write the notes.

Lecture Vault runs on your laptop and watches Panopto. When a lecture recording appears it pulls the captions (or transcribes the audio itself), reads that week's slides, notes pack, tutorial sheets and coursework brief from Moodle, and has Claude write first-class revision notes that compare what was said with what is on the slides. Everything shows up in a calendar app in your browser.

* **Week view:** your timetable, each lecture coloured by state (noted, waiting for the recording, not recorded, upcoming). Click one for its notes, the lecture vs slides comparison, linked tutorial questions and a link back to the recording.
* **Today:** what to work on next, your open tutorial sheets with a question grid to mark, coursework milestones and module priorities.
* **Review:** for exam modules, every tutorial, past paper and worked-example question as practice cards with the official method and the lecture that explains it. Mark Right / Half / Wrong and missed questions come back in a couple of days.
* **Modules:** each module's lectures, Moodle files, weekly plan and Anki export.
* **Moodle sync:** works out how each module's Moodle page is laid out and downloads slides, packs, tutorial sheets (with solutions), past papers and coursework briefs into the right folders. Scanned handwritten worked examples are read too.

## What you need

* Windows 10 or 11 (it also runs on a Mac, see below)
* [Python 3.11 or newer](https://www.python.org/downloads/): tick **Add python.exe to PATH** in the installer
* Microsoft Edge (already on every Windows laptop): used for the university sign-in
* A Claude API key with some credit, from [console.anthropic.com](https://console.anthropic.com) (API keys, then Billing). This is separate from a Claude.ai subscription. You pay per lecture processed; check Usage in the console after your first week to see what it costs you
* Your university login (Panopto and Moodle). Defaults are set up for the University of Bath
* `prompts.py`, which you get from Shyan (see Install)

## Install (about 10 minutes)

1. **Get the code.** On this page press **Code > Download ZIP** and unzip it somewhere permanent (for example `Documents\lecture-vault`), or `git clone` it. Then put **`prompts.py`** in that folder, next to `app.py`. It is not on GitHub: get it from Shyan directly. Without it the app opens but cannot write notes.
2. **Run the installer.** Open the folder, click the address bar, type `powershell` and press Enter. Then run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```
   It creates a private Python environment, installs everything, then opens the **setup wizard**, which asks for:
   * your Claude API key
   * one line about you (for example `Year 3 MEng Aerospace Engineering student at the University of Bath`), so the notes pitch at your level
   * your timetable iCal link (Bath: MyTimetable > Subscribe, copy the link). Optional but it is what puts lectures on the calendar
   * the Monday of teaching week 1
   * your modules: code, name, and whether each has an exam, coursework and weekly tutorial sheets. This decides what the notes focus on: exam modules get tutorial guidance and practice questions, coursework-only modules get everything tied back to the brief
   It finishes by offering to start Lecture Vault every time you log in, which is what makes notes appear without you doing anything.
3. **Sign in.** Open **Lecture Vault** from the desktop shortcut. Press **Sign in**, log in with your university account in the browser window that appears (tick "stay signed in"), and do the same if Moodle asks. That's it.

The first sync looks back two weeks, so you get notes for recent lectures straight away. To change modules or keys later, run `.venv\Scripts\python.exe setup_wizard.py` again.

## Daily use

Nothing. Every 20 minutes the app checks Panopto. When a recording has captions (usually within a few hours) it writes and files the notes, and you get a Windows notification. If your login expires you get a notification asking you to sign in again. The laptop needs to be on and awake; anything missed while it was asleep is caught up when it wakes.

## Course files

Moodle files are downloaded for you. You can also drop files in yourself; **Open materials folder** in the Modules view takes you there.

| Where | Treated as |
|---|---|
| `materials/<module>/` | Slides or handouts for one lecture, matched by content, lecture number or date in the name |
| `materials/<module>/pack/` | A notes pack covering many lectures: the right page range is found for each lecture |
| `materials/<module>/questions/` | Tutorial sheets, solutions, past papers, worked examples |
| `materials/<module>/info/` | Handbooks and coursework briefs |

PDF, PowerPoint, Word, text and Markdown all work.

## Chatting with your notes in Claude

Every lecture's notes are saved as Markdown in `notes/<module>/` (they open nicely in Obsidian too). For a study chat per module: in Claude, create a Project for the module and upload that module's notes folder plus the tutorial sheets and coursework brief from `materials/<module>/`. Re-upload new notes every week or so. `module-chats.md` has instructions you can paste into each Project.

## Privacy

Your API key, login session, database, notes and downloaded course files stay on your laptop. They are all listed in `.gitignore`, so they can never be committed. The only things that leave your machine are the lecture text and course files sent to Claude to write the notes. Recordings and course materials are for your own study: don't share transcripts or downloaded files.

## Updating

Download the new ZIP over the old folder (your `.env`, `config.json`, `prompts.py`, notes and database are kept), or `git pull`. Then run `install.ps1` again to pick up any new packages. If Shyan sends you a new `prompts.py`, replace the old one and restart the app.

## Troubleshooting

* **Sign-in window doesn't appear:** end `pythonw.exe` in Task Manager, open Lecture Vault again from the shortcut, then press Sign in.
* **A lecture says "Not recorded" but it was:** recordings appear on Panopto a while after the lecture; wait for the next sync or press **Sync now**.
* **Lectures Panopto never captioned:** run `install.ps1` again and say yes to local transcription.
* **"prompts.py is missing":** put the `prompts.py` file Shyan sent you next to `app.py` and restart.
* **Logs:** `lecture-vault.log` in the app folder.

## Mac

Not packaged, but it runs. In Terminal from the folder:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && playwright install chromium   # and put prompts.py in the folder
python setup_wizard.py
python app.py
```
Leave the Terminal window open. There are no notifications or auto-start on a Mac.

## Licence

Copyright (c) 2026 Shyan Shah. All rights reserved. You may run it for your own study if you have been given access; you may not copy, modify, redistribute or build on it. See `LICENSE`. `prompts.py` is confidential: don't share it.

## Advanced: Supabase sync

The app can also file everything into a Supabase database (notes, materials, tracked work, priorities) for use from other tools. Run `migration.sql` in your Supabase project and give the wizard your project URL and service_role key. Leave it blank and everything stays local.
