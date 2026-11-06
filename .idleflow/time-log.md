# Time log

How long each idleflow task took against its estimate, and why, one row per task, appended by the
retro after each run. The planner reads the latest rows to estimate the next plan from this repo's
own history. Times come from `run.json` (task start to report, epoch seconds); tokens only when the
harness reported them, otherwise `unknown`. Rows are not rewritten afterwards.

| Date | Run | Task | Model | Lines est / actual | Minutes est / actual | Tokens | Why they differ |
|---|---|---|---|---|---|---|---|
| 2026-11-05 | 20261105-161508-bytes-rendering | T1 | opus | 150 / 176 | 40 / 13.0 | unknown | one retry for the NUL-char rendering and the unowned tests/test_server.py fake; the 176 includes the retry's 25 lines |
| 2026-11-05 | 20261105-161508-bytes-rendering | T2 | opus | 140 / 150 | 40 / 10.3 | unknown | no retry; two fixtures gained attributes and the fakes now refuse what asyncpg refuses, adding 10 lines over estimate |
| 2026-11-05 | 20261105-173159-config-writes | T1 | opus | 265 / 441 | 25 / 13.6 | unknown | no retry; 287 of the 441 lines are tests (extra tests disclosed in the report), and the run finished 11 minutes under estimate |
| 2026-11-05 | 20261105-173159-config-writes | T2 | sonnet | 205 / 796 | 18 / 25.0 | unknown | one retry (T2-correct rejected an unreworded hint, a prompt untested under the lock, and an OSError before validation); the retry added 378 lines, so the diff ran 3.6x estimate |
| 2026-11-06 | 20261106-113416-cursor-leak-diagnostics | T1 | sonnet | 80 / 165 | 15 / 19.0 | 118501 | one retry for a concurrent fetch that destroyed the in-flight cursor and a surviving mutant; the overlap guard and its tests sat outside the criteria and roughly doubled the diff |
| 2026-11-06 | 20261106-113416-cursor-leak-diagnostics | T2 | sonnet | 85 / 176 | 20 / 39.3 | 235763 | two retries: the commit subject, then the Neon and Supavisor SSL advice lost and five surviving mutants missing from the report; a third SSL marker beyond the pinned list added lines |
