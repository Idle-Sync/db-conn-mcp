"""The mutation check's CI job and README section must name the same command.

No YAML parser is a declared dependency, so the workflow is checked as text.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMAND = "python scripts/mutation_check.py --base"


def mutation_job_text() -> str:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    return workflow.split("\n  mutation:\n", maxsplit=1)[1]


def development_section() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    after_heading = readme.split("\n## Development\n", maxsplit=1)[1]
    return after_heading.split("\n## ", maxsplit=1)[0]


def test_ci_job_runs_the_check() -> None:
    assert COMMAND in mutation_job_text()


def test_ci_job_fetches_full_history() -> None:
    # The check diffs from the merge base, which a shallow clone does not have.
    assert "fetch-depth: 0" in mutation_job_text()


def test_readme_development_section_shows_the_command() -> None:
    assert COMMAND in development_section()


def test_readme_names_the_marker() -> None:
    assert "# mutation-ok:" in development_section()
