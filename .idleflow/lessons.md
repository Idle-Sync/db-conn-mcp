# Lessons for this repo

One line per lesson, written by the idleflow retro agent, read by the planner at the start of every run.
Prune monthly: delete anything that stopped being true, merge near-duplicates. Keep under 40 lines —
past that the planner skims and the file stops working.

Format: `- [<run-id>] <lesson>  (seen Nx)`

<!-- lessons start -->
- [20261105-161508-bytes-rendering] A fake-fetch test (a fake statement or cursor whose fetch raises ValueError, asserting the sanitized "the statement ran" text) would have caught 2 review findings (runs 20261105-143226-statement-classification, 20261105-161508-bytes-rendering); add it to the repo's gate or lint.  (seen 2x)
- [20261108-204913-audit-client-injections] A mutation sweep over the changed files would have caught 16 review findings (runs 20261105-161508-bytes-rendering, 20261105-173159-config-writes, 20261106-113416-cursor-leak-diagnostics, 20261106-125455-fetch-size-clamp, 20261106-182012-false-success-reports, 20261107-123442-css-test-line-endings, 20261108-120742-windows-config-save, 20261108-204913-audit-client-injections); add it to the repo's gate or lint.  (seen 8x)
- [20261108-194518-lock-sidecar-and-add-oserror] Checks that only a CI job can run here (Windows-only paths: os.replace, msvcrt, posix_only tests; live Postgres tests, since no server runs locally and Docker is off limits) show only in that job; local runs prove nothing about them, so plan a step that records the CI job log before any fix or merge  (seen 5x: 20261105-173159-config-writes, 20261106-141820-windows-config-write-flake, 20261108-110653-composite-key-columns, 20261108-124636-config-audit-log, 20261108-194518-lock-sidecar-and-add-oserror).
- [20261108-115310-setup-config-path] A commit-msg check that the subject starts with `T<n>: ` would have caught 7 review findings (runs 20261106-113416-cursor-leak-diagnostics, 20261107-114257-false-success-review-findings, 20261108-115310-setup-config-path); add it to the repo's gate or lint.  (seen 3x)
- [20261108-113553-partitioned-fk-columns] Task worktrees have no .venv (seen in 4 runs), and the main checkout's .venv is an editable install of the main src, so a test run through it imports the base code and reports a pass for the unchanged worktree; plan test commands as `PYTHONPATH=<worktree>/src .venv/bin/python -m pytest ...` run from the worktree, and assert `db_conn_mcp.__file__` is under the worktree.
