# Lessons for this repo

One line per lesson, written by the idleflow retro agent, read by the planner at the start of every run.
Prune monthly: delete anything that stopped being true, merge near-duplicates. Keep under 40 lines —
past that the planner skims and the file stops working.

Format: `- [<run-id>] <lesson>  (seen Nx)`

<!-- lessons start -->
- [20261105-161508-bytes-rendering] A fake-fetch test (a fake statement or cursor whose fetch raises ValueError, asserting the sanitized "the statement ran" text) would have caught 2 review findings (runs 20261105-143226-statement-classification, 20261105-161508-bytes-rendering); add it to the repo's gate or lint.  (seen 2x)
- [20261106-182012-false-success-reports] A mutation sweep over the changed files would have caught 13 review findings (runs 20261105-161508-bytes-rendering, 20261105-173159-config-writes, 20261106-113416-cursor-leak-diagnostics, 20261106-125455-fetch-size-clamp, 20261106-182012-false-success-reports); add it to the repo's gate or lint.  (seen 5x)
- [20261106-141820-windows-config-write-flake] Windows-only failures here (os.replace, msvcrt, posix_only tests) show only in the windows-latest CI job; macOS runs prove nothing about them, so plan a step that records the CI log before any Windows fix  (seen 2x: 20261105-173159-config-writes, this run).
