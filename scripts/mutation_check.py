"""Mutation check on the ``src/`` lines a branch changes.

Usage, from anywhere inside the repository::

    python scripts/mutation_check.py [--base <ref>] [--max-mutants <N>]

A changed line is a new-side line of the diff from the merge base to the working tree, or
any line of a ``.py`` file under ``src/`` that git does not track yet. Every expression on
a changed line is mutated one fault at a time (a flipped comparison, ``and``/``or``
swapped, ``True``/``False`` swapped, a dropped ``not``, ``+``/``-`` swapped, a number plus
one, a string replaced) and the module's mapped tests run against each mutant. A mutant
the tests still pass on has survived: the changed line has no test that tells the fault
from the code. Each mutant runs on a fresh copy of ``src/`` with bytecode writing off, so
a stale ``.pyc`` can never stand in for the mutated file.

A comment ``# mutation-ok: <reason>`` on a line of an expression marks its mutants as
equivalent; they are reported, not run.

Exit codes: 0 no survivor, 1 a survivor or mutants left unrun past ``--max-mutants``,
2 the check could not run.
"""

import argparse
import ast
import copy
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = "src/db_conn_mcp/"

#: A mutant run gets at least this long, however fast its baseline was.
MINIMUM_TIME_LIMIT_SECONDS = 60.0

#: A mutant run gets this many times its module's baseline test time.
BASELINE_TIME_MULTIPLE = 3

#: The test files that exercise each module, keyed by path under ``src/db_conn_mcp/``.
#: A new module needs an entry before a change to it can be checked.
TESTS_FOR_MODULE: dict[str, list[str]] = {
    "__init__.py": ["test_smoke.py", "test_cli.py"],
    "__main__.py": ["test_smoke.py", "test_cli.py"],
    "atomic_write.py": ["test_atomic_write.py"],
    "audit.py": ["test_audit.py"],
    "cli.py": ["test_cli.py"],
    "clients.py": ["test_clients_codex.py", "test_cli.py", "test_gui_app.py"],
    "config.py": ["test_config.py"],
    "diagnostics.py": ["test_diagnostics.py"],
    "doctor.py": ["test_doctor.py"],
    "guard.py": ["test_guard.py"],
    "handlers.py": ["test_handlers_dry_run.py", "test_server.py"],
    "models.py": ["test_config.py", "test_safety.py", "test_smoke.py"],
    "safety.py": ["test_safety.py", "test_handlers_dry_run.py"],
    "server.py": ["test_server.py", "test_gui_lifecycle.py"],
    "update_check.py": ["test_update_check.py"],
    "verify.py": ["test_verify.py"],
    "dialects/__init__.py": ["test_postgres.py", "test_smoke.py"],
    "dialects/base.py": ["test_postgres.py", "test_smoke.py"],
    "dialects/registry.py": ["test_postgres.py", "test_smoke.py"],
    "dialects/postgres.py": ["test_postgres.py", "test_postgres_live.py"],
    "gui/__init__.py": ["test_gui_app.py", "test_gui_lifecycle.py"],
    "gui/app.py": ["test_gui_app.py", "test_gui_lifecycle.py"],
}

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

MARKER = re.compile(r"#\s*mutation-ok:(.*)")

FLIPPED_COMPARISONS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}

OPERATOR_SYMBOLS: dict[type[ast.AST], str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.GtE: ">=",
    ast.Gt: ">",
    ast.LtE: "<=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.And: "and",
    ast.Or: "or",
    ast.Add: "+",
    ast.Sub: "-",
}


class CheckError(Exception):
    """The check could not run; the message says why. ``main`` turns it into exit code 2."""


@dataclass(frozen=True)
class Mutant:
    """One faulty version of a module: where the fault is, what it is, and the module text."""

    line: int
    end_line: int
    description: str
    source: str


def git(repo_root: Path, *args: str) -> str:
    """Run git in ``repo_root`` and return its output; raise ``CheckError`` if it fails."""
    # quotepath=off keeps a non-ASCII path readable instead of octal-escaped and quoted.
    command = ["git", "-c", "core.quotepath=off", *args]
    try:
        # errors="replace": a diff carries the changed lines' bytes, which need not be UTF-8;
        # the file itself is decoded later, where a failure can name it.
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise CheckError(f"could not run git: {exc}") from exc
    if completed.returncode != 0:
        raise CheckError(f"git {args[0]} failed: {completed.stderr.strip()}")
    return completed.stdout


def merge_base(repo_root: Path, base: str) -> str:
    """The commit where HEAD's branch left ``base``: the branch's changes start after it."""
    return git(repo_root, "merge-base", base, "HEAD").strip()


def parse_changed_lines(diff_text: str) -> dict[str, set[int]]:
    """New-side line numbers per file in a ``git diff -U0`` output; deleted lines are not kept."""
    changed: dict[str, set[int]] = {}
    path: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[len("+++ ") :]
            # "+++ /dev/null" is a deleted file: it has no new side.
            path = target[len("b/") :] if target.startswith("b/") else None
            continue
        match = HUNK_HEADER.match(line)
        if match is None or path is None:
            continue
        start = int(match.group(1))
        # "+c" with no count is one line; "+c,0" is a hunk that only deletes.
        count = 1 if match.group(2) is None else int(match.group(2))
        if count == 0:
            continue
        changed.setdefault(path, set()).update(range(start, start + count))
    return changed


def changed_lines(repo_root: Path, base: str) -> dict[str, set[int]]:
    """Lines under ``src/`` changed since the merge base, uncommitted edits included.

    A ``.py`` file git does not track yet is all new, so every one of its lines counts.
    """
    diff_text = git(
        repo_root,
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        # Fixed prefixes, so a user's diff.noprefix or diff.mnemonicPrefix cannot change them.
        "--src-prefix=a/",
        "--dst-prefix=b/",
        merge_base(repo_root, base),
        "--",
        "src/",
    )
    changed = parse_changed_lines(diff_text)
    untracked = git(repo_root, "ls-files", "-z", "--others", "--exclude-standard", "--", "src/")
    for path in untracked.split("\0"):
        if not path.endswith(".py"):
            continue
        # Bytes, so a file that is not UTF-8 still reaches the parse step that names it.
        line_count = len((repo_root / path).read_bytes().splitlines())
        if line_count == 0:
            continue
        changed[path] = set(range(1, line_count + 1))
    return changed


def read_markers(source: str) -> dict[int, str]:
    """The ``# mutation-ok: <reason>`` comments by line; the reason is empty if none was given."""
    markers: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        match = MARKER.search(token.string)
        if match is None:
            continue
        markers[token.start[0]] = match.group(1).strip()
    return markers


def marker_reason(mutant: Mutant, markers: dict[int, str]) -> str | None:
    """The reason of a marker on any line the mutated expression spans, or None."""
    for line in range(mutant.line, mutant.end_line + 1):
        if line in markers:
            return markers[line]
    return None


def nodes_never_mutated(tree: ast.AST) -> set[int]:
    """The ids of docstrings, annotations and f-string format specs in ``tree``."""
    skipped: set[int] = set()
    for node in ast.walk(tree):
        untouched: ast.AST | None = None
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            untouched = node.returns
        elif isinstance(node, ast.arg | ast.AnnAssign):
            untouched = node.annotation
        elif isinstance(node, ast.FormattedValue):
            # A replaced format spec is a ValueError, not a fault a test has to catch.
            untouched = node.format_spec
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            # A docstring, or any other constant statement: changing it changes nothing.
            skipped.add(id(node.value))
        if untouched is not None:
            skipped.update(id(child) for child in ast.walk(untouched))
    return skipped


def short_repr(value: object) -> str:
    """``repr(value)``, cut to 40 characters so a long SQL string keeps the line readable."""
    text = repr(value)
    if len(text) <= 40:
        return text
    return text[:37] + "..."


def constant_replacements(node: ast.Constant) -> list[tuple[str, ast.AST]]:
    """The mutation of a constant: booleans swap, numbers add one, strings are replaced."""
    value = node.value
    # bool before int: True is an int, and must become False, not 2.
    if isinstance(value, bool):
        return [(f"{value} -> {not value}", ast.Constant(not value))]
    if isinstance(value, int | float):
        return [(f"{value!r} -> {value + 1!r}", ast.Constant(value + 1))]
    if isinstance(value, str):
        replaced = "XX" + value + "XX"
        return [(f"{short_repr(value)} -> {short_repr(replaced)}", ast.Constant(replaced))]
    return []


def comparison_replacements(node: ast.Compare) -> list[tuple[str, ast.AST]]:
    """One mutation per operator of a (possibly chained) comparison, each flipped."""
    replacements: list[tuple[str, ast.AST]] = []
    for position, operator in enumerate(node.ops):
        flipped = FLIPPED_COMPARISONS[type(operator)]
        replacement = copy.deepcopy(node)
        replacement.ops[position] = flipped()
        description = f"{OPERATOR_SYMBOLS[type(operator)]} -> {OPERATOR_SYMBOLS[flipped]}"
        replacements.append((description, replacement))
    return replacements


def operator_swap(node: ast.BoolOp | ast.BinOp, new_operator: ast.AST) -> tuple[str, ast.AST]:
    """The mutation that puts ``new_operator`` in place of the operator of ``node``."""
    replacement = copy.deepcopy(node)
    replacement.op = new_operator
    description = f"{OPERATOR_SYMBOLS[type(node.op)]} -> {OPERATOR_SYMBOLS[type(new_operator)]}"
    return description, replacement


def replacements_for(node: ast.AST) -> list[tuple[str, ast.AST]]:
    """Each mutation of ``node``: what changes, and the node to put in its place."""
    if isinstance(node, ast.Compare):
        return comparison_replacements(node)
    if isinstance(node, ast.BoolOp):
        swapped = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return [operator_swap(node, swapped)]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return [("drop not", copy.deepcopy(node.operand))]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Sub):
        swapped = ast.Sub() if isinstance(node.op, ast.Add) else ast.Add()
        return [operator_swap(node, swapped)]
    if isinstance(node, ast.Constant):
        return constant_replacements(node)
    return []


def replace_node(tree: ast.AST, old: ast.AST, new: ast.AST) -> None:
    """Put ``new`` where ``old`` is in ``tree``; raise ``ValueError`` if ``old`` is not there."""
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            if value is old:
                setattr(parent, field, new)
                return
            if not isinstance(value, list):
                continue
            for position, item in enumerate(value):
                if item is old:
                    value[position] = new
                    return
    raise ValueError(f"{type(old).__name__} node is not in the tree")


def find_mutants(source: str, lines: set[int]) -> list[Mutant]:
    """Every mutant of an expression that spans one of ``lines``, in line and column order."""
    tree = ast.parse(source)
    skipped = nodes_never_mutated(tree)
    sites: list[tuple[ast.expr, int]] = []
    for index, node in enumerate(ast.walk(tree)):
        if not isinstance(node, ast.expr) or id(node) in skipped:
            continue
        end_line = node.end_lineno if node.end_lineno is not None else node.lineno
        if lines.isdisjoint(range(node.lineno, end_line + 1)):
            continue
        sites.append((node, index))
    # ast.walk is breadth-first; the report and the --max-mutants cut go by position.
    sites.sort(key=lambda site: (site[0].lineno, site[0].col_offset))

    mutants: list[Mutant] = []
    for node, index in sites:
        end_line = node.end_lineno if node.end_lineno is not None else node.lineno
        for description, replacement in replacements_for(node):
            # A fresh parse has the same nodes in the same walk order, so index finds the twin.
            mutated_tree = ast.parse(source)
            target = list(ast.walk(mutated_tree))[index]
            replace_node(mutated_tree, target, replacement)
            mutants.append(Mutant(node.lineno, end_line, description, ast.unparse(mutated_tree)))
    return mutants


def copy_source(repo_root: Path, destination: Path) -> Path:
    """Copy ``src/`` into ``destination`` without any ``__pycache__``; return the copy."""
    source_copy = destination / "src"
    shutil.copytree(repo_root / "src", source_copy, ignore=shutil.ignore_patterns("__pycache__"))
    return source_copy


def child_environment(source_copy: Path) -> dict[str, str]:
    """The environment of a child run: the copy first on the path, no bytecode written."""
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    if existing:
        environment["PYTHONPATH"] = str(source_copy) + os.pathsep + existing
    else:
        environment["PYTHONPATH"] = str(source_copy)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def pytest_command(test_files: list[str]) -> list[str]:
    """The pytest command for ``test_files``, stopping at the first failure."""
    paths = [f"tests/{name}" for name in test_files]
    return [sys.executable, "-B", "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *paths]


def imported_package_file(source_copy: Path, repo_root: Path) -> Path:
    """The ``db_conn_mcp.__file__`` a child run with the copy on its path imports."""
    command = [sys.executable, "-B", "-c", "import db_conn_mcp; print(db_conn_mcp.__file__)"]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=child_environment(source_copy),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CheckError(f"the copy of src/ cannot be imported: {completed.stderr.strip()}")
    return Path(completed.stdout.strip()).resolve()


def run_tests(
    source_copy: Path, test_files: list[str], repo_root: Path, time_limit: float | None = None
) -> bool:
    """Whether ``test_files`` pass against ``source_copy``.

    Raises ``subprocess.TimeoutExpired`` when the run takes longer than ``time_limit``.
    Output goes nowhere: a test's own child process can hold a pipe open past the kill.
    """
    completed = subprocess.run(
        pytest_command(test_files),
        cwd=repo_root,
        env=child_environment(source_copy),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=time_limit,
        check=False,
    )
    return completed.returncode == 0


def run_baseline(repo_root: Path, test_files: list[str], printed_skips: set[str]) -> float:
    """Run ``test_files`` on an unmutated copy and return the seconds taken.

    Raises ``CheckError`` when the copy is not what the tests import, or the tests fail:
    a mutant counted as killed then would prove nothing. Skip lines not printed before are
    printed, so a run without the live database shows those tests did not run.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        source_copy = copy_source(repo_root, Path(temp))
        package_file = imported_package_file(source_copy, repo_root)
        if not package_file.is_relative_to(source_copy.resolve()):
            raise CheckError(
                f"the tests import db_conn_mcp from {package_file}, not from the copy of src/"
            )
        started = time.monotonic()
        completed = subprocess.run(
            [*pytest_command(test_files), "-rs"],
            cwd=repo_root,
            env=child_environment(source_copy),
            capture_output=True,
            text=True,
            check=False,
        )
        elapsed = time.monotonic() - started
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        print(output, flush=True)
        paths = ", ".join(f"tests/{name}" for name in test_files)
        raise CheckError(f"the unmutated code fails {paths}")
    for line in output.splitlines():
        if line.startswith("SKIPPED") and line not in printed_skips:
            printed_skips.add(line)
            print(line, flush=True)
    return elapsed


def time_limit_seconds(baseline_seconds: float, minimum_seconds: float) -> float:
    """The time a mutant run may take: three baselines, but never under the minimum."""
    return max(minimum_seconds, BASELINE_TIME_MULTIPLE * baseline_seconds)


def run_mutant(
    repo_root: Path, path: str, mutant: Mutant, test_files: list[str], time_limit: float
) -> str:
    """Test a copy holding ``mutant``: returns ``killed``, ``survived`` or ``timed out``."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
        source_copy = copy_source(repo_root, Path(temp))
        mutated_file = source_copy / path.removeprefix("src/")
        mutated_file.write_text(mutant.source, encoding="utf-8")
        try:
            passed = run_tests(source_copy, test_files, repo_root, time_limit)
        except subprocess.TimeoutExpired:
            return "timed out"
    if passed:
        return "survived"
    return "killed"


def planned_mutants(
    repo_root: Path, changed: dict[str, set[int]]
) -> list[tuple[str, Mutant, str | None]]:
    """Each changed file's mutants with their marker reason, in file and line order.

    Raises ``CheckError`` for a changed file with no ``TESTS_FOR_MODULE`` entry, one that
    cannot be parsed, and a marker without a reason, before any test runs.
    """
    planned: list[tuple[str, Mutant, str | None]] = []
    for path in sorted(changed):
        if path.removeprefix(PACKAGE_DIR) not in TESTS_FOR_MODULE:
            raise CheckError(
                f"{path} has no entry in TESTS_FOR_MODULE in scripts/mutation_check.py"
            )
        try:
            source = (repo_root / path).read_text(encoding="utf-8")
            markers = read_markers(source)
            mutants = find_mutants(source, changed[path])
        except (UnicodeDecodeError, tokenize.TokenError, SyntaxError) as exc:
            raise CheckError(f"{path} cannot be parsed: {exc}") from exc
        for line, reason in markers.items():
            if not reason:
                raise CheckError(f"{path}:{line}: '# mutation-ok:' needs a reason after it")
        for mutant in mutants:
            planned.append((path, mutant, marker_reason(mutant, markers)))
    return planned


def run_planned_mutants(
    repo_root: Path,
    planned: list[tuple[str, Mutant, str | None]],
    max_mutants: int,
    minimum_time_limit: float,
) -> dict[str, int]:
    """Run the first ``max_mutants`` unmarked mutants, printing each non-killed result."""
    counts = {"killed": 0, "survived": 0, "timed out": 0, "marked": 0, "not run": 0}
    baseline_seconds: dict[str, float] = {}
    printed_skips: set[str] = set()
    ran = 0
    for path, mutant, reason in planned:
        where = f"{path}:{mutant.line} {mutant.description}"
        if reason is not None:
            counts["marked"] += 1
            print(f"marked {where} (reason: {reason})", flush=True)
            continue
        if ran >= max_mutants:
            counts["not run"] += 1
            continue
        ran += 1
        test_files = TESTS_FOR_MODULE[path.removeprefix(PACKAGE_DIR)]
        if path not in baseline_seconds:
            baseline_seconds[path] = run_baseline(repo_root, test_files, printed_skips)
        time_limit = time_limit_seconds(baseline_seconds[path], minimum_time_limit)
        outcome = run_mutant(repo_root, path, mutant, test_files, time_limit)
        counts[outcome] += 1
        if outcome != "killed":
            print(f"{outcome} {where}", flush=True)
    return counts


def run_check(
    repo_root: Path,
    base: str,
    max_mutants: int,
    minimum_time_limit: float = MINIMUM_TIME_LIMIT_SECONDS,
) -> int:
    """Check the Python lines changed under ``src/`` since ``base``; return the exit code."""
    started = time.monotonic()
    try:
        base_commit = merge_base(repo_root, base)
        changed = {
            path: lines
            for path, lines in changed_lines(repo_root, base).items()
            if path.endswith(".py")
        }
        planned = planned_mutants(repo_root, changed)
        counts = run_planned_mutants(repo_root, planned, max_mutants, minimum_time_limit)
        short_sha = git(repo_root, "rev-parse", "--short", base_commit).strip()
    # OSError: a copy, a write or a child process failed, which says nothing about the tests.
    except (CheckError, OSError) as exc:
        print(f"mutation check: could not run: {exc}", flush=True)
        return 2
    changed_line_count = sum(len(lines) for lines in changed.values())
    print(
        f"mutation check: base {short_sha} | files {len(changed)} | lines {changed_line_count}"
        f" | mutants {len(planned)} | killed {counts['killed']} | survived {counts['survived']}"
        f" | timed out {counts['timed out']} | marked {counts['marked']}"
        f" | not run {counts['not run']} | {time.monotonic() - started:.1f}s",
        flush=True,
    )
    if counts["survived"] or counts["not run"]:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments, find the repository around the current directory, run the check."""
    parser = argparse.ArgumentParser(
        description="Mutate the src/ lines changed since a base ref and run their tests."
    )
    parser.add_argument(
        "--base",
        default="origin/main",
        help="ref the branch is compared with, from its merge base (default: origin/main)",
    )
    parser.add_argument(
        "--max-mutants",
        type=int,
        default=100,
        help="mutants to run at most; any left over fail the check (default: 100)",
    )
    args = parser.parse_args(argv)
    try:
        repo_root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    except CheckError as exc:
        print(f"mutation check: could not run: {exc}", flush=True)
        return 2
    return run_check(repo_root, args.base, args.max_mutants)


if __name__ == "__main__":
    raise SystemExit(main())
