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
  attributes count as uses; comments, strings and keyword arguments do not,
- for a renamed keyword argument (``rename.keyword_of``), the names are not
  Python keywords, ``example.before`` passes the old keyword to one of the
  callables, and ``example.after`` passes the new keyword and not the old one,
- a rename's ``example.after`` is ``example.before`` with the rename applied, and
  nothing else, when the rename stays in one module or renames a keyword
  argument. A move to another module is left for the engine to check.

It also checks that every notice shown in ``README.md`` matches its file exactly.

Each problem is printed with its file and line. In GitHub Actions it is also
reported as an annotation on the pull request. The exit code is 1 if there is
any problem, otherwise 0.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import io
import itertools
import json
import keyword
import os
import re
import sys
import tokenize
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator, ValidationError
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent
# The tools read only the latest format. Earlier schemas stay in schema/, unchanged.
SCHEMA_FILE = Path("schema/notice-0.3.schema.json")
NOTICES_DIR = Path("notices")
README_FILE = Path("README.md")

README_YAML_BLOCK = re.compile(r"^```ya?ml\n(.*?)^```$", re.MULTILINE | re.DOTALL)

# Every value in a notice is text. A value YAML resolves to any other type was
# written without quotes, e.g. 2.10, which YAML reads as the number 2.1.
# Empty values are left to the schema, which says that they are missing.
TEXT_TAGS = frozenset({"tag:yaml.org,2002:str", "tag:yaml.org,2002:null"})

# What the schema's "not" rules forbid, by the path of the field they name.
NOT_ALLOWED = {
    "rename": "`rename` is only allowed when `type` is `rename`.",
    "instructions": (
        "`instructions` is not allowed when `type` is `rename`: a rename is fixed automatically."
    ),
    "affects": (
        "`affects` is not allowed when `type` is `rename`: `rename.from` names what breaks."
    ),
    "example.fails_with.after": (
        "`example.fails_with.after` is only allowed with `works_since`: "
        "it says how `example.after` fails on the release before `works_since`."
    ),
}

# What the schema requires only for some notices, by the path of the field.
REQUIRED = {
    "affects": (
        "`affects` is required when `type` is not `rename`: it names the code that breaks."
    ),
    "example.fails_with": (
        "`example.fails_with` is required when `type` is not `rename`: "
        "it says how `example.before` fails on `breaks_in`."
    ),
    "example.fails_with.after": (
        "`example.fails_with.after` is required when `type` is not `rename` and `works_since` "
        "is set: it says how `example.after` fails on the release before `works_since`."
    ),
}


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
    if error.validator == "not":
        rule = cast("Mapping[str, list[str]]", error.validator_value)
        if "required" in rule:
            # A field that a rule forbids, written as {"required": [<field>]} at its parent.
            (name,) = rule["required"]
            return NOT_ALLOWED[field_name([*error.absolute_path, name])]
    if error.validator == "required":
        # A rule that requires a field only for some notices names that one field.
        names = cast("list[str]", error.validator_value)
        required = REQUIRED.get(field_name([*error.absolute_path, names[0]]))
        if len(names) == 1 and required is not None:
            return required
    message = _expected(error)
    return f"{field}: {message}" if field else message


def _expected(error: ValidationError) -> str:
    description = error.schema.get("description") if isinstance(error.schema, Mapping) else None
    if error.validator in {"pattern", "const"} and description:
        return f"{error.instance!r} is not valid. Expected {description}"
    if error.validator == "not":
        # The other "not" rule is the one for text: it must not match a newline.
        return "must be one line."
    if error.validator == "type" and error.instance is None:
        return "has no value."
    return error.message


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


# Where a name is written in code: a 1-based line and a 0-based character column.
Place = tuple[int, int]
Positioned = ast.expr | ast.stmt | ast.alias | ast.keyword


@dataclass(frozen=True)
class NameUse:
    """A dotted name that code uses, and where each of its parts is written.

    ``places`` has one entry for each part: its place, or None for the ``*``
    of ``from a import *``.
    """

    parts: tuple[str, ...]
    places: tuple[Place | None, ...]


class _Source:
    """Places in Python code: ast's UTF-8 offsets as character columns, and the name tokens."""

    def __init__(self, code: str) -> None:
        self.lines = code.split("\n")
        tokens = tokenize.generate_tokens(io.StringIO(code).readline)
        self.names = [
            (token.start, token.string) for token in tokens if token.type == tokenize.NAME
        ]

    def start(self, node: Positioned) -> Place:
        return node.lineno, self._column(node.lineno, node.col_offset)

    def end(self, node: Positioned) -> Place:
        line = cast("int", node.end_lineno)
        return line, self._column(line, cast("int", node.end_col_offset))

    def names_in(self, start: Place, end: Place) -> list[Place]:
        """The places of the names from ``start`` up to ``end``, except ``from`` and ``import``."""
        return [place for place, name in self.names if start <= place < end and name not in IMPORT]

    def _column(self, line: int, offset: int) -> int:
        return len(self.lines[line - 1].encode("utf-8")[:offset].decode("utf-8"))


IMPORT = frozenset({"from", "import"})


def name_uses(code: str) -> list[NameUse]:
    """Return the names that Python code uses, with the places of their parts.

    ``import a.b``, ``from a import b`` and ``a.b`` all use ``("a", "b")``.
    Comments, strings, keyword arguments and definitions are not uses.
    The code must be valid Python.
    """
    source = _Source(code)
    found: list[NameUse] = []
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = tuple(alias.name.split("."))
                places = source.names_in(source.start(alias), source.end(alias))[: len(parts)]
                found.append(NameUse(parts, tuple(places)))
        elif isinstance(node, ast.ImportFrom):
            module = tuple(node.module.split(".")) if node.module else ()
            module_places = source.names_in(source.start(node), source.start(node.names[0]))
            for alias in node.names:
                name = source.names_in(source.start(alias), source.end(alias))
                place = name[0] if name else None
                found.append(NameUse((*module, alias.name), (*module_places, place)))
        elif isinstance(node, ast.Name):
            found.append(NameUse((node.id,), (source.start(node),)))
        elif isinstance(node, ast.Attribute):
            found.append(_attribute_use(node, source))
    return found


def _attribute_use(node: ast.Attribute, source: _Source) -> NameUse:
    """``a.b.c`` uses ``("a", "b", "c")``, and ``f().c``, which starts with a call, ``("c",)``."""
    chain = [node]
    value = node.value
    while isinstance(value, ast.Attribute):
        chain.append(value)
        value = value.value
    # An attribute ends with its name, so the name starts that many characters before the end.
    attributes = [(link.attr, source.end(link)) for link in reversed(chain)]
    places = [(line, column - len(attr)) for attr, (line, column) in attributes]
    if isinstance(value, ast.Name):
        return NameUse(
            (value.id, *(attr for attr, _ in attributes)), (source.start(value), *places)
        )
    return NameUse((node.attr,), (places[-1],))


def resolved_uses(code: str) -> set[str]:
    """Return the full dotted paths that Python code uses, through its imports.

    After ``from a.b import X as Y``, ``Y()`` uses ``a.b.X``. After
    ``import a.b as m``, ``m.X`` uses ``a.b.X``. The imports are uses too.
    A name that no import binds, such as a builtin, stays as it is written,
    and ``from a import *`` binds nothing. The code must be valid Python.
    """
    return _uses(code)[0]


def _uses(code: str) -> tuple[set[str], set[str]]:
    """The full paths that code uses, and the names of the members it reaches through objects.

    A member is an attribute of something that no import binds, such as a
    variable or the result of a call: ``run`` in ``agent.run()`` or ``output``
    in ``f().output``.
    """
    tree = ast.parse(code)
    bindings, paths = _imports(tree)
    source = _Source(code)
    members: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            parts: tuple[str, ...] = (node.id,)
        elif isinstance(node, ast.Attribute):
            parts = _attribute_use(node, source).parts
            if not _reached_through_import(node, bindings):
                members.add(node.attr)
        else:
            continue
        first, *rest = parts
        paths.add(".".join([bindings.get(first, first), *rest]))
    return paths, members


def _reached_through_import(node: ast.Attribute, bindings: Mapping[str, str]) -> bool:
    """Whether an attribute chain starts with a name that an import binds, as ``m.X.f`` does."""
    root = node.value
    while isinstance(root, ast.Attribute):
        root = root.value
    return isinstance(root, ast.Name) and root.id in bindings


def _imports(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """The full path that each name an import binds stands for, and the imported paths."""
    bindings: dict[str, str] = {}
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                paths.add(alias.name)
                # import a.b binds a; import a.b as m binds m to a.b.
                top = alias.name.partition(".")[0]
                bindings[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                if alias.name != "*":
                    path = f"{node.module}.{alias.name}"
                    paths.add(path)
                    bindings[alias.asname or alias.name] = path
    return bindings, paths


def _uses_path(code: str, path: str) -> bool:
    """Whether code uses ``path``, or something in it, such as a method of a class.

    A method or an attribute that code reaches through an object, such as
    ``agent.run()``, counts by its last name, as long as the code uses something
    from the top-level package of ``path``. The verifier checks the full path.
    """
    paths, members = _uses(code)
    top, name = path.partition(".")[0], path.rpartition(".")[2]
    return _within(paths, path) or (name in members and _within(paths, top))


def _within(paths: Iterable[str], prefix: str) -> bool:
    return any(used == prefix or used.startswith(f"{prefix}.") for used in paths)


# This fix is temporary. Once the engine can fix code (phase 4 in docs/architecture.md),
# the validator uses the engine's fix instead, so that there is only one implementation.
def apply_rename(code: str, old: str, new: str) -> str:
    """Rename ``old``, a name without dots, wherever :func:`uses` sees it, and nowhere else."""
    places = {
        place
        for use in name_uses(code)
        for part, place in zip(use.parts, use.places, strict=True)
        if part == old and place is not None
    }
    return _replace(code, places, old, new)


def apply_keyword_rename(code: str, callables: Collection[str], old: str, new: str) -> str:
    """Rename the keyword argument ``old`` wherever :func:`passed_keywords` sees it."""
    places = {place for name, place in keyword_uses(code, callables) if name == old}
    return _replace(code, places, old, new)


def _replace(code: str, places: Iterable[Place], old: str, new: str) -> str:
    lines = code.split("\n")
    # From the end, so that each replacement leaves the places before it where they are.
    for line, column in sorted(places, reverse=True):
        text = lines[line - 1]
        lines[line - 1] = text[:column] + new + text[column + len(old) :]
    return "\n".join(lines)


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
        *_check_keyword_names(notice),
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


def _check_keyword_names(notice: Notice) -> Iterator[Problem]:
    """A renamed keyword argument can't have a name that Python reserves, such as ``class``."""
    rename = notice.data.get("rename", {})
    if "keyword_of" not in rename:
        return
    for field in ("from", "to"):
        if keyword.iskeyword(rename[field]):
            message = f"rename.{field}: {rename[field]} is a Python keyword, so no argument has it."
            yield notice.problem(f"rename.{field}", message)


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
    rename = notice.data["rename"]
    old, new = rename["from"], rename["to"]
    if old == new:
        yield notice.problem("rename", "rename.from and rename.to are the same.")
        return
    if "keyword_of" in rename:
        yield from _check_keyword_rename(notice, old, new, rename["keyword_of"])
    else:
        yield from _check_name_rename(notice, old, new)


def _check_name_rename(notice: Notice, old: str, new: str) -> Iterator[Problem]:
    """The examples use the paths of the rename, as written, directly or through an import."""
    before, after = notice.data["example"]["before"], notice.data["example"]["after"]
    through = "by its full path or through an import of it"
    problems = []
    if not _uses_path(before, old):
        message = f"example.before must use rename.from, {old}, {through}."
        problems.append(notice.problem("example.before", message))
    if not _uses_path(after, new):
        message = f"example.after must use rename.to, {new}, {through}."
        problems.append(notice.problem("example.after", message))
    if _uses_path(after, old):
        message = f"example.after must not use rename.from, {old}."
        problems.append(notice.problem("example.after", message))
    yield from problems
    # The validator applies a rename within one module. A move to another waits for the engine.
    if not problems and old.rpartition(".")[0] == new.rpartition(".")[0]:
        old_name, new_name = distinguishing_names(old, new)
        yield from _check_fix(notice, apply_rename(before, old_name, new_name))


def _check_keyword_rename(
    notice: Notice, old: str, new: str, callables: Sequence[str]
) -> Iterator[Problem]:
    """The examples pass the old keyword before the change, and only the new one after it."""
    names = {callable_.rpartition(".")[2] for callable_ in callables}
    listed = " or ".join(callables)
    before = notice.data["example"]["before"]
    passed_before = passed_keywords(before, names)
    passed_after = passed_keywords(notice.data["example"]["after"], names)
    problems = []
    if old not in passed_before:
        problems.append(
            notice.problem("example.before", f"example.before must pass {old}= to {listed}.")
        )
    if new not in passed_after:
        problems.append(
            notice.problem("example.after", f"example.after must pass {new}= to {listed}.")
        )
    if old in passed_after:
        problems.append(
            notice.problem("example.after", f"example.after must not pass {old}= to {listed}.")
        )
    yield from problems
    if not problems:
        yield from _check_fix(notice, apply_keyword_rename(before, names, old, new))


def _check_fix(notice: Notice, fixed: str) -> Iterator[Problem]:
    """``example.after`` must be ``fixed``, which is ``example.before`` with the rename applied."""
    after = notice.data["example"]["after"]
    if fixed == after:
        return
    start = "example.after must be example.before with the rename applied, and nothing else"
    pairs = itertools.zip_longest(fixed.splitlines(), after.splitlines())
    for number, (expected, actual) in enumerate(pairs, start=1):
        if expected != actual:
            detail = "is extra" if expected is None else f"should be {expected!r}"
            yield Problem(
                notice.rel,
                _code_line(notice.tree, "after", number),
                f"{start}: line {number} {detail}.",
            )
            return
    yield notice.problem("example.after", f"{start}: only the newline at the end differs.")


def keyword_uses(code: str, callables: Collection[str]) -> list[tuple[str, Place]]:
    """Return the keyword arguments that code passes to the named callables, and their places.

    A call matches by the last part of its name, so ``Agent(...)``,
    ``pydantic_ai.Agent(...)`` and ``agent.run(...)`` match ``Agent`` and ``run``.
    Keyword arguments passed through ``**`` are not counted.
    """
    source = _Source(code)
    return [
        (argument.arg, source.start(argument))
        for node in ast.walk(ast.parse(code))
        if isinstance(node, ast.Call) and _called_name(node.func) in callables
        for argument in node.keywords
        if argument.arg
    ]


def passed_keywords(code: str, callables: Collection[str]) -> set[str]:
    """Return the names of the keyword arguments that :func:`keyword_uses` finds."""
    return {name for name, _ in keyword_uses(code, callables)}


def _called_name(function: ast.expr) -> str | None:
    """``f`` for ``f()``, ``run`` for ``agent.run()``, and None for a call such as ``f()()``."""
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


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
