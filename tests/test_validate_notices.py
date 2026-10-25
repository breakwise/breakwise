"""Tests for tools/validate_notices.py."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import textwrap
from pathlib import Path

import jsonschema
import pytest
import yaml

import validate_notices as vn

REPO_ROOT = Path(__file__).resolve().parent.parent
NOTICE = Path("notices/pypi/pydantic-ai/BW-2026-0001.yaml")
TODAY = dt.date(2026, 10, 21)

# The tests start from this copy of BW-2026-0001 as it was first published, moved to format 0.3.
# It is fixed here, so that changes to the real notice don't break the tests.
NOTICE_TEXT = """\
---
schema_version: "0.3"
id: BW-2026-0001
status: verified
published: "2026-10-21"
package:
  ecosystem: PyPI
  name: pydantic-ai
  aliases:
    - pydantic-ai-slim
type: rename
breaks_in: "2.0.0"
works_since: "0.7.6"
summary: OpenAIModel was renamed to OpenAIChatModel, and the old name was removed in 2.0.0.
rename:
  from: pydantic_ai.models.openai.OpenAIModel
  to: pydantic_ai.models.openai.OpenAIChatModel
example:
  before: |
    from pydantic_ai.models.openai import OpenAIModel

    model = OpenAIModel("gpt-5")
  after: |
    from pydantic_ai.models.openai import OpenAIChatModel

    model = OpenAIChatModel("gpt-5")
references:
  - https://pydantic.dev/docs/ai/changelog/
  - https://github.com/pydantic/pydantic-ai/blob/v0.7.6/pydantic_ai_slim/pydantic_ai/models/openai.py
"""

BEFORE_BLOCK = (
    '    from pydantic_ai.models.openai import OpenAIModel\n\n    model = OpenAIModel("gpt-5")\n'
)
AFTER_BLOCK = (
    "    from pydantic_ai.models.openai import OpenAIChatModel\n\n"
    '    model = OpenAIChatModel("gpt-5")\n'
)

# The messages of the rules that format 0.3 added, written out to pin their exact words.
REQUIRED_AFFECTS = (
    "`affects` is required when `type` is not `rename`: it names the code that breaks."
)
REQUIRED_FAILS_WITH = (
    "`example.fails_with` is required when `type` is not `rename`: "
    "it says how `example.before` fails on `breaks_in`."
)
REQUIRED_AFTER = (
    "`example.fails_with.after` is required when `type` is not `rename` and `works_since` "
    "is set: it says how `example.after` fails on the release before `works_since`."
)
NOT_ALLOWED_AFFECTS = (
    "`affects` is not allowed when `type` is `rename`: `rename.from` names what breaks."
)
NOT_ALLOWED_AFTER = (
    "`example.fails_with.after` is only allowed with `works_since`: "
    "it says how `example.after` fails on the release before `works_since`."
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small repository with the real schema, the fixed notice and a README without examples."""
    schema = tmp_path / vn.SCHEMA_FILE
    schema.parent.mkdir(parents=True)
    shutil.copy(REPO_ROOT / vn.SCHEMA_FILE, schema)
    notice = tmp_path / NOTICE
    notice.parent.mkdir(parents=True)
    notice.write_text(NOTICE_TEXT, encoding="utf-8")
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


def at(root: Path, startswith: str, message: str) -> str:
    """The text of a problem in the notice at the line that starts with ``startswith``."""
    return f"{NOTICE.as_posix()}:{line_number(root, startswith)}: {message}"


# --- The registry -------------------------------------------------------------


def test_the_real_repository_is_valid() -> None:
    problems, count = vn.validate(REPO_ROOT)
    assert [str(p) for p in problems] == []
    assert count >= 1


def test_a_valid_notice_passes(repo: Path) -> None:
    assert problems_in(repo) == []
    assert vn.validate(repo, today=TODAY)[1] == 1


def test_only_notices_and_markdown_files_belong_in_the_registry(repo: Path) -> None:
    folder = repo / NOTICE.parent
    for name in ("BW-2026-0002.YAML", "BW-2026-0003.yml", "BW-2026-0004", "notes.txt"):
        (folder / name).write_text("id: x\n", encoding="utf-8")
    (folder / "README.md").write_text("# Notes\n", encoding="utf-8")
    (repo / "notices/.DS_Store").write_bytes(b"\0")
    (repo / "notices/.cache").mkdir()
    (repo / "notices/.cache/BW-2026-0005.yaml").write_text("id: x\n", encoding="utf-8")
    message = "Notice files must end in .yaml, and other files here in .md."
    assert problems_in(repo) == [
        f"{NOTICE.parent.as_posix()}/{name}:1: {message}"
        for name in ("BW-2026-0002.YAML", "BW-2026-0003.yml", "BW-2026-0004", "notes.txt")
    ]
    assert vn.validate(repo, today=TODAY)[1] == 1


def test_duplicate_ids_are_rejected(repo: Path) -> None:
    copy = repo / "notices/pypi/openai/BW-2026-0001.yaml"
    copy.parent.mkdir(parents=True)
    shutil.copy(repo / NOTICE, copy)
    # Files are checked in path order, so the copy in openai/ claims the ID first.
    message = "ID BW-2026-0001 is already used by notices/pypi/openai/BW-2026-0001.yaml."
    assert f"{NOTICE.as_posix()}:3: {message}" in problems_in(repo)


def test_a_broken_schema_is_an_error_not_a_pass(repo: Path) -> None:
    schema_file = repo / vn.SCHEMA_FILE
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    schema["type"] = "no-such-type"
    schema_file.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(jsonschema.SchemaError):
        vn.validate(repo, today=TODAY)


# --- Reading notice files -----------------------------------------------------


def test_a_notice_must_be_utf8(repo: Path) -> None:
    (repo / NOTICE).write_bytes("summary: café\n".encode("latin-1"))
    assert problems_in(repo) == [f"{NOTICE.as_posix()}:1: A notice file must be UTF-8 text."]


@pytest.mark.parametrize("text", ["", "# only a comment\n"])
def test_an_empty_file_is_rejected(repo: Path, text: str) -> None:
    (repo / NOTICE).write_text(text, encoding="utf-8")
    assert problems_in(repo) == [f"{NOTICE.as_posix()}:1: The notice file is empty."]


def test_an_empty_document_is_rejected(repo: Path) -> None:
    (repo / NOTICE).write_text("---\n", encoding="utf-8")
    assert problems_in(repo) == [f"{NOTICE.as_posix()}:1: The notice file is empty."]


def test_a_second_document_is_rejected_at_its_line(repo: Path) -> None:
    path = repo / NOTICE
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "---\nid: BW-2026-0002\n", encoding="utf-8")
    line = text.count("\n") + 1
    assert problems_in(repo) == [
        f"{NOTICE.as_posix()}:{line}: A notice file must contain only one YAML document."
    ]


def test_invalid_yaml_is_reported_at_its_line(repo: Path) -> None:
    edit(repo, "summary: OpenAIModel", "summary: [OpenAIModel")
    problems = problems_in(repo)
    assert len(problems) == 1
    assert problems[0].startswith(f"{NOTICE.as_posix()}:")
    assert "Invalid YAML:" in problems[0]


def test_characters_yaml_does_not_allow_are_reported(repo: Path) -> None:
    edit(repo, "summary: OpenAIModel", "summary: Open\x01AIModel")
    problems = problems_in(repo)
    assert len(problems) == 1
    assert problems[0].startswith(f"{NOTICE.as_posix()}:1: Invalid YAML: unacceptable character")


def test_values_of_the_wrong_kind_are_named(repo: Path) -> None:
    edit(repo, "  aliases:\n    - pydantic-ai-slim", "  aliases: pydantic-ai-slim")
    message = "package.aliases: 'pydantic-ai-slim' is not of type 'array'"
    assert problems_in(repo) == [at(repo, "  aliases:", message)]


def test_dates_are_checked_against_the_given_day(repo: Path) -> None:
    problems, _ = vn.validate(repo, today=dt.date(2026, 1, 1))
    assert [p.message for p in problems] == ["published: 2026-10-21 is in the future."]


def test_keys_that_are_lists_are_rejected(repo: Path) -> None:
    path = repo / NOTICE
    path.write_text(path.read_text(encoding="utf-8") + "? [a, b]\n: c\n", encoding="utf-8")
    line = line_number(repo, "? [a, b]")
    assert problems_in(repo) == [f"{NOTICE.as_posix()}:{line}: Invalid YAML: found unhashable key"]


def test_anchors_aliases_and_tags_are_rejected(repo: Path) -> None:
    edit(repo, 'breaks_in: "2.0.0"', 'breaks_in: &version "2.0.0"')
    edit(repo, 'works_since: "0.7.6"', "works_since: *version")
    edit(repo, "summary: ", "summary: !!str ")
    assert problems_in(repo) == [
        at(repo, "breaks_in:", "Notices are plain YAML: remove the anchor &version."),
        at(repo, "works_since:", "Notices are plain YAML: remove the alias *version."),
        at(repo, "summary:", "Notices are plain YAML: remove the tag."),
    ]


@pytest.mark.parametrize(
    ("old", "new", "starts", "value"),
    [
        pytest.param('breaks_in: "2.0.0"', "breaks_in: 2.10", "breaks_in:", "2.10", id="number"),
        pytest.param('breaks_in: "2.0.0"', "breaks_in: 2", "breaks_in:", "2", id="integer"),
        pytest.param(
            'published: "2026-10-21"',
            "published: 2026-10-21",
            "published:",
            "2026-10-21",
            id="date",
        ),
        pytest.param(
            'published: "2026-10-21"',
            "published: 2026-02-30",
            "published:",
            "2026-02-30",
            id="date that does not exist",
        ),
        pytest.param("status: verified", "status: yes", "status:", "yes", id="boolean"),
        pytest.param("  name: pydantic-ai", "  name: 0x_", "  name:", "0x_", id="broken number"),
    ],
)
def test_values_must_be_written_as_text(
    repo: Path, old: str, new: str, starts: str, value: str
) -> None:
    edit(repo, old, new)
    message = f'Write {value} in quotes, as "{value}", so that YAML reads it as text.'
    assert problems_in(repo) == [at(repo, starts, message)]


def test_duplicate_keys_are_rejected_at_any_depth(repo: Path) -> None:
    edit(repo, 'breaks_in: "2.0.0"', 'breaks_in: "2.0.0"\nbreaks_in: "2.0.0"')
    edit(repo, "  name: pydantic-ai", "  name: pydantic-ai\n  name: pydantic-ai")
    lines = (repo / NOTICE).read_text(encoding="utf-8").splitlines()
    name_line = [i for i, line in enumerate(lines, 1) if line == "  name: pydantic-ai"][1]
    breaks_line = [i for i, line in enumerate(lines, 1) if line.startswith("breaks_in:")][1]
    assert problems_in(repo) == [
        f"{NOTICE.as_posix()}:{name_line}: Duplicate key: name",
        f"{NOTICE.as_posix()}:{breaks_line}: Duplicate key: breaks_in",
    ]


# --- The schema ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "starts", "message"),
    [
        pytest.param(
            "rename:\n  from: pydantic_ai.models.openai.OpenAIModel\n"
            "  to: pydantic_ai.models.openai.OpenAIChatModel\n",
            "",
            "schema_version:",
            "'rename' is a required property",
            id="rename without its names",
        ),
        pytest.param(
            "id: BW-2026-0001",
            "id: BW-26-1",
            "id:",
            "id: 'BW-26-1' is not valid. Expected an ID like BW-2026-0001: the year it was "
            "published, then a number of four to eight digits, without extra leading zeros.",
            id="bad id",
        ),
        pytest.param(
            "type: rename",
            'type: rename\ninstructions: "Rename every use of OpenAIModel."',
            "schema_version:",
            "`instructions` is not allowed when `type` is `rename`: "
            "a rename is fixed automatically.",
            id="rename with instructions",
        ),
        pytest.param(
            "  from: pydantic_ai.models.openai.OpenAIModel",
            "  from: pydantic_ai.models.openai.Open-AI-Model",
            "  from:",
            "rename.from: 'pydantic_ai.models.openai.Open-AI-Model' is not valid. "
            "Expected a Python name or import path, e.g. TypeError or "
            "pydantic_ai.models.openai.OpenAIModel.",
            id="bad name",
        ),
        pytest.param(
            "  name: pydantic-ai",
            "  name: Pydantic_AI",
            "  name:",
            "package.name: 'Pydantic_AI' is not valid. Expected a normalized PyPI name: "
            "lowercase letters and digits, words joined by single hyphens.",
            id="bad package name",
        ),
        pytest.param(
            "https://pydantic.dev/docs/ai/changelog/",
            "http://pydantic.dev/docs/ai/changelog/",
            "  - http://",
            "references[0]: 'http://pydantic.dev/docs/ai/changelog/' is not valid. "
            "Expected an https:// link.",
            id="http link",
        ),
        pytest.param(
            "status: verified",
            "status: verified\nowner: someone",
            "schema_version:",
            "Additional properties are not allowed ('owner' was unexpected)",
            id="unknown field",
        ),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2026-10-21"\nmodified:',
            "modified:",
            "modified: has no value.",
            id="empty value",
        ),
    ],
)
def test_notices_must_match_the_schema(
    repo: Path, old: str, new: str, starts: str, message: str
) -> None:
    edit(repo, old, new)
    assert problems_in(repo) == [at(repo, starts, message)]


def test_every_schema_problem_is_reported(repo: Path) -> None:
    edit(repo, "type: rename", "type: removal")
    assert problems_in(repo) == [
        at(repo, "schema_version:", "'instructions' is a required property"),
        at(repo, "schema_version:", REQUIRED_AFFECTS),
        at(repo, "schema_version:", "`rename` is only allowed when `type` is `rename`."),
        at(repo, "example:", REQUIRED_FAILS_WITH),
    ]


def test_line_of_finds_a_value_or_its_closest_parent() -> None:
    tree = yaml.compose("a:\n  b: 1\nc:\n  - x\n  - y\n", Loader=yaml.SafeLoader)
    assert vn.line_of(tree, ["a", "b"]) == 2
    assert vn.line_of(tree, ["c", 1]) == 5
    assert vn.line_of(tree, ["a", "missing"]) == 1
    assert vn.line_of(tree, ["c", 2]) == 3
    assert vn.line_of(tree, ["c", 5]) == 3
    assert vn.line_of(tree, ["a", "b", "deeper"]) == 2


def test_field_names_read_like_the_docs() -> None:
    assert vn.field_name([]) == ""
    assert vn.field_name(["example", "before"]) == "example.before"
    assert vn.field_name(["references", 1]) == "references[1]"


# --- The fields and rules of format 0.3 ---------------------------------------

# A notice of a type other than rename, with every field that format 0.3 has.
EVERY_FIELD_TEXT = """\
---
schema_version: "0.3"
id: BW-2026-0001
status: draft
published: "2026-10-21"
modified: "2026-10-21"
package:
  ecosystem: PyPI
  name: pydantic-ai
  aliases:
    - pydantic-ai-slim
type: signature
breaks_in: "2.0.0"
works_since: "0.7.6"
restored_in: "2.1.0"
summary: Agent no longer takes result_type, and output_type takes its place.
instructions: Pass the same value as output_type instead of result_type.
affects:
  - pydantic_ai.Agent.run
  - path: pydantic_ai.Agent
    keyword: result_type
    when: passed
example:
  before: |
    from pydantic_ai import Agent

    Agent("test", result_type=str)
  after: |
    from pydantic_ai import Agent

    Agent("test", output_type=str)
  fails_with:
    before:
      error: TypeError
      message: unexpected keyword argument 'result_type'
    after:
      error: pydantic_ai.exceptions.UserError
      message: "Unknown keyword arguments: `output_type`"
references:
  - https://pydantic.dev/docs/ai/project/changelog/
withdrawn:
  date: "2026-10-21"
  reason: It was a duplicate of another notice.
"""

AFFECTS_BLOCK = (
    "affects:\n  - pydantic_ai.Agent.run\n  - path: pydantic_ai.Agent\n"
    "    keyword: result_type\n    when: passed\n"
)
FAILS_WITH_AFTER = (
    "    after:\n      error: pydantic_ai.exceptions.UserError\n"
    '      message: "Unknown keyword arguments: `output_type`"\n'
)
FAILS_WITH_BLOCK = (
    "  fails_with:\n    before:\n      error: TypeError\n"
    "      message: unexpected keyword argument 'result_type'\n" + FAILS_WITH_AFTER
)
RENAME_FAILS_WITH = "  fails_with:\n    before:\n      error: ImportError\n"


@pytest.fixture
def full_repo(repo: Path) -> Path:
    """The repository with a notice that has every field of format 0.3."""
    (repo / NOTICE).write_text(EVERY_FIELD_TEXT, encoding="utf-8")
    return repo


def test_a_notice_with_every_field_passes(full_repo: Path) -> None:
    assert problems_in(full_repo) == []


def test_a_final_newline_passes_a_pattern_but_not_the_one_line_rule() -> None:
    # Python's $ also matches just before a final newline, so format 0.2 took "a.b\n" as a name.
    def valid(schema_file: str, definition: str, value: str) -> bool:
        schema = json.loads((REPO_ROOT / schema_file).read_text(encoding="utf-8"))
        rule = {"$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"}
        return jsonschema.Draft202012Validator(rule).is_valid(value)

    assert valid("schema/notice-0.2.schema.json", "dotted_name", "a.b\n")
    assert not valid(vn.SCHEMA_FILE.as_posix(), "path", "a.b\n")
    assert valid(vn.SCHEMA_FILE.as_posix(), "path", "a.b")


@pytest.mark.parametrize(
    ("fixture", "line", "field"),
    [
        ("full_repo", "id: BW-2026-0001", "id"),
        ("full_repo", 'published: "2026-10-21"', "published"),
        ("full_repo", 'modified: "2026-10-21"', "modified"),
        ("full_repo", "  name: pydantic-ai", "package.name"),
        ("full_repo", "    - pydantic-ai-slim", "package.aliases[0]"),
        ("full_repo", 'breaks_in: "2.0.0"', "breaks_in"),
        ("full_repo", 'works_since: "0.7.6"', "works_since"),
        ("full_repo", 'restored_in: "2.1.0"', "restored_in"),
        ("full_repo", "summary: Agent no longer takes result_type, and output_type", "summary"),
        ("full_repo", "instructions: Pass the same value as output_type", "instructions"),
        ("full_repo", "  - pydantic_ai.Agent.run", "affects[0]"),
        ("full_repo", "  - path: pydantic_ai.Agent", "affects[1].path"),
        ("full_repo", "    keyword: result_type", "affects[1].keyword"),
        ("full_repo", "      error: TypeError", "example.fails_with.before.error"),
        ("full_repo", "      message: unexpected keyword", "example.fails_with.before.message"),
        ("full_repo", "      error: pydantic_ai.exceptions", "example.fails_with.after.error"),
        ("full_repo", "  - https://pydantic.dev/docs/ai/project/changelog/", "references[0]"),
        ("full_repo", '  date: "2026-10-21"', "withdrawn.date"),
        ("full_repo", "  reason: It was a duplicate", "withdrawn.reason"),
        ("repo", "  from: pydantic_ai.models.openai.OpenAIModel", "rename.from"),
        ("repo", "  to: pydantic_ai.models.openai.OpenAIChatModel", "rename.to"),
        ("keyword_repo", "    - pydantic_ai.Agent", "rename.keyword_of[0]"),
        ("keyword_repo", "  from: mcp_servers", "rename.from"),
        ("keyword_repo", "  to: toolsets", "rename.to"),
    ],
)
def test_text_fields_must_be_one_line(
    request: pytest.FixtureRequest, fixture: str, line: str, field: str
) -> None:
    root: Path = request.getfixturevalue(fixture)
    # The whole line, whose value then ends in a newline, written in double quotes.
    text = (root / NOTICE).read_text(encoding="utf-8")
    old = next(whole for whole in text.splitlines() if whole.startswith(line))
    head, sep, value = old.rpartition(": ") if ": " in old else old.rpartition("- ")
    new = f"{head}{sep}{json.dumps(value.strip(chr(34)) + chr(10))}"
    edit(root, old, new)
    assert problems_in(root) == [at(root, new, f"{field}: must be one line.")]


def test_an_empty_text_field_is_not_also_more_than_one_line(full_repo: Path) -> None:
    # The one-line rule only looks at text, so an empty value gets one message.
    edit(full_repo, "  reason: It was a duplicate of another notice.", "  reason:")
    assert problems_in(full_repo) == [at(full_repo, "  reason:", "withdrawn.reason: has no value.")]


@pytest.mark.parametrize(
    ("fixture", "changes", "starts", "message"),
    [
        pytest.param(
            "full_repo",
            [(AFFECTS_BLOCK, "")],
            "schema_version:",
            REQUIRED_AFFECTS,
            id="affects is required for other types",
        ),
        pytest.param(
            "full_repo",
            [(FAILS_WITH_BLOCK, "")],
            "example:",
            REQUIRED_FAILS_WITH,
            id="fails_with is required for other types",
        ),
        pytest.param(
            "full_repo",
            [(FAILS_WITH_AFTER, "")],
            "  fails_with:",
            REQUIRED_AFTER,
            id="fails_with.after is required with works_since",
        ),
        pytest.param(
            "full_repo",
            [('works_since: "0.7.6"\n', "")],
            "  fails_with:",
            NOT_ALLOWED_AFTER,
            id="fails_with.after needs works_since",
        ),
        pytest.param(
            "repo",
            [
                ('works_since: "0.7.6"\n', ""),
                (AFTER_BLOCK, AFTER_BLOCK + RENAME_FAILS_WITH + "    after:\n      error: E\n"),
            ],
            "  fails_with:",
            NOT_ALLOWED_AFTER,
            id="fails_with.after needs works_since for a rename too",
        ),
        pytest.param(
            "repo",
            [("example:\n", "affects:\n  - pydantic_ai.models.openai.OpenAIModel\nexample:\n")],
            "schema_version:",
            NOT_ALLOWED_AFFECTS,
            id="affects with a rename",
        ),
        pytest.param(
            "full_repo",
            [("example:\n", "rename:\n  from: a.Old\n  to: a.New\nexample:\n")],
            "schema_version:",
            "`rename` is only allowed when `type` is `rename`.",
            id="rename with another type",
        ),
        pytest.param(
            "full_repo",
            [("type: signature", "type: behavior")],
            "      error: TypeError",
            "example.fails_with.before.error: 'TypeError' is not valid. "
            "Expected AssertionError: the example of a behavior change fails an assert.",
            id="behavior fails with something else than an assert",
        ),
        pytest.param(
            "full_repo",
            [(AFFECTS_BLOCK, "affects: []\n")],
            "affects:",
            "affects: [] should be non-empty",
            id="empty affects",
        ),
        pytest.param(
            "full_repo",
            [("  - pydantic_ai.Agent.run\n", "  - pydantic_ai.Agent.run\n" * 2)],
            "affects:",
            "affects: ['pydantic_ai.Agent.run', 'pydantic_ai.Agent.run', {'path': "
            "'pydantic_ai.Agent', 'keyword': 'result_type', 'when': 'passed'}] "
            "has non-unique elements",
            id="affects twice",
        ),
        pytest.param(
            "full_repo",
            [("  - pydantic_ai.Agent.run", "  - pydantic-ai.Agent.run")],
            "  - pydantic-ai",
            "affects[0]: 'pydantic-ai.Agent.run' is not valid. Expected a Python name or "
            "import path, e.g. TypeError or pydantic_ai.models.openai.OpenAIModel.",
            id="affects with a bad path",
        ),
        pytest.param(
            "full_repo",
            [("  - path: pydantic_ai.Agent\n    keyword", "  - keyword")],
            "  - keyword",
            "affects[1]: 'path' is a required property",
            id="affects without its path",
        ),
        pytest.param(
            "full_repo",
            [("    when: passed\n", "    when: passed\n    since: x\n")],
            "  - path:",
            "affects[1]: Additional properties are not allowed ('since' was unexpected)",
            id="affects with an unknown field",
        ),
        pytest.param(
            # A path without a keyword is written alone, so each item is written one way only.
            "full_repo",
            [("    keyword: result_type\n    when: passed\n", "")],
            "  - path:",
            "affects[1]: 'keyword' is a required property",
            id="a path in an object without a keyword",
        ),
        pytest.param(
            "full_repo",
            [("when: passed", "when: always")],
            "    when:",
            "affects[1].when: 'always' is not one of ['passed', 'omitted']",
            id="unknown when",
        ),
        pytest.param(
            "full_repo",
            [("    before:\n      error: TypeError\n", "    before:\n")],
            "    before:",
            "example.fails_with.before: 'error' is a required property",
            id="failure without its error",
        ),
        pytest.param(
            "full_repo",
            [(FAILS_WITH_BLOCK.removeprefix("  fails_with:\n").removesuffix(FAILS_WITH_AFTER), "")],
            "  fails_with:",
            "example.fails_with: 'before' is a required property",
            id="fails_with without before",
        ),
        pytest.param(
            "full_repo",
            [("      error: TypeError", "      error: Type Error")],
            "      error: Type",
            "example.fails_with.before.error: 'Type Error' is not valid. Expected a Python name "
            "or import path, e.g. TypeError or pydantic_ai.models.openai.OpenAIModel.",
            id="bad error",
        ),
        pytest.param(
            "full_repo",
            [("      message: unexpected keyword argument 'result_type'", '      message: ""')],
            '      message: ""',
            "example.fails_with.before.message: '' should be non-empty",
            id="empty message",
        ),
        pytest.param(
            "full_repo",
            [("      error: TypeError\n", "      error: TypeError\n      code: E1\n")],
            "    before:",
            "example.fails_with.before: Additional properties are not allowed "
            "('code' was unexpected)",
            id="failure with an unknown field",
        ),
        pytest.param(
            "full_repo",
            [("  fails_with:\n", "  fails_with:\n    during:\n      error: E\n")],
            "  fails_with:",
            "example.fails_with: Additional properties are not allowed ('during' was unexpected)",
            id="fails_with with an unknown field",
        ),
        pytest.param(
            "full_repo",
            [('restored_in: "2.1.0"', "restored_in: soon")],
            "restored_in:",
            "restored_in: 'soon' is not valid. "
            "Expected a PEP 440 public version, e.g. 2.0.0, 1.0.0rc1, 2.1.0.post1 or 1!2.0.",
            id="bad restored_in",
        ),
        pytest.param(
            "full_repo",
            [("  reason: It was a duplicate of another notice.\n", "")],
            "withdrawn:",
            "withdrawn: 'reason' is a required property",
            id="withdrawn without a reason",
        ),
        pytest.param(
            "full_repo",
            [("It was a duplicate of another notice.", "Duplicate")],
            "  reason:",
            "withdrawn.reason: 'Duplicate' is too short",
            id="withdrawn with a short reason",
        ),
        pytest.param(
            "full_repo",
            [('  date: "2026-10-21"', '  date: "2026-10-21"\n  by: someone')],
            "withdrawn:",
            "withdrawn: Additional properties are not allowed ('by' was unexpected)",
            id="withdrawn with an unknown field",
        ),
    ],
)
def test_rules_of_format_0_3(
    request: pytest.FixtureRequest,
    fixture: str,
    changes: list[tuple[str, str]],
    starts: str,
    message: str,
) -> None:
    root: Path = request.getfixturevalue(fixture)
    for old, new in changes:
        edit(root, old, new)
    assert problems_in(root) == [at(root, starts, message)]


@pytest.mark.parametrize(
    ("fixture", "changes"),
    [
        pytest.param("repo", [(AFTER_BLOCK, AFTER_BLOCK + RENAME_FAILS_WITH)], id="rename with"),
        pytest.param(
            "repo",
            [(AFTER_BLOCK, AFTER_BLOCK + RENAME_FAILS_WITH + "    after:\n      error: E\n")],
            id="rename with fails_with.after and works_since",
        ),
        pytest.param(
            "full_repo",
            [('works_since: "0.7.6"\n', ""), (FAILS_WITH_AFTER, "")],
            id="no works_since and no fails_with.after",
        ),
        pytest.param(
            "full_repo",
            [("type: signature", "type: behavior"), ("error: TypeError", "error: AssertionError")],
            id="behavior that fails an assert",
        ),
        pytest.param(
            "full_repo", [("error: TypeError", "error: AssertionError")], id="assert for others"
        ),
        pytest.param("full_repo", [("when: passed", "when: omitted")], id="when omitted"),
        pytest.param("full_repo", [("    when: passed\n", "")], id="keyword without when"),
        pytest.param(
            "full_repo", [("  - pydantic_ai.Agent.run", "  - pydantic_ai")], id="one-part affects"
        ),
        pytest.param(
            "full_repo",
            [("      message: unexpected keyword argument 'result_type'\n", "")],
            id="failure without its message",
        ),
        pytest.param("full_repo", [('breaks_in: "2.0.0"', 'breaks_in: "1!2.0"')], id="epoch"),
        pytest.param(
            "repo",
            [
                ("  from: pydantic_ai.models.openai.OpenAIModel", "  from: pydantic_ai_v1"),
                ("  to: pydantic_ai.models.openai.OpenAIChatModel", "  to: pydantic_ai"),
                (BEFORE_BLOCK, '    import pydantic_ai_v1\n\n    pydantic_ai_v1.Agent("test")\n'),
                (AFTER_BLOCK, '    import pydantic_ai\n\n    pydantic_ai.Agent("test")\n'),
            ],
            id="one-part rename",
        ),
    ],
)
def test_what_format_0_3_allows(
    request: pytest.FixtureRequest, fixture: str, changes: list[tuple[str, str]]
) -> None:
    root: Path = request.getfixturevalue(fixture)
    for old, new in changes:
        edit(root, old, new)
    assert problems_in(root) == []


# --- Rules beyond the schema --------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "starts", "message"),
    [
        pytest.param(
            'published: "2026-10-21"',
            'published: "2026-02-30"',
            "published:",
            "published: '2026-02-30' is not a real date.",
            id="impossible date",
        ),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2026-10-23"',
            "published:",
            "published: 2026-10-23 is in the future.",
            id="future date",
        ),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2026-10-21"\nmodified: "2026-10-23"',
            "modified:",
            "modified: 2026-10-23 is in the future.",
            id="future modified date",
        ),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2025-10-21"',
            "id:",
            "The year in BW-2026-0001 must be the year it was published (2025).",
            id="id year",
        ),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2026-10-21"\nmodified: "2026-10-20"',
            "modified:",
            "modified must not be before published.",
            id="modified too early",
        ),
        pytest.param(
            'works_since: "0.7.6"',
            'works_since: "2.1.0"',
            "works_since:",
            "works_since (2.1.0) must not be later than breaks_in (2.0.0).",
            id="works_since too late",
        ),
        pytest.param(
            "    - pydantic-ai-slim",
            "    - pydantic-ai",
            "  aliases:",
            "package.aliases must not repeat package.name.",
            id="alias repeats the name",
        ),
    ],
)
def test_rules_beyond_the_schema(repo: Path, old: str, new: str, starts: str, message: str) -> None:
    edit(repo, old, new)
    assert problems_in(repo) == [at(repo, starts, message)]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param('published: "2026-10-21"', 'published: "2026-10-22"', id="published tomorrow"),
        pytest.param(
            'published: "2026-10-21"',
            'published: "2026-10-21"\nmodified: "2026-10-21"',
            id="modified on the same day",
        ),
        pytest.param('works_since: "0.7.6"', 'works_since: "2.0.0"', id="works_since is breaks_in"),
    ],
)
def test_boundaries_that_are_allowed(repo: Path, old: str, new: str) -> None:
    edit(repo, old, new)
    assert problems_in(repo) == []


def test_wrong_file_name_is_rejected(repo: Path) -> None:
    (repo / NOTICE).rename(repo / NOTICE.with_name("BW-2026-0002.yaml"))
    rel = NOTICE.with_name("BW-2026-0002.yaml").as_posix()
    message = "from its id, package.ecosystem and package.name."
    assert problems_in(repo) == [f"{rel}:3: Must be stored at {NOTICE.as_posix()}, {message}"]


def test_wrong_folder_is_rejected(repo: Path) -> None:
    target = repo / "notices/pypi/pydantic/BW-2026-0001.yaml"
    target.parent.mkdir(parents=True)
    (repo / NOTICE).rename(target)
    assert any(f"Must be stored at {NOTICE.as_posix()}" in p for p in problems_in(repo))


def test_problems_are_sorted_by_line(repo: Path) -> None:
    edit(repo, "    - pydantic-ai-slim", "    - pydantic-ai")
    edit(repo, 'published: "2026-10-21"', 'published: "2026-12-01"')
    lines = [int(problem.split(":")[1]) for problem in problems_in(repo)]
    assert len(lines) == 2
    assert lines == sorted(lines)


# --- Examples -----------------------------------------------------------------


def test_syntax_errors_point_to_the_line_in_the_example(repo: Path) -> None:
    edit(repo, '    model = OpenAIChatModel("gpt-5")', '    model = OpenAIChatModel("gpt-5"')
    assert problems_in(repo) == [
        at(
            repo,
            '    model = OpenAIChatModel("gpt-5"',
            "example.after is not valid Python: '(' was never closed",
        )
    ]


def test_syntax_errors_in_quoted_examples_point_to_their_first_line(repo: Path) -> None:
    edit(repo, f"  before: |\n{BEFORE_BLOCK}", '  before: "x = ("\n')
    assert problems_in(repo) == [
        at(repo, "  before:", "example.before is not valid Python: '(' was never closed")
    ]


OLD_PATH = "pydantic_ai.models.openai.OpenAIModel"
NEW_PATH = "pydantic_ai.models.openai.OpenAIChatModel"
THROUGH = "by its full path or through an import of it"
MUST_USE_FROM = f"example.before must use rename.from, {OLD_PATH}, {THROUGH}."
MUST_USE_TO = f"example.after must use rename.to, {NEW_PATH}, {THROUGH}."


@pytest.mark.parametrize(
    ("before", "after", "starts", "message"),
    [
        pytest.param(
            "    import pydantic_ai\n\n    model = None\n",
            AFTER_BLOCK,
            "  before:",
            MUST_USE_FROM,
            id="before without the old name",
        ),
        pytest.param(
            "    # OpenAIModel\n    model = 'OpenAIModel'\n",
            AFTER_BLOCK,
            "  before:",
            MUST_USE_FROM,
            id="old name only in a comment and a string",
        ),
        pytest.param(
            "    from pydantic_ai.models.other import OpenAIModel\n\n"
            '    model = OpenAIModel("gpt-5")\n',
            AFTER_BLOCK,
            "  before:",
            MUST_USE_FROM,
            id="before with the old name from another module",
        ),
        pytest.param(
            '    from pydantic_ai.models import OpenAIModel\n\n    model = OpenAIModel("gpt-5")\n',
            AFTER_BLOCK,
            "  before:",
            MUST_USE_FROM,
            id="before with a re-export of the old name",
        ),
        pytest.param(
            '    from pydantic_ai.models.openai import *\n\n    model = OpenAIModel("gpt-5")\n',
            AFTER_BLOCK,
            "  before:",
            MUST_USE_FROM,
            id="before with a star import",
        ),
        pytest.param(
            BEFORE_BLOCK,
            "    import pydantic_ai\n\n    model = None\n",
            "  after:",
            MUST_USE_TO,
            id="after without the new name",
        ),
        pytest.param(
            BEFORE_BLOCK,
            AFTER_BLOCK + "    from pydantic_ai.models.openai import OpenAIModel\n",
            "  after:",
            f"example.after must not use rename.from, {OLD_PATH}.",
            id="after with the old name",
        ),
    ],
)
def test_rename_examples_must_show_the_rename(
    repo: Path, before: str, after: str, starts: str, message: str
) -> None:
    edit(repo, BEFORE_BLOCK, before)
    edit(repo, AFTER_BLOCK, after)
    assert problems_in(repo) == [at(repo, starts, message)]


def test_rename_examples_may_use_names_through_modules(repo: Path) -> None:
    edit(
        repo,
        BEFORE_BLOCK,
        "    from pydantic_ai.models import openai\n\n"
        '    model = openai.OpenAIModel("gpt-5")  # OpenAIModel\n',
    )
    edit(
        repo,
        AFTER_BLOCK,
        "    from pydantic_ai.models import openai\n\n"
        '    model = openai.OpenAIChatModel("gpt-5")  # OpenAIModel\n',
    )
    assert problems_in(repo) == []


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            "    from pydantic_ai.models.openai import OpenAIModel as Model\n\n"
            '    model = Model("x")\n',
            "    from pydantic_ai.models.openai import OpenAIChatModel as Model\n\n"
            '    model = Model("x")\n',
            id="an alias of the name",
        ),
        pytest.param(
            "    import pydantic_ai.models.openai as models\n\n"
            '    model = models.OpenAIModel("x")\n',
            "    import pydantic_ai.models.openai as models\n\n"
            '    model = models.OpenAIChatModel("x")\n',
            id="an alias of the module",
        ),
        pytest.param(
            "    import pydantic_ai.models.openai\n\n"
            '    m = pydantic_ai.models.openai.OpenAIModel("x")\n',
            "    import pydantic_ai.models.openai\n\n"
            '    m = pydantic_ai.models.openai.OpenAIChatModel("x")\n',
            id="the full path",
        ),
        pytest.param(
            "    from pydantic_ai.models.openai import OpenAIModel\n\n"
            "    model = OpenAIModel.for_tests()\n",
            "    from pydantic_ai.models.openai import OpenAIChatModel\n\n"
            "    model = OpenAIChatModel.for_tests()\n",
            id="something in the renamed class",
        ),
    ],
)
def test_rename_examples_may_use_the_paths_through_aliases(
    repo: Path, before: str, after: str
) -> None:
    edit(repo, BEFORE_BLOCK, before)
    edit(repo, AFTER_BLOCK, after)
    assert problems_in(repo) == []


def set_rename(repo: Path, old: str, new: str) -> None:
    edit(repo, f"  from: {OLD_PATH}\n", f"  from: {old}\n")
    edit(repo, f"  to: {NEW_PATH}\n", f"  to: {new}\n")


AGENT = "    from pydantic_ai import Agent\n\n"
RESULT_PATHS = ("pydantic_ai.agent.AgentRunResult.data", "pydantic_ai.agent.AgentRunResult.output")


@pytest.mark.parametrize(
    ("old", "new", "before", "after"),
    [
        pytest.param(
            "pydantic_ai.Agent.run_old",
            "pydantic_ai.Agent.run_new",
            AGENT + '    agent = Agent("test")\n    agent.run_old("hi")\n',
            AGENT + '    agent = Agent("test")\n    agent.run_new("hi")\n',
            id="a method of an instance",
        ),
        pytest.param(
            *RESULT_PATHS,
            AGENT + '    print(Agent("test").run_sync("hi").data)\n',
            AGENT + '    print(Agent("test").run_sync("hi").output)\n',
            id="a property of the result of calls",
        ),
    ],
)
def test_members_of_objects_count_by_their_last_name(
    repo: Path, old: str, new: str, before: str, after: str
) -> None:
    set_rename(repo, old, new)
    edit(repo, BEFORE_BLOCK, before)
    edit(repo, AFTER_BLOCK, after)
    assert problems_in(repo) == []
    # The fix of example.before is example.after, so the fix check passes for the right reason.
    old_name, new_name = vn.distinguishing_names(old, new)
    fixed = vn.apply_rename(textwrap.dedent(before), old_name, new_name)
    assert fixed == textwrap.dedent(after)


@pytest.mark.parametrize(
    ("old", "new", "before", "after", "problems"),
    [
        pytest.param(
            *RESULT_PATHS,
            AGENT + '    result = Agent("test").run_sync("hi")\n    print(result.data)\n',
            AGENT
            + '    result = Agent("test").run_sync("hi")\n'
            + "    print(result.output, result.data)\n",
            [("  after:", f"example.after must not use rename.from, {RESULT_PATHS[0]}.")],
            id="after with the old member",
        ),
        pytest.param(
            *RESULT_PATHS,
            "    result = make()\n    print(result.data)\n",
            AGENT + '    print(Agent("test").run_sync("hi").output)\n',
            [
                (
                    "  before:",
                    f"example.before must use rename.from, {RESULT_PATHS[0]}, {THROUGH}.",
                )
            ],
            id="a member without anything from the package",
        ),
        pytest.param(
            "pydantic_ai.models.WRONG.OpenAIModel",
            "pydantic_ai.models.WRONG.OpenAIChatModel",
            '    from pydantic_ai.models import openai\n\n    model = openai.OpenAIModel("x")\n',
            "    from pydantic_ai.models import openai\n\n"
            '    model = openai.OpenAIChatModel("x")\n',
            [
                (
                    "  before:",
                    (
                        "example.before must use rename.from, "
                        f"pydantic_ai.models.WRONG.OpenAIModel, {THROUGH}."
                    ),
                ),
                (
                    "  after:",
                    (
                        "example.after must use rename.to, "
                        f"pydantic_ai.models.WRONG.OpenAIChatModel, {THROUGH}."
                    ),
                ),
            ],
            id="a wrong module, reached through an import of its parent",
        ),
        pytest.param(
            "pydantic_ai.models.WRONG.OpenAIModel",
            "pydantic_ai.models.WRONG.OpenAIChatModel",
            "    import pydantic_ai.models.openai\n\n"
            '    model = pydantic_ai.models.openai.OpenAIModel("x")\n',
            "    import pydantic_ai.models.openai\n\n"
            '    model = pydantic_ai.models.openai.OpenAIChatModel("x")\n',
            [
                (
                    "  before:",
                    (
                        "example.before must use rename.from, "
                        f"pydantic_ai.models.WRONG.OpenAIModel, {THROUGH}."
                    ),
                ),
                (
                    "  after:",
                    (
                        "example.after must use rename.to, "
                        f"pydantic_ai.models.WRONG.OpenAIChatModel, {THROUGH}."
                    ),
                ),
            ],
            # import a.b binds a, so the full path is resolved instead of counted as a member.
            id="a wrong module, reached through a plain import of the full module",
        ),
    ],
)
def test_members_of_objects_need_the_package_and_the_right_names(
    repo: Path, old: str, new: str, before: str, after: str, problems: list[tuple[str, str]]
) -> None:
    set_rename(repo, old, new)
    edit(repo, BEFORE_BLOCK, before)
    edit(repo, AFTER_BLOCK, after)
    assert problems_in(repo) == [at(repo, starts, message) for starts, message in problems]


def test_rename_paths_that_the_examples_do_not_use_are_rejected(repo: Path) -> None:
    # The short names still appear in the examples, but in another module.
    edit(repo, f"  from: {OLD_PATH}", "  from: this.module.does_not_exist.OpenAIModel")
    edit(repo, f"  to: {NEW_PATH}", "  to: this.module.does_not_exist.OpenAIChatModel")
    assert problems_in(repo) == [
        at(
            repo,
            "  before:",
            "example.before must use rename.from, this.module.does_not_exist.OpenAIModel, "
            f"{THROUGH}.",
        ),
        at(
            repo,
            "  after:",
            "example.after must use rename.to, this.module.does_not_exist.OpenAIChatModel, "
            f"{THROUGH}.",
        ),
    ]


def test_same_names_are_rejected(repo: Path) -> None:
    edit(
        repo,
        "  to: pydantic_ai.models.openai.OpenAIChatModel",
        "  to: pydantic_ai.models.openai.OpenAIModel",
    )
    assert problems_in(repo) == [at(repo, "rename:", "rename.from and rename.to are the same.")]


def test_moved_objects_are_checked_by_module(repo: Path) -> None:
    edit(
        repo,
        "  to: pydantic_ai.models.openai.OpenAIChatModel",
        "  to: pydantic_ai.models.chat.OpenAIModel",
    )
    edit(repo, AFTER_BLOCK, BEFORE_BLOCK)
    moved = "pydantic_ai.models.chat.OpenAIModel"
    assert problems_in(repo) == [
        at(repo, "  after:", f"example.after must not use rename.from, {OLD_PATH}."),
        at(repo, "  after:", f"example.after must use rename.to, {moved}, {THROUGH}."),
    ]

    edit(
        repo,
        f"  after: |\n{BEFORE_BLOCK}",
        "  after: |\n    from pydantic_ai.models import chat\n\n"
        '    model = chat.OpenAIModel("gpt-5")\n',
    )
    assert problems_in(repo) == []


@pytest.mark.parametrize(
    ("code", "paths"),
    [
        ("import a.b", {"a.b"}),
        ("import a.b\na.b.c()", {"a.b", "a", "a.b.c"}),
        ("import a.b as m\nm.c()", {"a.b", "a.b.c"}),
        ("from a import b", {"a.b"}),
        ("from a.b import c as d\nd.e()", {"a.b.c", "a.b.c.e"}),
        ("from a import b\nb.c.d", {"a.b", "a.b.c", "a.b.c.d"}),
        ("x = (a\n     .b())", {"x", "a", "a.b"}),
        ("x = f().b.c", {"x", "f", "b", "c"}),
        # Nothing binds these names, so they stay as written.
        ("from . import X\nX()", {"X"}),
        ("from a import *\nX()", {"X"}),
        ("x = f'{X}'", {"x", "X"}),
        ("# a.b\nx = 'a.b'", {"x"}),
        ("f(X=1)", {"f"}),
        ("def X():\n    pass", set()),
    ],
)
def test_resolved_uses_follow_the_imports(code: str, paths: set[str]) -> None:
    assert vn.resolved_uses(code) == paths


def test_distinguishing_names() -> None:
    assert vn.distinguishing_names("a.b.Old", "a.b.New") == ("Old", "New")
    assert vn.distinguishing_names("a.b.Name", "a.c.Name") == ("a.b", "a.c")


# --- IDs ----------------------------------------------------------------------


def renumber(repo: Path, notice_id: str) -> Path:
    """Give the notice another ID and move its file to match. Return the new path in the repo."""
    path = repo / NOTICE
    text = path.read_text(encoding="utf-8").replace("id: BW-2026-0001", f"id: {notice_id}")
    path.unlink()
    target = path.with_name(f"{notice_id}.yaml")
    target.write_text(text, encoding="utf-8")
    return target.relative_to(repo)


@pytest.mark.parametrize(
    "notice_id", ["BW-2026-0001", "BW-2026-9999", "BW-2026-10000", "BW-2026-12345678"]
)
def test_ids_have_four_to_eight_digits(repo: Path, notice_id: str) -> None:
    renumber(repo, notice_id)
    assert problems_in(repo) == []


@pytest.mark.parametrize(
    "notice_id",
    ["BW-2026-0000", "BW-2026-00001", "BW-2026-01000", "BW-2026-123456789", "BW-2026-012345678"],
)
def test_ids_have_no_more_than_eight_digits_and_no_extra_leading_zeros(
    repo: Path, notice_id: str
) -> None:
    rel = renumber(repo, notice_id)
    message = (
        f"id: {notice_id!r} is not valid. Expected an ID like BW-2026-0001: the year it was "
        "published, then a number of four to eight digits, without extra leading zeros."
    )
    assert problems_in(repo) == [f"{rel.as_posix()}:3: {message}"]


# --- Renamed keyword arguments ------------------------------------------------

KEYWORD_RENAME = (
    "rename:\n  keyword_of:\n    - pydantic_ai.Agent\n  from: mcp_servers\n  to: toolsets\n"
)
KEYWORD_BEFORE = '    from pydantic_ai import Agent\n\n    agent = Agent("test", mcp_servers=[])\n'
KEYWORD_AFTER = '    from pydantic_ai import Agent\n\n    agent = Agent("test", toolsets=[])\n'


@pytest.fixture
def keyword_repo(repo: Path) -> Path:
    """The repository with a notice for a renamed keyword argument, mcp_servers to toolsets."""
    edit(
        repo,
        "rename:\n  from: pydantic_ai.models.openai.OpenAIModel\n"
        "  to: pydantic_ai.models.openai.OpenAIChatModel\n",
        KEYWORD_RENAME,
    )
    edit(repo, BEFORE_BLOCK, KEYWORD_BEFORE)
    edit(repo, AFTER_BLOCK, KEYWORD_AFTER)
    return repo


def test_a_renamed_keyword_passes(keyword_repo: Path) -> None:
    assert problems_in(keyword_repo) == []


@pytest.mark.parametrize(
    ("block", "code", "starts", "message"),
    [
        pytest.param(
            KEYWORD_BEFORE,
            '    from pydantic_ai import Agent\n\n    agent = Agent("test")\n',
            "  before:",
            "example.before must pass mcp_servers= to pydantic_ai.Agent.",
            id="before without the old keyword",
        ),
        pytest.param(
            KEYWORD_BEFORE,
            "    from pydantic_ai import Agent\n\n"
            '    agent = Agent("test", **{"mcp_servers": []})\n',
            "  before:",
            "example.before must pass mcp_servers= to pydantic_ai.Agent.",
            id="old keyword only through **",
        ),
        pytest.param(
            KEYWORD_BEFORE,
            "    settings = dict(mcp_servers=[])\n",
            "  before:",
            "example.before must pass mcp_servers= to pydantic_ai.Agent.",
            id="old keyword passed to another callable",
        ),
        pytest.param(
            KEYWORD_AFTER,
            '    from pydantic_ai import Agent\n\n    agent = Agent("test")\n',
            "  after:",
            "example.after must pass toolsets= to pydantic_ai.Agent.",
            id="after without the new keyword",
        ),
        pytest.param(
            KEYWORD_AFTER,
            "    from pydantic_ai import Agent\n\n"
            '    agent = Agent("test", toolsets=[], mcp_servers=[])\n',
            "  after:",
            "example.after must not pass mcp_servers= to pydantic_ai.Agent.",
            id="after still passes the old keyword",
        ),
    ],
)
def test_renamed_keywords_must_be_passed(
    keyword_repo: Path, block: str, code: str, starts: str, message: str
) -> None:
    edit(keyword_repo, block, code)
    assert problems_in(keyword_repo) == [at(keyword_repo, starts, message)]


def test_a_variable_may_have_the_old_keyword_name(keyword_repo: Path) -> None:
    edit(
        keyword_repo,
        KEYWORD_BEFORE,
        "    from pydantic_ai import Agent\n\n    mcp_servers = []\n"
        '    agent = Agent("test", mcp_servers=mcp_servers)\n',
    )
    edit(
        keyword_repo,
        KEYWORD_AFTER,
        "    from pydantic_ai import Agent\n\n    mcp_servers = []\n"
        '    agent = Agent("test", toolsets=mcp_servers)\n',
    )
    assert problems_in(keyword_repo) == []


def test_keywords_of_several_callables_and_of_methods(keyword_repo: Path) -> None:
    edit(
        keyword_repo,
        "    - pydantic_ai.Agent\n",
        "    - pydantic_ai.Agent\n    - pydantic_ai.Agent.run_sync\n",
    )
    edit(keyword_repo, KEYWORD_BEFORE, '    agent.run_sync("hi", mcp_servers=[])\n')
    edit(keyword_repo, KEYWORD_AFTER, '    agent.run_sync("hi", toolsets=[])\n')
    assert problems_in(keyword_repo) == []

    edit(keyword_repo, '    agent.run_sync("hi", mcp_servers=[])\n', '    agent.run_sync("hi")\n')
    message = (
        "example.before must pass mcp_servers= to pydantic_ai.Agent or pydantic_ai.Agent.run_sync."
    )
    assert problems_in(keyword_repo) == [at(keyword_repo, "  before:", message)]


@pytest.mark.parametrize(
    ("old", "new", "starts", "message"),
    [
        pytest.param(
            "  from: mcp_servers",
            "  from: pydantic_ai.Agent.mcp_servers",
            "  from:",
            "rename.from: 'pydantic_ai.Agent.mcp_servers' is not valid. "
            "Expected a keyword argument name, e.g. toolsets.",
            id="dotted keyword name",
        ),
        pytest.param(
            "    - pydantic_ai.Agent\n",
            "    - Agent\n",
            "    - Agent",
            "rename.keyword_of[0]: 'Agent' is not valid. "
            "Expected a Python import path of two parts or more, e.g. pydantic_ai.Agent.",
            id="callable without its module",
        ),
    ],
)
def test_renamed_keywords_must_match_the_schema(
    keyword_repo: Path, old: str, new: str, starts: str, message: str
) -> None:
    edit(keyword_repo, old, new)
    assert problems_in(keyword_repo) == [at(keyword_repo, starts, message)]


def test_keyword_of_lists_at_least_one_callable(keyword_repo: Path) -> None:
    edit(keyword_repo, "  keyword_of:\n    - pydantic_ai.Agent\n", "  keyword_of: []\n")
    problems = problems_in(keyword_repo)
    assert len(problems) == 1
    assert problems[0].startswith(
        f"{NOTICE.as_posix()}:{line_number(keyword_repo, '  keyword_of:')}: "
    )
    assert "rename.keyword_of: [] " in problems[0]


def test_keyword_names_are_not_python_keywords(keyword_repo: Path) -> None:
    edit(keyword_repo, "  to: toolsets", "  to: class")
    message = "rename.to: class is a Python keyword, so no argument has it."
    # The example still passes toolsets=, which is not the new name any more.
    assert problems_in(keyword_repo) == [
        at(keyword_repo, "  to:", message),
        at(keyword_repo, "  after:", "example.after must pass class= to pydantic_ai.Agent."),
    ]


@pytest.mark.parametrize(
    ("code", "callables", "keywords"),
    [
        ("Agent(x=1, y=2)", {"Agent"}, {"x", "y"}),
        ("pydantic_ai.Agent(x=1)", {"Agent"}, {"x"}),
        ("agent.run(x=1)", {"run"}, {"x"}),
        ("other(x=1)", {"Agent"}, set()),
        ("Agent(**{'x': 1})", {"Agent"}, set()),
        ("f()(x=1)", {"f"}, set()),
    ],
)
def test_passed_keywords(code: str, callables: set[str], keywords: set[str]) -> None:
    assert vn.passed_keywords(code, callables) == keywords


# --- example.after is the fix of example.before -------------------------------


@pytest.mark.parametrize(
    ("code", "fixed"),
    [
        pytest.param("from a import Old\nx = Old()\n", "from a import New\nx = New()\n", id="name"),
        pytest.param("import a.Old\nx = a.Old.f()\n", "import a.New\nx = a.New.f()\n", id="module"),
        pytest.param(
            "from a.Old import f\nfrom a.Old import *\n",
            "from a.New import f\nfrom a.New import *\n",
            id="module of an import",
        ),
        pytest.param("from a import Old as O\n", "from a import New as O\n", id="alias"),
        pytest.param("Old.Old\n", "New.New\n", id="twice on a line"),
        pytest.param("f().Old\n", "f().New\n", id="attribute of a call"),
        pytest.param("x = (a\n     .Old())\n", "x = (a\n     .New())\n", id="over two lines"),
        pytest.param('t = "é"; Old()\n', 't = "é"; New()\n', id="after non-ASCII text"),
        pytest.param(
            "Old()  # Old\ns = 'Old'\n", "New()  # Old\ns = 'Old'\n", id="comment and string"
        ),
        pytest.param("f(Old=1)\ndef Old(): pass\n", "f(Old=1)\ndef Old(): pass\n", id="not uses"),
    ],
)
def test_apply_rename_changes_exactly_what_uses_sees(code: str, fixed: str) -> None:
    assert vn.apply_rename(code, "Old", "New") == fixed
    assert not any("Old" in use.parts for use in vn.name_uses(fixed))


@pytest.mark.parametrize(
    ("code", "fixed"),
    [
        ("Agent(mcp_servers=[], x=1)\n", "Agent(toolsets=[], x=1)\n"),
        ("agent.run(\n    mcp_servers=[],\n)\n", "agent.run(\n    toolsets=[],\n)\n"),
        ('t = "é"; Agent(mcp_servers=[])\n', 't = "é"; Agent(toolsets=[])\n'),
        ("other(mcp_servers=[])\n", "other(mcp_servers=[])\n"),
        ('Agent(**{"mcp_servers": []})\n', 'Agent(**{"mcp_servers": []})\n'),
    ],
)
def test_apply_keyword_rename_changes_exactly_what_passed_keywords_sees(
    code: str, fixed: str
) -> None:
    callables = {"Agent", "run"}
    assert vn.apply_keyword_rename(code, callables, "mcp_servers", "toolsets") == fixed
    assert "mcp_servers" not in vn.passed_keywords(fixed, callables)


# The fix replaces from the end of a line, so that each replacement leaves the places
# before it where they are. That only shows when the new name has another length.


def test_two_renamed_keywords_on_one_line_are_both_fixed() -> None:
    code = "a, b = Agent('test', mcp_servers=[]), Agent('test', mcp_servers=[])\n"
    fixed = "a, b = Agent('test', toolsets=[]), Agent('test', toolsets=[])\n"
    # Calls match by the last part of their name, so the callable is "Agent".
    assert vn.apply_keyword_rename(code, {"Agent"}, "mcp_servers", "toolsets") == fixed


def test_two_renamed_names_on_one_line_are_both_fixed() -> None:
    code = "a, b = OpenAIModel('gpt-5'), OpenAIModel('gpt-4.1')\n"
    fixed = "a, b = OpenAIChatModel('gpt-5'), OpenAIChatModel('gpt-4.1')\n"
    assert vn.apply_rename(code, "OpenAIModel", "OpenAIChatModel") == fixed


def test_a_notice_with_two_renamed_names_on_one_line_is_valid(repo: Path) -> None:
    edit(repo, '    model = OpenAIModel("gpt-5")', '    a, b = OpenAIModel("x"), OpenAIModel("y")')
    edit(
        repo,
        '    model = OpenAIChatModel("gpt-5")',
        '    a, b = OpenAIChatModel("x"), OpenAIChatModel("y")',
    )
    assert problems_in(repo) == []


def test_a_notice_with_two_renamed_keywords_on_one_line_is_valid(keyword_repo: Path) -> None:
    edit(
        keyword_repo,
        '    agent = Agent("test", mcp_servers=[])',
        '    a, b = Agent("test", mcp_servers=[]), Agent("test", mcp_servers=[])',
    )
    edit(
        keyword_repo,
        '    agent = Agent("test", toolsets=[])',
        '    a, b = Agent("test", toolsets=[]), Agent("test", toolsets=[])',
    )
    assert problems_in(keyword_repo) == []


FIX = "example.after must be example.before with the rename applied, and nothing else"


@pytest.mark.parametrize(
    ("old", "new", "starts", "detail"),
    [
        pytest.param(
            AFTER_BLOCK,
            AFTER_BLOCK + "    print(model)\n",
            "    print(model)",
            "line 4 is extra",
            id="extra line",
        ),
        pytest.param(
            '    model = OpenAIChatModel("gpt-5")',
            '    model = OpenAIChatModel( "gpt-5" )',
            "    model = OpenAIChatModel(",
            "line 3 should be 'model = OpenAIChatModel(\"gpt-5\")'",
            id="other formatting",
        ),
        pytest.param(
            AFTER_BLOCK,
            "    from pydantic_ai.models.openai import OpenAIChatModel\n"
            '    model = OpenAIChatModel("gpt-5")\n',
            "    model = OpenAIChatModel",
            "line 2 should be ''",
            id="missing line",
        ),
    ],
)
def test_after_must_be_the_fix_of_before(
    repo: Path, old: str, new: str, starts: str, detail: str
) -> None:
    edit(repo, old, new)
    assert problems_in(repo) == [at(repo, starts, f"{FIX}: {detail}.")]


def test_after_of_a_renamed_keyword_must_be_the_fix_of_before(keyword_repo: Path) -> None:
    edit(keyword_repo, 'Agent("test", toolsets=[])', 'Agent("other", toolsets=[])')
    detail = "line 3 should be 'agent = Agent(\"test\", toolsets=[])'"
    assert problems_in(keyword_repo) == [
        at(keyword_repo, '    agent = Agent("other"', f"{FIX}: {detail}.")
    ]


def test_a_missing_final_newline_is_reported(repo: Path) -> None:
    edit(repo, "  after: |\n", "  after: |-\n")
    message = f"{FIX}: only the newline at the end differs."
    assert problems_in(repo) == [at(repo, "  after:", message)]


def test_a_move_to_another_module_is_left_for_the_engine(repo: Path) -> None:
    # Renamed and moved: the fix also changes the import, which only the engine can do.
    edit(
        repo,
        "  to: pydantic_ai.models.openai.OpenAIChatModel",
        "  to: pydantic_ai.models.chat.OpenAIChatModel",
    )
    edit(
        repo,
        "    from pydantic_ai.models.openai import OpenAIChatModel\n",
        "    from pydantic_ai.models.chat import OpenAIChatModel\n",
    )
    assert problems_in(repo) == []


# --- README -------------------------------------------------------------------


def test_readme_example_must_match_the_notice(repo: Path) -> None:
    notice_text = (repo / NOTICE).read_text(encoding="utf-8")
    readme = repo / vn.README_FILE
    readme.write_text(f"# Test\n\n```yaml\n{notice_text}```\n", encoding="utf-8")
    assert problems_in(repo) == []

    readme.write_text(f"# Test\n\n```yml\n{notice_text}# changed\n```\n", encoding="utf-8")
    assert problems_in(repo) == [
        f"README.md:3: The example must match {NOTICE.as_posix()} exactly."
    ]

    quoted = notice_text.replace("id: BW-2026-0001", 'id: "BW-2026-0001"')
    readme.write_text(f"```yaml\n{quoted}```\n", encoding="utf-8")
    assert problems_in(repo) == [
        f"README.md:1: The example must match {NOTICE.as_posix()} exactly."
    ]


def test_readme_example_must_be_a_real_notice(repo: Path) -> None:
    readme = repo / vn.README_FILE
    block = '```yaml\nschema_version: "0.3"\nid: BW-2026-9999\n```\n'
    readme.write_text(f"\n{block}", encoding="utf-8")
    assert problems_in(repo) == [
        "README.md:2: The example shows BW-2026-9999, but there is no notice with that ID."
    ]


def test_readme_example_is_compared_with_an_invalid_notice_too(repo: Path) -> None:
    edit(repo, 'breaks_in: "2.0.0"', "breaks_in: 2.0")
    message = 'Write 2.0 in quotes, as "2.0", so that YAML reads it as text.'
    notice_problem = at(repo, "breaks_in:", message)
    notice_text = (repo / NOTICE).read_text(encoding="utf-8")
    readme = repo / vn.README_FILE

    # The README shows the notice as it is, so only the notice's own problem is reported.
    readme.write_text(f"```yaml\n{notice_text}```\n", encoding="utf-8")
    assert problems_in(repo) == [notice_problem]

    readme.write_text(f"```yaml\n{notice_text}# an older copy\n```\n", encoding="utf-8")
    assert problems_in(repo) == [
        notice_problem,
        f"README.md:1: The example must match {NOTICE.as_posix()} exactly.",
    ]


@pytest.mark.parametrize(
    "block",
    ['schema_version: "0.3"\nid: [BW\n', 'schema_version: "0.3"\npublished: 2026-02-30\n'],
)
def test_readme_example_must_be_valid_yaml(repo: Path, block: str) -> None:
    (repo / vn.README_FILE).write_text(f"```yaml\n{block}```\n", encoding="utf-8")
    assert problems_in(repo) == ["README.md:1: The example notice is not valid YAML."]


@pytest.mark.parametrize("block", ["name: example\n", "id: x\n", "- a list\n", "[not yaml\n"])
def test_other_yaml_blocks_in_the_readme_are_ignored(repo: Path, block: str) -> None:
    (repo / vn.README_FILE).write_text(f"```yaml\n{block}```\n", encoding="utf-8")
    assert problems_in(repo) == []


def test_a_repository_without_readme_is_fine(repo: Path) -> None:
    (repo / vn.README_FILE).unlink()
    assert problems_in(repo) == []


# --- Output -------------------------------------------------------------------


def test_github_annotations_are_escaped() -> None:
    problem = vn.Problem(Path("a,b:c.yaml"), 3, "50% wrong\r\nsecond line")
    assert (
        problem.github_annotation()
        == "::error file=a%2Cb%3Ac.yaml,line=3::50%25 wrong%0D%0Asecond line"
    )
    assert vn.github_error(Path("x.yaml"), "no line") == "::error file=x.yaml::no line"


def test_main_returns_an_error_code_only_when_something_is_wrong(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert vn.main(["--root", str(repo)]) == 0
    assert capsys.readouterr().out == "All 1 notice(s) are valid.\n"

    edit(repo, 'breaks_in: "2.0.0"', "breaks_in: 2.0")
    assert vn.main(["--root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "1 problem(s) found in 1 notice(s)." in out
    assert "::error" not in out


def test_main_annotates_problems_in_github_actions(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    edit(repo, 'breaks_in: "2.0.0"', "breaks_in: 2.0")
    assert vn.main(["--root", str(repo)]) == 1
    line = line_number(repo, "breaks_in:")
    assert (
        f"::error file={NOTICE.as_posix()},line={line}::Write 2.0 in quotes"
        in capsys.readouterr().out
    )
