# Web module

## Responsibility

Render the data-free authentication, authenticated Cycle workbench, admin user management, Cycle report, and Safari probe shells from trusted templates and separated CSS/JavaScript. Escape the small probe metadata projection and validate canonical Cycle IDs before embedding them as JSON.

## Non-responsibility

No SQLite, password hashing, Token file, Mach-O, dSYM, profraw, Git object, analysis-result path, arbitrary file, or GitLab access. Browser code obtains data only from same-origin APIs and never reads or stores machine credentials or the HttpOnly Session.

## Entry files

- `ioscoverage/web/assets.py`: fixed-name template/static loader.
- `pages/login.py`, `pages/cycles.py`, `pages/users.py`, `pages/report.py`, `pages/probe.py`: bounded projection functions.
- `templates/*.html`, `static/*.css`, `static/*.js`: presentation assets.

The server routes call these renderers. `/login/` defaults to login and switches to registration without showing both forms; the existing registration response sets the Session Cookie before navigation to `/cycles/`. The workbench has no authentication forms and is served only to an active Web Session; expired sessions return to `/login/`. Server-provided usernames and fields are assigned through `textContent`; password inputs are cleared after requests or mode changes. Tests are in `tests/web/` and the page-related server tests.
