#!/usr/bin/env python3
"""Check that every notice in the registry is valid.

Run it from anywhere:

    python tools/validate_notices.py

Every file under ``notices/`` must be a notice named ``<id>.yaml``. Only
Markdown files, such as ``notices/LICENSE.md``, may sit next to the notices,
and hidden files, such as ``.DS_Store``, are ignored. For every notice, this
checks that:

- it is UTF-8 text with exactly one YAML document, written in plain YAML:
  no anchors, aliases or tags, no duplicate keys, and no unquoted value that
  YAML reads as a number, date or boolean, so that a version such as "2.10"
  keeps every digit,
- it matches the JSON Schema in ``schema/``,
- it is stored at ``notices/<ecosystem>/<package>/<id>.yaml`` and its ID is unique,
- ``package.aliases`` does not repeat ``package.name``,
- its dates are real dates and not in the future, the year in its ID is the
  year it was published, and ``modified`` is not before ``published``,
- ``works_since`` is not later than ``breaks_in``,
- its example code is valid Python, and a rename's examples use the old name
  before the change and only the new name after it. Imports, names and
  attributes count as uses; comments, strings and keyword arguments do not.

It also checks that every notice shown in ``README.md`` matches its file exactly.

Each problem is printed with its file and line. In GitHub Actions it is also
reported as an annotation on the pull request. The exit code is 1 if there is
any problem, otherwise 0.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, ValidationError
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = Path("schema/notice-0.1.schema.json")
NOTICES_DIR = Path("notices")
README_FILE = Path("README.md")

README_YAML_BLOCK = re.compile(r"^```ya?ml\n(.*?)^```$", re.MULTILINE | re.DOTALL)

# Every value in a notice is text. A value YAML resolves to any other type was
# written without quotes, e.g. 2.10, which YAML reads as the number 2.1.
# Empty values are left to the schema, which says that they are missing.
TEXT_TAGS = frozenset({"tag:yaml.org,2002:str", "tag:yaml.org,2002:null"})


@dataclass(frozen=True, order=True)
class Problem:
    """A problem found at a line of a file."""

    file: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.file.as_posix()}:{self.line}: {self.message}"

    def github_annotation(self) -> str:
        return github_error(self.file, self.message, self.line)


class InvalidNoticeError(Exception):
    """A notice file that is not plain YAML or does not match the schema."""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__("; ".join(str(problem) for problem in problems))
        self.problems = problems


@dataclass(frozen=True)
class Notice:
    """A notice that matches the schema, with its YAML tree for line numbers."""

    rel: Path
    data: dict[str, Any]
    tree: yaml.Node

    def problem(self, field: str, message: str) -> Problem:
        """A problem at a field such as ``example.before``."""
        return Problem(self.rel, line_of(self.tree, field.split(".")), message)


def github_error(file: Path, message: str, line: int | None = None) -> str:
    """Format an error as a GitHub Actions annotation."""
    location = f"file={_escape_property(file.as_posix())}"
    if line is not None:
        location += f",line={line}"
    return f"::error {location}::{_escape_data(message)}"


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def line_of(node: yaml.Node, path: Iterable[str | int]) -> int:
    """Return the 1-based line of the value at ``path``, or of its closest parent."""
    line: int = node.start_mark.line + 1
    for part in path:
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if key.value == part:
                    line = key.start_mark.line + 1
                    node = value
                    break
            else:
                return line
        elif (
            isinstance(node, yaml.SequenceNode) and isinstance(part, int) and part < len(node.value)
        ):
            node = node.value[part]
            line = node.start_mark.line + 1
        else:
            return line
    return line


def value_at(node: yaml.Node, keys: Iterable[str]) -> yaml.Node:
    """Return the node of the value at ``keys``, which must exist."""
    for key in keys:
        node = next(value for name, value in node.value if name.value == key)
    return node


def field_name(path: Iterable[str | int]) -> str:
    """Name a field by its path, e.g. ``example.before`` or ``references[1]``."""
    name = ""
    for part in path:
        if isinstance(part, int):
            name += f"[{part}]"
        else:
            name += f".{part}" if name else part
    return name


def describe(error: ValidationError) -> str:
    """Turn a schema error into a message that says what is expected."""
    field = field_name(error.absolute_path)
    description = error.schema.get("description") if isinstance(error.schema, Mapping) else None
    if error.validator == "pattern" and description:
        message = f"{error.instance!r} is not valid. Expected {description}"
    elif error.validator == "not" and not field:
        message = "`rename` is only allowed when `type` is `rename`."
    elif error.validator == "type" and error.instance is None:
        message = "has no value."
    else:
        message = error.message
    return f"{field}: {message}" if field else message


def distinguishing_names(old: str, new: str) -> tuple[str, str]:
    """Return the part of each dotted name that tells them apart.

    For a renamed object this is the object's name, e.g. OpenAIModel and
    OpenAIChatModel. For an object that moved to another module under the
    same name, it is the module path.
    """
    old_module, _, old_name = old.rpartition(".")
    new_module, _, new_name = new.rpartition(".")
    if old_name != new_name:
        return old_name, new_name
    return old_module, new_module


def used_names(code: str) -> set[tuple[str, ...]]:
    """Return the names that Python code uses, as dotted paths split into parts.

    ``import a.b``, ``from a import b`` and ``a.b`` all give ``("a", "b")``.
    Comments, strings, keyword arguments and definitions are not uses.
    The code must be valid Python.
    """
    names: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Import):
            names.update(tuple(alias.name.split(".")) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = tuple(node.module.split(".")) if node.module else ()
            names.update((*module, alias.name) for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add((node.id,))
        elif isinstance(node, ast.Attribute):
            names.add(_attribute_path(node) or (node.attr,))
    return names


def _attribute_path(node: ast.Attribute) -> tuple[str, ...] | None:
    """``a.b.c`` gives ``("a", "b", "c")``; ``f().c``, which does not start with a name, None."""
    parts = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    return (value.id, *reversed(parts)) if isinstance(value, ast.Name) else None


def uses(code: str, name: str) -> bool:
    """Whether Python code uses ``name``.

    A name without dots matches any part of a used path, so ``OpenAIModel``
    is used in ``openai.OpenAIModel()``. A dotted name must match the start of
    one, so ``pydantic_ai.models.openai`` is used in both
    ``from pydantic_ai.models.openai import OpenAIModel`` and
    ``from pydantic_ai.models import openai``.
    """
    parts = tuple(name.split("."))
    if len(parts) == 1:
        return any(name in path for path in used_names(code))
    return any(path[: len(parts)] == parts for path in used_names(code))


def load_validator(root: Path) -> Draft202012Validator:
    """Return a validator for the notice schema of the repository at ``root``."""
    schema = json.loads((root / SCHEMA_FILE).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def read_notice(path: Path, rel: Path, validator: Draft202012Validator) -> Notice:
    """Read a notice file and check it against the schema.

    ``rel`` is the path shown in problems. Raise InvalidNoticeError with the
    problems found if the file is not plain YAML or does not match the schema.
    """
    tree = _read_tree(path, rel)
    # Checked before the tree becomes data: YAML crashes on unquoted dates such as 2026-02-30.
    problems = [*_values_not_written_as_text(rel, tree), *_duplicate_keys(rel, tree)]
    if problems:
        raise InvalidNoticeError(sorted(problems))
    try:
        data = yaml.constructor.SafeConstructor().construct_document(tree)
    except yaml.YAMLError as error:
        raise InvalidNoticeError([_yaml_problem(rel, error)]) from error
    if data is None:
        raise InvalidNoticeError([Problem(rel, 1, "The notice file is empty.")])
    problems = [
        Problem(rel, line_of(tree, error.absolute_path), describe(error))
        for error in validator.iter_errors(data)
    ]
    if problems:
        raise InvalidNoticeError(sorted(problems))
    return Notice(rel, data, tree)


def _read_tree(path: Path, rel: Path) -> yaml.Node:
    """Read a file as one plain YAML document: no anchors, aliases or tags."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise InvalidNoticeError([Problem(rel, 1, "A notice file must be UTF-8 text.")]) from error
    try:
        events = list(yaml.parse(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as error:
        raise InvalidNoticeError([_yaml_problem(rel, error)]) from error

    problems = list(_not_plain_yaml(rel, events))
    documents = [event for event in events if isinstance(event, yaml.DocumentStartEvent)]
    if not documents:
        problems.append(Problem(rel, 1, "The notice file is empty."))
    elif len(documents) > 1:
        line = _line(documents[1])
        problems.append(Problem(rel, line, "A notice file must contain only one YAML document."))
    if problems:
        raise InvalidNoticeError(sorted(problems))
    tree: yaml.Node = next(yaml.compose_all(text, Loader=yaml.SafeLoader))
    return tree


def _line(event: yaml.Event) -> int:
    return event.start_mark.line + 1 if event.start_mark else 1


def _yaml_problem(rel: Path, error: yaml.YAMLError) -> Problem:
    mark = getattr(error, "problem_mark", None)
    reason = getattr(error, "problem", None) or str(error)
    return Problem(rel, mark.line + 1 if mark else 1, f"Invalid YAML: {reason}")


def _not_plain_yaml(rel: Path, events: Iterable[yaml.Event]) -> Iterator[Problem]:
    """Anchors, aliases and tags, which make notices harder to read and to process safely."""
    for event in events:
        line = _line(event)
        if isinstance(event, yaml.AliasEvent):
            yield Problem(rel, line, f"Notices are plain YAML: remove the alias *{event.anchor}.")
        elif isinstance(event, yaml.NodeEvent) and event.anchor is not None:
            yield Problem(rel, line, f"Notices are plain YAML: remove the anchor &{event.anchor}.")
        elif getattr(event, "tag", None) is not None:
            yield Problem(rel, line, "Notices are plain YAML: remove the tag.")


def _children(node: yaml.Node) -> list[yaml.Node]:
    """The keys and values of a mapping, or the items of a list."""
    if isinstance(node, yaml.MappingNode):
        return [child for pair in node.value for child in pair]
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    return []


def _values_not_written_as_text(rel: Path, node: yaml.Node) -> Iterator[Problem]:
    if isinstance(node, yaml.ScalarNode) and node.tag not in TEXT_TAGS:
        message = f'Write {node.value} in quotes, as "{node.value}", so that YAML reads it as text.'
        yield Problem(rel, node.start_mark.line + 1, message)
    for child in _children(node):
        yield from _values_not_written_as_text(rel, child)


def _duplicate_keys(rel: Path, node: yaml.Node) -> Iterator[Problem]:
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key, _ in node.value:
            if not isinstance(key, yaml.ScalarNode):
                continue  # Not hashable as a key, so loading reports it.
            if key.value in seen:
                yield Problem(rel, key.start_mark.line + 1, f"Duplicate key: {key.value}")
            seen.add(key.value)
    for child in _children(node):
        yield from _duplicate_keys(rel, child)


def check_notice(
    root: Path,
    path: Path,
    validator: Draft202012Validator,
    seen_ids: dict[str, Path],
    today: dt.date,
) -> list[Problem]:
    """Check one notice file. Add its ID to ``seen_ids`` if it matches the schema."""
    try:
        notice = read_notice(path, path.relative_to(root), validator)
    except InvalidNoticeError as error:
        return error.problems

    problems = [
        *_check_location(notice),
        *_check_dates(notice, today),
        *_check_versions(notice),
        *_check_examples(notice),
    ]
    notice_id: str = notice.data["id"]
    if notice_id in seen_ids:
        used_by = seen_ids[notice_id].as_posix()
        problems.append(notice.problem("id", f"ID {notice_id} is already used by {used_by}."))
    else:
        seen_ids[notice_id] = notice.rel
    return sorted(problems)


def _check_location(notice: Notice) -> Iterator[Problem]:
    package = notice.data["package"]
    name = f"{notice.data['id']}.yaml"
    expected = NOTICES_DIR / package["ecosystem"].lower() / package["name"] / name
    if notice.rel.as_posix() != expected.as_posix():
        message = (
            f"Must be stored at {expected.as_posix()}, from its id, "
            "package.ecosystem and package.name."
        )
        yield notice.problem("id", message)
    if package["name"] in package.get("aliases", []):
        yield notice.problem("package.aliases", "package.aliases must not repeat package.name.")


def _check_dates(notice: Notice, today: dt.date) -> Iterator[Problem]:
    dates: dict[str, dt.date] = {}
    for field in ("published", "modified"):
        if field in notice.data:
            try:
                dates[field] = dt.date.fromisoformat(notice.data[field])
            except ValueError:
                yield notice.problem(field, f"{field}: {notice.data[field]!r} is not a real date.")
    for field, date in dates.items():
        # One day of margin, because it is already tomorrow in some time zones.
        if date > today + dt.timedelta(days=1):
            yield notice.problem(field, f"{field}: {date} is in the future.")

    published, modified = dates.get("published"), dates.get("modified")
    if published is None:
        return
    notice_id = notice.data["id"]
    if int(notice_id.split("-")[1]) != published.year:
        message = f"The year in {notice_id} must be the year it was published ({published.year})."
        yield notice.problem("id", message)
    if modified is not None and modified < published:
        yield notice.problem("modified", "modified must not be before published.")


def _check_versions(notice: Notice) -> Iterator[Problem]:
    breaks_in, works_since = notice.data["breaks_in"], notice.data.get("works_since")
    if works_since is not None and Version(works_since) > Version(breaks_in):
        message = f"works_since ({works_since}) must not be later than breaks_in ({breaks_in})."
        yield notice.problem("works_since", message)


def _check_examples(notice: Notice) -> Iterator[Problem]:
    valid = True
    for which in ("before", "after"):
        try:
            compile(notice.data["example"][which], f"example.{which}", "exec", dont_inherit=True)
        except SyntaxError as error:
            valid = False
            line = _code_line(notice.tree, which, error.lineno or 1)
            message = f"example.{which} is not valid Python: {error.msg}"
            yield Problem(notice.rel, line, message)
    if valid and notice.data["type"] == "rename":
        yield from _check_rename(notice)


def _code_line(tree: yaml.Node, which: str, lineno: int) -> int:
    """Return the line in the file of a line of example code.

    This is exact for a literal block (``|``), where the code starts on the
    next line and keeps its lines. Other styles join lines, so for them it is
    the line where the code starts.
    """
    node = value_at(tree, ("example", which))
    start: int = node.start_mark.line + 1
    literal = isinstance(node, yaml.ScalarNode) and node.style == "|"
    return start + lineno if literal else start


def _check_rename(notice: Notice) -> Iterator[Problem]:
    old, new = notice.data["rename"]["from"], notice.data["rename"]["to"]
    if old == new:
        yield notice.problem("rename", "rename.from and rename.to are the same.")
        return
    old_name, new_name = distinguishing_names(old, new)
    before, after = notice.data["example"]["before"], notice.data["example"]["after"]
    if not uses(before, old_name):
        yield notice.problem("example.before", f"example.before must use the old name {old_name}.")
    if not uses(after, new_name):
        yield notice.problem("example.after", f"example.after must use the new name {new_name}.")
    # When only the module changed, the old module may still be needed for other names.
    renamed = old.rpartition(".")[2] != new.rpartition(".")[2]
    if renamed and uses(after, old_name):
        message = f"example.after must not use the old name {old_name}."
        yield notice.problem("example.after", message)


def check_readme(root: Path, notices: Mapping[str, Path]) -> list[Problem]:
    """Check that every notice shown in README.md matches its file exactly, valid or not.

    ``notices`` maps each ID to the file of its notice.
    """
    readme = root / README_FILE
    if not readme.is_file():
        return []
    text = readme.read_text(encoding="utf-8")
    problems = []
    for match in README_YAML_BLOCK.finditer(text):
        block = match.group(1)
        line = text.count("\n", 0, match.start()) + 1
        try:
            shown = yaml.safe_load(block)
        except (yaml.YAMLError, ValueError):
            # ValueError: YAML fails this way on unquoted dates that do not exist.
            if "schema_version:" in block:
                problems.append(Problem(README_FILE, line, "The example notice is not valid YAML."))
            continue
        if not (isinstance(shown, dict) and "schema_version" in shown and "id" in shown):
            continue
        notice_id = str(shown["id"])
        path = notices.get(notice_id)
        if path is None:
            message = f"The example shows {notice_id}, but there is no notice with that ID."
            problems.append(Problem(README_FILE, line, message))
        elif block != (root / path).read_text(encoding="utf-8"):
            message = f"The example must match {path.as_posix()} exactly."
            problems.append(Problem(README_FILE, line, message))
    return problems


def registry_files(root: Path) -> list[Path]:
    """Every file under ``notices/``, except hidden files such as ``.DS_Store``."""
    return sorted(
        path
        for path in (root / NOTICES_DIR).rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def validate(root: Path, today: dt.date | None = None) -> tuple[list[Problem], int]:
    """Check the whole registry under ``root``. Return the problems and the number of notices."""
    if today is None:
        today = dt.datetime.now(dt.UTC).date()
    validator = load_validator(root)

    problems: list[Problem] = []
    seen_ids: dict[str, Path] = {}
    files_by_name: dict[str, Path] = {}
    count = 0
    for path in registry_files(root):
        if path.suffix == ".md":
            continue
        if path.suffix != ".yaml":
            message = "Notice files must end in .yaml, and other files here in .md."
            problems.append(Problem(path.relative_to(root), 1, message))
            continue
        count += 1
        files_by_name.setdefault(path.stem, path.relative_to(root))
        problems += check_notice(root, path, validator, seen_ids, today)
    # A notice that cannot be read is found by its file name, which must be its ID.
    problems += check_readme(root, {**files_by_name, **seen_ids})
    return problems, count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that every notice in the registry is valid."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="repository root (default: the repository this script is in)",
    )
    args = parser.parse_args(argv)

    problems, count = validate(args.root.resolve())
    in_github = os.environ.get("GITHUB_ACTIONS") == "true"
    for problem in problems:
        print(problem)
        if in_github:
            print(problem.github_annotation())
    if problems:
        print(f"\n{len(problems)} problem(s) found in {count} notice(s).")
        return 1
    print(f"All {count} notice(s) are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
