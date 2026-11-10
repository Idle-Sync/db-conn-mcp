"""The mutation check's CI job and README section must name the same command.

No YAML parser is a declared dependency, so the workflow is checked as text.
"""

import re
from pathlib import Path

from mutation_check import find_mutants

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMAND = "python scripts/mutation_check.py --base"


def mutation_job_text() -> str:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    after_header = workflow.split("\n  mutation:\n", maxsplit=1)[1]
    return re.split(r"\n  [\w-]+:\n", after_header, maxsplit=1)[0]


def development_section() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    after_heading = readme.split("\n## Development\n", maxsplit=1)[1]
    return after_heading.split("\n## ", maxsplit=1)[0]


def marker_example_line() -> str:
    python_block = development_section().split("```python\n", maxsplit=1)[1]
    for line in python_block.splitlines():
        if "# mutation-ok:" in line:
            return line
    raise AssertionError("the README's python block has no '# mutation-ok:' line")


def test_ci_job_runs_the_check() -> None:
    assert COMMAND in mutation_job_text()


def test_ci_job_fetches_full_history() -> None:
    # The check diffs from the merge base, which a shallow clone does not have.
    assert "fetch-depth: 0" in mutation_job_text()


def test_readme_development_section_shows_the_command() -> None:
    assert COMMAND in development_section()


def test_readme_names_the_marker() -> None:
    assert "# mutation-ok:" in development_section()


def test_readme_marker_example_marks_only_a_line_that_never_runs() -> None:
    # The reason says the line never runs, which holds for any mutant of it; a line the
    # script mutated in some other way would need a different reason.
    mutants = find_mutants(marker_example_line() + "\n", {1})
    assert [mutant.description for mutant in mutants] == ["'unreachable' -> 'XXunreachableXX'"]
