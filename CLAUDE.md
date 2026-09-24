# VidEditor

Desktop non-linear video editor. Python 3.12 + PySide6 + bundled FFmpeg. Offline only.

Full phase plan is in PHASES.md. We work through it one phase at a time.
Do not work ahead of the phase I give you.

## Hard invariants

1. `core/` must NEVER import PySide6 or anything from `ui/`. One-way dependency only.
2. ALL time values are integer ticks. `TICKS_PER_SECOND = 120000`.
   The only floats in the codebase are `gain_db`, `pixels_per_second`, and values
   being formatted for display or for an FFmpeg argument string.
3. Frame rates are stored as a rational `FrameRate(num, den)`. Never a float.
4. FFmpeg binaries resolve through `core/binaries.py`. Never rely on system PATH.
5. Undo commands are pushed once per completed gesture, on mouse release.
   Never one per mouse-move event.
6. Long-running work never runs on the GUI thread.

## Environment

Python 3.12, venv at `.venv`.
PySide6 6.11.2, pydantic 2.13.5, numpy 2.5.2, Pillow 12.3.0, pytest 9.1.1.
FFmpeg 2026-07-16 gyan.dev essentials build, vendored at `bin/`.
Not committed to git, see .gitignore.

## Commands

.venv\Scripts\activate
pytest                      # all tests
pytest -m "not slow"        # skip ffmpeg integration tests
python app.py               # run the app, from Phase 2 onward

## Git

Commits are authored by me alone. Never add a Co-Authored-By trailer, a
"Generated with" line, or any AI tool attribution to a commit message.
Commit messages are subject line only.

## Working agreement

Stop at the end of each phase. Do not start the next one.
If a phase instruction conflicts with an invariant above, tell me instead of choosing.