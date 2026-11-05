# Lessons for this repo

One line per lesson, written by the idleflow retro agent, read by the planner at the start of every run.
Prune monthly: delete anything that stopped being true, merge near-duplicates. Keep under 40 lines —
past that the planner skims and the file stops working.

Format: `- [<run-id>] <lesson>  (seen Nx)`

<!-- lessons start -->
- [20261105-161508-bytes-rendering] A fake-fetch test (a fake statement or cursor whose fetch raises ValueError, asserting the sanitized "the statement ran" text) would have caught 2 review findings (runs 20261105-143226-statement-classification, 20261105-161508-bytes-rendering); add it to the repo's gate or lint.  (seen 2x)
