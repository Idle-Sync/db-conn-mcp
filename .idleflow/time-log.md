# Time log

How long each idleflow task took against its estimate, and why, one row per task, appended by the
retro after each run. The planner reads the latest rows to estimate the next plan from this repo's
own history. Times come from `run.json` (task start to report, epoch seconds); tokens only when the
harness reported them, otherwise `unknown`. Rows are not rewritten afterwards.

| Date | Run | Task | Model | Lines est / actual | Minutes est / actual | Tokens | Why they differ |
|---|---|---|---|---|---|---|---|
| 2026-11-05 | 20261105-161508-bytes-rendering | T1 | opus | 150 / 176 | 40 / 13.0 | unknown | one retry for the NUL-char rendering and the unowned tests/test_server.py fake; the 176 includes the retry's 25 lines |
| 2026-11-05 | 20261105-161508-bytes-rendering | T2 | opus | 140 / 150 | 40 / 10.3 | unknown | no retry; two fixtures gained attributes and the fakes now refuse what asyncpg refuses, adding 10 lines over estimate |
