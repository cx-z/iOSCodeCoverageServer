# Web module

## Responsibility

Render the data-free Cycle list, Cycle report, and Safari probe shells from trusted templates and separated CSS/JavaScript. Escape the small probe metadata projection and validate canonical Cycle IDs before embedding them as JSON.

## Non-responsibility

No SQLite, Token file, Mach-O, dSYM, profraw, Git object, analysis-result path, arbitrary file, or GitLab access. Browser code obtains data only from same-origin protected APIs.

## Entry files

- `ioscoverage/web/assets.py`: fixed-name template/static loader.
- `pages/cycles.py`, `pages/report.py`, `pages/probe.py`: bounded projection functions.
- `templates/*.html`, `static/*.css`, `static/*.js`: presentation assets.

The server routes call these renderers. Tests are in `tests/web/test_report_ui.py` and the page-related server tests.
