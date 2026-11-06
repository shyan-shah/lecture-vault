"""
Loads prompts.py, which holds every instruction the app gives Claude.

prompts.py is private and not part of this repository. Get it from the person who shared Lecture Vault with you
and put it in the app folder, next to app.py. Without it the app still opens, but cannot write notes.
"""
from __future__ import annotations

MISSING_MESSAGE = ("prompts.py is missing. Ask the person who shared Lecture Vault with you for it and put it in the "
                   "app folder next to app.py, then restart the app.")

try:
    import prompts as P  # noqa: F401
    MISSING = False
except ImportError:
    MISSING = True

    class _Missing:
        def __getattr__(self, name):
            raise RuntimeError(MISSING_MESSAGE)

    P = _Missing()
