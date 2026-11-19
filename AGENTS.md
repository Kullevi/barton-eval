# barton-eval: guide for agents

- **Setting Barton up for a user**: follow the "Setup (for the agent doing it)"
  section of `README.md`.
- **Running a test of a model**: follow `SKILL.md`.
- **Changing Barton**: `barton.py` (Python standard library only, Python 3.9+,
  Windows, macOS and Linux) and `report.html` (no libraries, works from
  `file://`). Keep it small. Start every subprocess through `sp_run`, so Windows
  runs stay windowless. Run `python -m unittest discover -s tests` before
  committing. The reasons behind the design are in `docs/DESIGN.md`; update it
  when a choice changes.
