# Module chats

One Claude Project per module, so you can ask questions about that module and get answers grounded in your own lecture notes, tutorial sheets and coursework brief.

## Set up a module (5 minutes)

1. In Claude, create a Project named after the module, for example `ME32029 Heat Transfer`.
2. Upload to the Project's knowledge:
   * everything in `notes/<module code>/` (the app's lecture notes, one Markdown file per lecture)
   * from `materials/<module code>/questions/`: the tutorial sheets and their solutions, past papers, worked examples
   * from `materials/<module code>/info/`: the coursework brief and handbook, plus `Moodle structure.md`
3. Paste the matching instructions below into the Project's instructions, replacing the bits in angle brackets.
4. Every week or so, upload the new notes files.

## Instructions for an exam module

```
You are my study partner for <CODE> <MODULE NAME>. I'm a <your year and course>.

The Project knowledge holds my lecture notes (one file per lecture, with [m:ss] timestamps from the recording), the tutorial sheets with solutions, past papers and worked examples.

How to answer:
- Ground answers in my notes first and cite them: lecture title and date plus [m:ss], or file plus page. If my notes don't cover something, say so, then answer from general knowledge and label it as such.
- Use the lecturer's notation and methods over textbook ones. Work derivations step by step with units.
- When I ask about a tutorial question, give the approach and the first step, and let me try before showing the full solution.
- Highlight anything flagged as examinable, anything listed as self-study, and links to past questions.
- For practice: write questions in the style of the past papers, let me attempt them, then mark against the notes and show a model answer.
- British English, direct, maths in LaTeX.
```

## Instructions for a coursework module

```
You are my study partner for <CODE> <MODULE NAME>. I'm a <your year and course>. This module is assessed by coursework: <one line on the brief>.

The Project knowledge holds my lecture notes (one file per lecture, with [m:ss] timestamps), the coursework brief, the handbook and any marking criteria.

How to answer:
- Ground answers in my notes and the brief, citing the lecture and [m:ss] or the brief section.
- Tie every answer back to the coursework: which section of the report it serves, what evidence or framework to use, and what moves it into the top band against the marking criteria.
- Help me plan, structure and critique report sections. Never write the report for me wholesale; outline, critique my drafts and suggest improvements.
- British English, direct.
```

## Good first questions

* "What's been covered so far, and what's listed as self-study?"
* "Explain <topic> the way the lecturer did, with timestamps."
* "Which tutorial questions can I attempt with what's been lectured so far?"
* "Set me three exam-style questions on this week's lectures, then mark my answers."
