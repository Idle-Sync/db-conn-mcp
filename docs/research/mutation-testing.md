# mutation testing — library options
researched: 2026-11-10   for: CI: mutation check on the lines a PR changes (#77)   status: decided: hand-written

## What we need
Mutate only the `src/` lines a PR changes, run the tests against each mutant, fail on a surviving mutant, allow an equivalent-mutant marker with a reason, run the same command locally, and stay near the runtime of the existing test jobs. Python 3.12-3.14, Linux CI, macOS and Windows developers; dependencies only in `pyproject.toml` (CLAUDE.md rule 4); mutants must not be hidden by stale `.pyc`.

## Already in the repo
Nothing covers this. No mutation tool, no `scripts/` directory, no diff-scoping code (`pyproject.toml`, `.github/workflows/ci.yml`). `docs/research/` held no earlier file. No stack decision in `.idleflow/conventions.md` accepts or declines a library here.

## Options
### mutmut
what: fork-based mutation tester; mutates functions in `source_paths` inside a `mutants/` copy and runs only the tests that exercise each function.
install: `pip install mutmut`   source: https://raw.githubusercontent.com/boxed/mutmut/main/README.rst   (seen 2026-11-10)
maintained: yes — 3.8.1 released 2026-10-28, BSD-3-Clause, requires Python >=3.10, 3.14 classifier listed   source: https://pypi.org/project/mutmut/ and https://pypi.org/pypi/mutmut/json (2026-11-10); repo shows 61 open issues, 23 open PRs   source: https://github.com/boxed/mutmut (2026-11-10)
adoption: "most actively maintained" with cosmic-ray, per a 2026 paper and a March 2026 comparison page (competitor-authored)   source: https://pytest-gremlins.readthedocs.io/en/latest/guide/comparison/ (2026-11-10). No download figure verified.
fits: scoping is by file, function glob (`mutmut run "module.func*"`) or `mutate_only_covered_lines`; no diff or line scoping, so a filter step from `git diff` is needed to build `only_mutate` / function names. `# pragma: no mutate` (line, block, start/end) marks equivalents but has no reason field; a reason would be a plain comment beside it.
pros:
- pyproject.toml config (`[tool.mutmut]`), `# pragma: no mutate` built in
- runs only tests that reach the mutated function
- mutated code lives in a separate `mutants/` tree, not in `src/`
cons:
- needs `fork()`: no native Windows, WSL only (local runs on Windows developers' machines)
- function-level granularity, no line scoping from a diff; module-level code outside functions is not mutated in 3.x (README note on 2.x for that case)
- `libcst` dependency may need a Rust toolchain on some architectures (README); `mutants/` is not in `.gitignore`
- bytecode handling is not documented
sources:
- https://raw.githubusercontent.com/boxed/mutmut/main/README.rst — docs — 2026-11-10
- https://github.com/boxed/mutmut — repo — 2026-11-10
- https://pypi.org/project/mutmut/ — registry — 2026-11-10
- https://pytest-gremlins.readthedocs.io/en/latest/guide/comparison/ — independent (vendor of a competing tool; verified as of March 2026) — 2026-11-10

### cosmic-ray
what: mutation tester that mutates the AST through an import hook and keeps results in a session database.
install: no install command verified (landing page and README did not state one; PyPI package name is `cosmic-ray`)   source: https://cosmic-ray.readthedocs.io/en/latest/
maintained: yes — 8.8.0 uploaded 2026-11-08, MIT, Python >=3.9, classifiers 3.9-3.13 (no 3.14 classifier), 50 open issues   source: https://pypi.org/pypi/cosmic-ray/json and https://github.com/sixty-north/cosmic-ray (2026-11-10)
adoption: named as one of the two most actively maintained Python tools in a 2026 paper (https://arxiv.org/pdf/2608.15188); no download figure verified.
fits: operator control and a session database; no diff scoping verified (a git-based filter was not found in the docs I could reach); equivalent-mutant skipping via a pragma filter was not verified. Config and per-mutant test command are not verified.
pros:
- import hook: the mutated AST is compiled in memory, so no mutated file is written to `src/`
- runs on all platforms per the comparison page
- selectable mutation operators
cons:
- 3.14 not in classifiers (matrix includes 3.14)
- dependency set is large: aiohttp, sqlalchemy, gitpython, parso, rich, stevedore, and others
- session database and config-file workflow add setup (init, exec, report steps)
- `.pyc` handling not documented
sources:
- https://pypi.org/pypi/cosmic-ray/json — registry — 2026-11-10
- https://github.com/sixty-north/cosmic-ray — repo — 2026-11-10
- https://cosmic-ray.readthedocs.io/en/latest/ — docs (landing page only) — 2026-11-10
- https://pytest-gremlins.readthedocs.io/en/latest/guide/comparison/ — independent (competitor-authored) — 2026-11-10

Disagreement between sources: the GitHub releases page shows v8.8.0 as "08 Nov" while its changelog dates it 2026-10-15; PyPI upload is 2026-11-08.

## Hand-written alternative
A script of roughly 150-200 lines, stdlib only (`git diff -U0 <base>...HEAD -- src/` to get changed line ranges, `ast` to find mutable nodes on those lines, a short operator table: flip comparisons, swap `and`/`or`, `True`/`False`, `+`/`-`, constants, string literals, return values). For each mutant: copy `src/` to a temp dir, write the one mutated file there, run `python -B -m pytest -x -q` with `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH=<temp>/src` on the tests mapped to that module. This gives true line scoping, no `.pyc` problem, a `# mutation-ok: <reason>` marker with a mandatory reason, and Windows support. Upkeep: the operator table and the test-to-module map; roughly a day to build plus tests for the script itself (it must pass ruff and carry docstrings). Cost of running: one pytest invocation per mutant, so runtime scales with changed lines; a cap on mutants per run is needed.

Diff parsing needs no library: `git diff -U0` hunk headers (`@@ -a,b +c,d @@`) give changed ranges in a dozen lines. `actions/checkout@v6` needs `fetch-depth: 0` or a fetched base for `origin/main...HEAD`.
