"""Tests for ``scripts/mutation_check.py``, the mutation check on a branch's changed lines.

The end-to-end tests build a throwaway git repository holding a toy ``src/db_conn_mcp/``
and a toy test file, so every mutant run is a real pytest child on a real copy, never a
fake: the check's whole value is that a mutant the tests miss is reported.
"""

import re
import subprocess
from pathlib import Path

import mutation_check
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SUMMARY = re.compile(
    r"^mutation check: base (?P<sha>[0-9a-f]{4,}) \| files (?P<files>\d+) \| lines (?P<lines>\d+)"
    r" \| mutants (?P<mutants>\d+) \| killed (?P<killed>\d+) \| survived (?P<survived>\d+)"
    r" \| timed out (?P<timed_out>\d+) \| marked (?P<marked>\d+) \| not run (?P<not_run>\d+)"
    r" \| (?P<seconds>\d+(\.\d+)?)s$"
)

TOY_TESTS_FOR_MODULE = {"__init__.py": ["test_toy.py"], "toy.py": ["test_toy.py"]}

ANSWER_BEFORE = "def answer():\n    return 1\n"
ANSWER_AFTER = "def answer():\n    return 2\n"
TEST_CATCHES_ANSWER = (
    "from db_conn_mcp.toy import answer\n\n\ndef test_answer():\n    assert answer() == 2\n"
)
TEST_MISSES_ANSWER = (
    "from db_conn_mcp.toy import answer\n\n\ndef test_answer():\n    assert answer() > 0\n"
)


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a fixed identity, so commits work on any runner."""
    command = [
        "git",
        "-c",
        "user.name=Mutation Check Test",
        "-c",
        "user.email=mutation-check@example.com",
        "-c",
        "commit.gpgsign=false",
        *args,
    ]
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def _write(path: Path, text: str) -> None:
    """Write ``text`` with ``\\n`` line endings on every OS, so line numbers match the diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _repository(tmp_path: Path, committed: dict[str, str]) -> Path:
    """A git repository with ``committed`` (relative path -> text) as its one commit."""
    repo = tmp_path / "toy"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    for relative, text in committed.items():
        _write(repo / relative, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _toy_repository(tmp_path: Path, test_text: str, before: str, after: str) -> Path:
    """A toy package whose ``toy.py`` changes from ``before`` to ``after``, uncommitted."""
    repo = _repository(
        tmp_path,
        {
            "src/db_conn_mcp/__init__.py": "",
            "src/db_conn_mcp/toy.py": before,
            "tests/test_toy.py": test_text,
        },
    )
    _write(repo / "src/db_conn_mcp/toy.py", after)
    return repo


def _run_main(monkeypatch, capsys, repo: Path, *args: str) -> tuple[int, str]:
    """Run ``main`` from inside ``repo``; return its exit code and what it printed."""
    monkeypatch.chdir(repo)
    exit_code = mutation_check.main(["--base", "HEAD", *args])
    return exit_code, capsys.readouterr().out


def _summary(output: str) -> re.Match[str]:
    """The summary line, which is always the last line printed."""
    last_line = output.strip().splitlines()[-1]
    match = SUMMARY.match(last_line)
    assert match is not None, last_line
    return match


# ---- parse_changed_lines ----

DIFF = """\
diff --git a/src/db_conn_mcp/a.py b/src/db_conn_mcp/a.py
index 1111111..2222222 100644
--- a/src/db_conn_mcp/a.py
+++ b/src/db_conn_mcp/a.py
@@ -3 +3 @@ def f():
-    return 1
+    return 2
@@ -10,0 +11,2 @@ def g():
+    x = 1
+    y = 2
@@ -20,2 +22,0 @@ def h():
-    gone = 1
-    gone = 2
diff --git a/src/db_conn_mcp/b.py b/src/db_conn_mcp/b.py
deleted file mode 100644
index 3333333..0000000
--- a/src/db_conn_mcp/b.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a = 1
-b = 2
diff --git a/src/db_conn_mcp/c.py b/src/db_conn_mcp/c.py
index 4444444..5555555 100644
--- a/src/db_conn_mcp/c.py
+++ b/src/db_conn_mcp/c.py
@@ -5 +4,0 @@
-removed = 1
"""


def test_parse_changed_lines_keeps_only_new_side_lines():
    """``+3`` is one line, ``+11,2`` two, and a ``+c,0`` hunk adds no line at all."""
    assert mutation_check.parse_changed_lines(DIFF) == {"src/db_conn_mcp/a.py": {3, 11, 12}}


def test_parse_changed_lines_of_an_empty_diff_is_empty():
    assert mutation_check.parse_changed_lines("") == {}


# ---- changed_lines ----


def test_changed_lines_in_repository_counts_committed_and_uncommitted_edits_only(tmp_path):
    """Lines changed on the base after the fork are not the branch's, so they do not count."""
    module = "src/pkg/m.py"
    repo = _repository(tmp_path, {module: "a = 1\nb = 2\nc = 3\n", "notes.txt": "one\n"})
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo / module, "a = 10\nb = 2\nc = 3\n")
    _write(repo / "notes.txt", "two\n")
    _git(repo, "commit", "-q", "-am", "feature change")
    _git(repo, "checkout", "-q", "-")
    _write(repo / module, "a = 1\nb = 20\nc = 3\n")
    _git(repo, "commit", "-q", "-am", "base moves on")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _write(repo / module, "a = 10\nb = 2\nc = 30\n")
    _write(repo / "notes.txt", "three\n")

    assert mutation_check.changed_lines(repo, base) == {module: {1, 3}}


# ---- find_mutants ----


@pytest.mark.parametrize(
    ("source", "mutated"),
    [
        ("x = a == b", "x = a != b"),
        ("x = a != b", "x = a == b"),
        ("x = a < b", "x = a >= b"),
        ("x = a >= b", "x = a < b"),
        ("x = a > b", "x = a <= b"),
        ("x = a <= b", "x = a > b"),
        ("x = a is b", "x = a is not b"),
        ("x = a is not b", "x = a is b"),
        ("x = a in b", "x = a not in b"),
        ("x = a not in b", "x = a in b"),
        ("x = a and b", "x = a or b"),
        ("x = a or b", "x = a and b"),
        ("x = True", "x = False"),
        ("x = False", "x = True"),
        ("x = not a", "x = a"),
        ("x = a + b", "x = a - b"),
        ("x = a - b", "x = a + b"),
        ("x = 1", "x = 2"),
        ("x = 'abc'", "x = 'XXabcXX'"),
    ],
)
def test_find_mutants_makes_one_mutant_per_operator(source, mutated):
    """``True`` is an ``int`` too; it must flip to ``False``, not also become ``2``."""
    mutants = mutation_check.find_mutants(source + "\n", {1})
    assert [mutant.source.strip() for mutant in mutants] == [mutated]


def test_find_mutants_skips_unchanged_lines():
    mutants = mutation_check.find_mutants("x = a == b\ny = c == d\n", {2})
    assert [(mutant.line, mutant.source.strip()) for mutant in mutants] == [
        (2, "x = a == b\ny = c != d")
    ]


def test_find_mutants_takes_an_expression_that_spans_a_changed_line():
    mutants = mutation_check.find_mutants("x = (a ==\n     b)\n", {2})
    assert [(mutant.line, mutant.end_line, mutant.description) for mutant in mutants] == [
        (1, 2, "== -> !=")
    ]


def test_find_mutants_never_touches_docstrings():
    source = '"""Module doc."""\n\n\ndef f():\n    """Function doc."""\n    return 1\n'
    mutants = mutation_check.find_mutants(source, {1, 2, 3, 4, 5, 6})
    assert [mutant.description for mutant in mutants] == ["1 -> 2"]


def test_find_mutants_never_touches_annotations():
    """Defaults and assigned values are code; the annotations beside them are not."""
    source = 'def f(a: Literal["x"] = "y") -> Literal[True]:\n    b: Literal[1] = 2\n'
    mutants = mutation_check.find_mutants(source, {1, 2})
    assert [mutant.description for mutant in mutants] == ["'y' -> 'XXyXX'", "2 -> 3"]


def test_find_mutants_never_touches_a_literal_type_alias():
    source = 'Action = Literal["inject", "uninject"]\nKind = typing.Literal[1]\n'
    assert mutation_check.find_mutants(source, {1, 2}) == []


def test_find_mutants_replaces_f_string_text_but_not_its_format_spec():
    """A replaced format spec only raises ``ValueError``; the text is what a reader sees."""
    mutants = mutation_check.find_mutants('x = f"a{b:>3}"\n', {1})
    assert [mutant.source.strip() for mutant in mutants] == ["x = f'XXaXX{b:>3}'"]


def test_find_mutants_orders_mutants_by_line_then_column():
    mutants = mutation_check.find_mutants("y = c == d\nx = 1 + 2\n", {1, 2})
    assert [(mutant.line, mutant.description) for mutant in mutants] == [
        (1, "== -> !="),
        (2, "+ -> -"),
        (2, "1 -> 2"),
        (2, "2 -> 3"),
    ]


# ---- markers ----


def test_read_markers_returns_the_reason_by_line():
    source = "x = 1\ny = 2  # mutation-ok: same either way\n"
    assert mutation_check.read_markers(source) == {2: "same either way"}


def test_read_markers_ignores_marker_text_inside_a_string():
    assert mutation_check.read_markers('x = "# mutation-ok: x"\n') == {}


def test_read_markers_keeps_an_empty_marker_so_main_can_refuse_it():
    assert mutation_check.read_markers("x = 1  # mutation-ok:\n") == {1: ""}


def test_marker_on_any_line_of_a_multi_line_expression_marks_it():
    mutant = mutation_check.Mutant(line=1, end_line=3, description="== -> !=", source="")
    assert mutation_check.marker_reason(mutant, {3: "same either way"}) == "same either way"
    assert mutation_check.marker_reason(mutant, {4: "same either way"}) is None


def test_marker_end_to_end_reports_the_mutant_as_marked_and_does_not_run_it(
    tmp_path, monkeypatch, capsys
):
    """The toy tests miss this mutant, so running it would have made ``main`` return 1."""
    after = "def answer():\n    return 2  # mutation-ok: the caller ignores the value\n"
    repo = _toy_repository(tmp_path, TEST_MISSES_ANSWER, ANSWER_BEFORE, after)
    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", TOY_TESTS_FOR_MODULE)

    exit_code, output = _run_main(monkeypatch, capsys, repo)

    assert exit_code == 0, output
    assert "marked src/db_conn_mcp/toy.py:2 2 -> 3 (reason: the caller ignores the value)" in output
    summary = _summary(output)
    assert (summary["marked"], summary["killed"], summary["survived"]) == ("1", "0", "0")


def test_marker_without_a_reason_stops_the_check_naming_file_and_line(
    tmp_path, monkeypatch, capsys
):
    after = "def answer():\n    return 2  # mutation-ok:\n"
    repo = _toy_repository(tmp_path, TEST_MISSES_ANSWER, ANSWER_BEFORE, after)
    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", TOY_TESTS_FOR_MODULE)

    exit_code, output = _run_main(monkeypatch, capsys, repo)

    assert exit_code == 2
    assert "src/db_conn_mcp/toy.py:2" in output


# ---- end to end ----


def test_end_to_end_killed_mutant_returns_0_with_the_summary_line(tmp_path, monkeypatch, capsys):
    repo = _toy_repository(tmp_path, TEST_CATCHES_ANSWER, ANSWER_BEFORE, ANSWER_AFTER)
    toy_module = repo / "src/db_conn_mcp/toy.py"
    toy_bytes = toy_module.read_bytes()
    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", TOY_TESTS_FOR_MODULE)

    exit_code, output = _run_main(monkeypatch, capsys, repo)

    assert exit_code == 0, output
    summary = _summary(output)
    assert summary["sha"] == _git(repo, "rev-parse", "--short", "HEAD")
    counts = {name: summary[name] for name in ("files", "lines", "mutants", "killed")}
    assert counts == {"files": "1", "lines": "1", "mutants": "1", "killed": "1"}
    assert summary["survived"] == summary["timed_out"] == summary["not_run"] == "0"
    assert toy_module.read_bytes() == toy_bytes
    assert list(repo.rglob("__pycache__")) == []


def test_end_to_end_surviving_mutant_prints_survived_and_returns_1(tmp_path, monkeypatch, capsys):
    repo = _toy_repository(tmp_path, TEST_MISSES_ANSWER, ANSWER_BEFORE, ANSWER_AFTER)
    toy_module = repo / "src/db_conn_mcp/toy.py"
    toy_bytes = toy_module.read_bytes()
    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", TOY_TESTS_FOR_MODULE)

    exit_code, output = _run_main(monkeypatch, capsys, repo)

    assert exit_code == 1
    assert "survived src/db_conn_mcp/toy.py:2 2 -> 3" in output
    assert _summary(output)["survived"] == "1"
    assert toy_module.read_bytes() == toy_bytes
    assert list(repo.rglob("__pycache__")) == []


# ---- baseline ----


def test_baseline_failure_stops_the_check_before_any_mutant(tmp_path, monkeypatch, capsys):
    test_fails_unmutated = TEST_CATCHES_ANSWER.replace("== 2", "== 99")
    repo = _toy_repository(tmp_path, test_fails_unmutated, ANSWER_BEFORE, ANSWER_AFTER)
    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", TOY_TESTS_FOR_MODULE)

    exit_code, output = _run_main(monkeypatch, capsys, repo)

    assert exit_code == 2
    assert "tests/test_toy.py" in output
    assert "killed" not in output


def test_baseline_importing_the_package_from_outside_the_copy_stops_the_check(
    tmp_path, monkeypatch, capsys
):
    """A mutant run against the wrong package would count every mutant as survived."""
    repo = _toy_repository(tmp_path, TEST_CATCHES_ANSWER, ANSWER_BEFORE, ANSWER_AFTER)
    elsewhere = (tmp_path / "elsewhere" / "db_conn_mcp" / "__init__.py").resolve()
    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", TOY_TESTS_FOR_MODULE)
    monkeypatch.setattr(mutation_check, "imported_package_file", lambda *_: elsewhere)

    exit_code, output = _run_main(monkeypatch, capsys, repo)

    assert exit_code == 2
    assert str(elsewhere) in output
    assert "killed" not in output


# ---- max mutants ----


def test_max_mutants_runs_the_first_in_file_and_line_order(tmp_path, monkeypatch, capsys):
    """Mutants past the cap are unchecked code, so the run fails even with no survivor run."""
    repo = _repository(
        tmp_path,
        {
            "src/db_conn_mcp/__init__.py": "",
            "src/db_conn_mcp/alpha.py": "FIRST = None\nSECOND = None\n",
            "src/db_conn_mcp/beta.py": "THIRD = None\n",
            "tests/test_toy.py": "def test_nothing():\n    pass\n",
        },
    )
    _write(repo / "src/db_conn_mcp/beta.py", "THIRD = 3\n")
    _write(repo / "src/db_conn_mcp/alpha.py", "FIRST = 1\nSECOND = 2\n")
    tests_for_module = {
        "__init__.py": ["test_toy.py"],
        "alpha.py": ["test_toy.py"],
        "beta.py": ["test_toy.py"],
    }
    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", tests_for_module)

    exit_code, output = _run_main(monkeypatch, capsys, repo, "--max-mutants", "2")

    assert exit_code == 1
    assert "survived src/db_conn_mcp/alpha.py:1 1 -> 2" in output
    assert "survived src/db_conn_mcp/alpha.py:2 2 -> 3" in output
    assert "beta.py" not in output
    summary = _summary(output)
    assert (summary["mutants"], summary["survived"], summary["not_run"]) == ("3", "2", "1")


# ---- TESTS_FOR_MODULE ----


def test_tests_for_module_covers_every_module_and_names_real_test_files():
    package = REPO_ROOT / "src" / "db_conn_mcp"
    modules = {path.relative_to(package).as_posix() for path in package.rglob("*.py")}
    assert set(mutation_check.TESTS_FOR_MODULE) == modules
    for module, test_files in mutation_check.TESTS_FOR_MODULE.items():
        for name in test_files:
            assert (REPO_ROOT / "tests" / name).is_file(), (module, name)


def test_tests_for_module_missing_entry_stops_the_check_naming_the_dict(
    tmp_path, monkeypatch, capsys
):
    repo = _toy_repository(tmp_path, TEST_CATCHES_ANSWER, ANSWER_BEFORE, ANSWER_AFTER)
    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", {"__init__.py": ["test_toy.py"]})

    exit_code, output = _run_main(monkeypatch, capsys, repo)

    assert exit_code == 2
    assert "TESTS_FOR_MODULE" in output
    assert "src/db_conn_mcp/toy.py" in output


# ---- summary ----


def test_summary_with_zero_counts_when_no_src_python_line_changed(tmp_path, monkeypatch, capsys):
    repo = _toy_repository(tmp_path, TEST_CATCHES_ANSWER, ANSWER_BEFORE, ANSWER_BEFORE)
    _write(repo / "README.md", "changed outside src\n")
    _write(repo / "src/db_conn_mcp/notes.txt", "not python\n")
    _git(repo, "add", "-A")
    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", TOY_TESTS_FOR_MODULE)

    exit_code, output = _run_main(monkeypatch, capsys, repo)

    assert exit_code == 0, output
    summary = _summary(output)
    counts = [summary[name] for name in ("files", "lines", "mutants", "killed", "survived")]
    assert counts == ["0", "0", "0", "0", "0"]
    assert summary["sha"] == _git(repo, "rev-parse", "--short", "HEAD")


# ---- time limit ----


def test_timed_out_limit_is_the_larger_of_the_floor_and_three_baselines():
    assert mutation_check.time_limit_seconds(10.0, 60.0) == 60.0
    assert mutation_check.time_limit_seconds(30.0, 60.0) == 90.0


def test_timed_out_mutant_is_counted_apart_from_killed_and_does_not_fail(
    tmp_path, monkeypatch, capsys
):
    """A mutant that loops forever has not been caught by an assertion, nor has it survived."""
    test_loops_until_ready = (
        "from db_conn_mcp.toy import ready\n\n\n"
        "def test_ready():\n    while not ready():\n        pass\n"
    )
    before = "def ready():\n    return None\n"
    after = "def ready():\n    return True\n"
    repo = _toy_repository(tmp_path, test_loops_until_ready, before, after)

    monkeypatch.setattr(mutation_check, "TESTS_FOR_MODULE", TOY_TESTS_FOR_MODULE)

    exit_code = mutation_check.run_check(repo, "HEAD", 100, minimum_time_limit=0.1)
    output = capsys.readouterr().out

    assert exit_code == 0, output
    assert "timed out src/db_conn_mcp/toy.py:2 True -> False" in output
    summary = _summary(output)
    assert (summary["timed_out"], summary["killed"], summary["survived"]) == ("1", "0", "0")
