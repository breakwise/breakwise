"""Tests for tools/validate_notices.py."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest

import validate_notices as vn

REPO_ROOT = Path(__file__).resolve().parent.parent
NOTICE = Path("notices/pypi/pydantic-ai/BFX-2026-0001.yaml")
TODAY = dt.date(2026, 10, 21)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small copy of the repository with one valid notice and a README without examples."""
    for rel in (vn.SCHEMA_FILE, NOTICE):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, target)
    (tmp_path / vn.README_FILE).write_text("# Test\n", encoding="utf-8")
    return tmp_path


def problems_in(root: Path) -> list[str]:
    problems, _ = vn.validate(root, today=TODAY)
    return [str(problem) for problem in problems]


def edit(root: Path, old: str, new: str, rel: Path = NOTICE) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} is not in {rel}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def line_number(root: Path, startswith: str, rel: Path = NOTICE) -> int:
    lines = (root / rel).read_text(encoding="utf-8").splitlines()
    return next(i for i, line in enumerate(lines, 1) if line.startswith(startswith))


def test_the_real_repository_is_valid() -> None:
    problems, count = vn.validate(REPO_ROOT)
    assert [str(p) for p in problems] == []
    assert count >= 1


def test_a_valid_notice_passes(repo: Path) -> None:
    assert problems_in(repo) == []


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        pytest.param(
            'breaks_in: "2.0.0"',
            "breaks_in: 2.0",
            "2.0 is not of type 'string'",
            id="unquoted version",
        ),
        pytest.param(
            "rename:\n  from: pydantic_ai.models.openai.OpenAIModel\n"
            "  to: pydantic_ai.models.openai.OpenAIChatModel\n",
            "",
            "'rename' is a required property",
            id="rename without its names",
        ),
        pytest.param(
            "type: rename",
            "type: removal",
            "'instructions' is a required property",
            id="no instructions",
        ),
        pytest.param(
            "type: rename",
            'type: removal\ninstructions: "Use OpenAIChatModel instead."',
            "`rename` is only allowed when `type` is `rename`",
            id="rename with another type",
        ),
        pytest.param(
            "id: BFX-2026-0001", "id: BFX-26-1", "Expected an ID like BFX-2026-0001", id="bad id"
        ),
        pytest.param(
            "name: pydantic-ai",
            "name: Pydantic_AI",
            "Expected a normalized PyPI name",
            id="bad package name",
        ),
        pytest.param(
            "https://pydantic.dev/docs/ai/changelog/",
            "http://pydantic.dev/docs/ai/changelog/",
            "Expected an https:// link",
            id="http link",
        ),
        pytest.param(
            "status: verified",
            "status: verified\nowner: someone",
            "('owner' was unexpected)",
            id="unknown field",
        ),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2026-02-30"',
            "is not a real date",
            id="impossible date",
        ),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2026-12-01"',
            "is in the future",
            id="future date",
        ),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2025-10-21"',
            "must be the year it was published (2025)",
            id="id year",
        ),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2026-10-21"\nmodified: "2026-10-01"',
            "modified must not be before published",
            id="modified too early",
        ),
        pytest.param(
            'works_since: "0.7.6"',
            'works_since: "2.1.0"',
            "must not be later than breaks_in",
            id="works_since too late",
        ),
        pytest.param(
            "  to: pydantic_ai.models.openai.OpenAIChatModel",
            "  to: pydantic_ai.models.openai.OpenAIModel",
            "rename.from and rename.to are the same",
            id="same names",
        ),
        pytest.param(
            '    model = OpenAIChatModel("gpt-5")',
            '    model = OpenAIChatModel("gpt-5"',
            "example.after is not valid Python",
            id="syntax error",
        ),
        pytest.param(
            "    from pydantic_ai.models.openai import OpenAIModel\n\n"
            '    model = OpenAIModel("gpt-5")',
            "    import pydantic_ai\n\n    model = None",
            "example.before must use the old name OpenAIModel",
            id="before without old name",
        ),
        pytest.param(
            "    from pydantic_ai.models.openai import OpenAIChatModel\n\n"
            '    model = OpenAIChatModel("gpt-5")',
            "    import pydantic_ai\n\n    model = None",
            "example.after must use the new name OpenAIChatModel",
            id="after without new name",
        ),
        pytest.param(
            '    model = OpenAIChatModel("gpt-5")',
            '    model = OpenAIChatModel("gpt-5")\n    legacy = OpenAIModel',
            "example.after must not use the old name OpenAIModel",
            id="after with old name",
        ),
        pytest.param(
            'breaks_in: "2.0.0"',
            'breaks_in: "2.0.0"\nbreaks_in: "2.0.0"',
            "Duplicate key: breaks_in",
            id="duplicate key",
        ),
        pytest.param(
            "summary: OpenAIModel", "summary: [OpenAIModel", "Invalid YAML", id="invalid YAML"
        ),
    ],
)
def test_invalid_notices_are_rejected(repo: Path, old: str, new: str, expected: str) -> None:
    edit(repo, old, new)
    problems = problems_in(repo)
    assert any(expected in problem for problem in problems), problems


def test_two_documents_are_rejected(repo: Path) -> None:
    path = repo / NOTICE
    path.write_text(path.read_text(encoding="utf-8") + "---\nid: BFX-2026-0002\n", encoding="utf-8")
    assert any("exactly one YAML document" in p for p in problems_in(repo))


def test_wrong_file_name_is_rejected(repo: Path) -> None:
    (repo / NOTICE).rename(repo / NOTICE.with_name("BFX-2026-0002.yaml"))
    assert any(f"Must be stored at {NOTICE.as_posix()}" in p for p in problems_in(repo))


def test_wrong_folder_is_rejected(repo: Path) -> None:
    target = repo / "notices/pypi/pydantic/BFX-2026-0001.yaml"
    target.parent.mkdir(parents=True)
    (repo / NOTICE).rename(target)
    assert any(f"Must be stored at {NOTICE.as_posix()}" in p for p in problems_in(repo))


def test_duplicate_ids_are_rejected(repo: Path) -> None:
    copy = repo / "notices/pypi/openai/BFX-2026-0001.yaml"
    copy.parent.mkdir(parents=True)
    shutil.copy(repo / NOTICE, copy)
    assert any("BFX-2026-0001 is already used by" in p for p in problems_in(repo))


def test_yml_extension_is_rejected(repo: Path) -> None:
    (repo / NOTICE).rename(repo / NOTICE.with_suffix(".yml"))
    assert any("Use the .yaml extension" in p for p in problems_in(repo))


def test_problems_point_to_the_right_line(repo: Path) -> None:
    edit(repo, 'breaks_in: "2.0.0"', "breaks_in: 2.0")
    expected = f"{NOTICE.as_posix()}:{line_number(repo, 'breaks_in:')}:"
    assert any(problem.startswith(expected) for problem in problems_in(repo))


def test_syntax_errors_point_to_the_line_in_the_example(repo: Path) -> None:
    edit(repo, '    model = OpenAIChatModel("gpt-5")', '    model = OpenAIChatModel("gpt-5"')
    line = line_number(repo, '    model = OpenAIChatModel("gpt-5"')
    assert any(p.startswith(f"{NOTICE.as_posix()}:{line}:") for p in problems_in(repo))


def test_readme_example_must_match_the_notice(repo: Path) -> None:
    notice_text = (repo / NOTICE).read_text(encoding="utf-8")
    readme = repo / vn.README_FILE
    readme.write_text(f"# Test\n\n```yaml\n{notice_text}```\n", encoding="utf-8")
    assert problems_in(repo) == []

    readme.write_text(f"# Test\n\n```yaml\n{notice_text}# changed\n```\n", encoding="utf-8")
    assert any(f"must match {NOTICE.as_posix()} exactly" in p for p in problems_in(repo))


def test_readme_example_must_be_a_real_notice(repo: Path) -> None:
    (repo / vn.README_FILE).write_text("```yaml\nid: BFX-2026-9999\n```\n", encoding="utf-8")
    assert any("no valid notice with that ID" in p for p in problems_in(repo))


def test_other_yaml_blocks_in_the_readme_are_ignored(repo: Path) -> None:
    (repo / vn.README_FILE).write_text("```yaml\nname: example\n```\n", encoding="utf-8")
    assert problems_in(repo) == []


def test_moved_object_examples_are_checked_by_module(repo: Path) -> None:
    edit(
        repo,
        "  to: pydantic_ai.models.openai.OpenAIChatModel",
        "  to: pydantic_ai.models.chat.OpenAIModel",
    )
    edit(repo, "import OpenAIChatModel", "import OpenAIModel")
    edit(repo, 'model = OpenAIChatModel("gpt-5")', 'model = OpenAIModel("gpt-5")')
    assert any(
        "example.after must use the new name pydantic_ai.models.chat" in p
        for p in problems_in(repo)
    )


def test_github_annotations_are_escaped() -> None:
    problem = vn.Problem(Path("a,b:c.yaml"), 3, "50% wrong\nsecond line")
    assert (
        problem.github_annotation()
        == "::error file=a%2Cb%3Ac.yaml,line=3::50%25 wrong%0Asecond line"
    )


def test_main_returns_an_error_code_only_when_something_is_wrong(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert vn.main(["--root", str(repo)]) == 0
    assert "All 1 notice(s) are valid." in capsys.readouterr().out

    edit(repo, 'breaks_in: "2.0.0"', "breaks_in: 2.0")
    assert vn.main(["--root", str(repo)]) == 1
    assert "1 problem(s) found in 1 notice(s)." in capsys.readouterr().out
