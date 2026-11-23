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

Both Cycle ID copy controls use the same browser helper in `static/cycles.js`: prefer the Clipboard API, then use a selected temporary textarea with `execCommand('copy')` when the API is unavailable (including LAN HTTP) or rejects the write. The temporary field is removed and focus restored even on failure. The workbench announces success or a manual-copy fallback through a fixed bottom status notice that stays visible while scrolling. Success lasts three seconds, failure eight seconds; each result replaces the previous dismissal timer. It never reports success when both copy methods fail.

The Cycle report shell reads the latest MR presentation through a same-origin Cycle-scoped read endpoint. Its file list, summary and source rows share one issued snapshot; presentation changes invalidate selections and in-flight details. Formal Build percentage and report time are separately labeled, and mapped MR counts never acquire the old compiled denominator. Non-MR Cycles keep formal report rendering. Closed/merged MRs and temporarily unavailable projections also preserve formal report browsing via an independent `/report` read with an explicit fallback notice. Expired details join any active refresh and retry the requested file once, including same-ID recreation, without overriding a newer click. Browser regressions: `tests/web/cycle_report.test.js`.

Cycles without a successful report can render a verified `diff_only` snapshot with `report: null`. The page labels absent packages separately from pending/failed Build analysis, leaves coverage percentage/report time empty, and never claims Build evidence. Summary refresh and detail binding tolerate the later transition to a first formal report.

Mutable Build metadata is rendered on every successful summary refresh independently of presentation identity. First package registration can change `build_count` without changing the diff snapshot; it updates the no-package/no-report wording without clearing selected files or invalidating pending details.
