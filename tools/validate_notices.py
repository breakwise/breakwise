#!/usr/bin/env python3
"""Check that every notice in the registry is valid.

Run it from anywhere:

    python tools/validate_notices.py

For every YAML file under ``notices/``, this checks that:

- it is one valid YAML document without duplicate keys,
- it matches the JSON Schema in ``schema/``,
- it is stored at ``notices/<ecosystem>/<package>/<id>.yaml`` and its ID is unique,
- the year in its ID is the year it was published,
- its dates are real dates, ``published`` is not in the future,
  and ``modified`` is not before ``published``,
- ``works_since`` is not later than ``breaks_in``,
- its example code is valid Python, and a rename's examples use the old name
  before the change and only the new name after it.

It also checks that every notice shown in ``README.md`` matches its file exactly.

Each problem is printed with its file and line. In GitHub Actions it is also
reported as an annotation on the pull request. The exit code is 1 if there is
any problem, otherwise 0.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
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

README_YAML_BLOCK = re.compile(r"^```yaml\n(.*?)^```$", re.MULTILINE | re.DOTALL)
NOTICE_ID_LINE = re.compile(r"^id: *(\S+) *$", re.MULTILINE)


@dataclass(frozen=True)
class Problem:
    """A problem found at a line of a file."""

    file: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.file.as_posix()}:{self.line}: {self.message}"

    def github_annotation(self) -> str:
        return github_error(self.file, self.message, self.line)


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


def line_of(node: yaml.Node, path: Sequence[str | int]) -> int:
    """Return the 1-based line of the value at ``path``, or of its closest parent."""
    line = node.start_mark.line + 1
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


def duplicate_keys(node: yaml.Node) -> Iterator[yaml.Node]:
    """Yield every mapping key that appears more than once in its mapping."""
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            if key.value in seen:
                yield key
            seen.add(key.value)
            yield from duplicate_keys(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from duplicate_keys(item)


def describe(error: ValidationError) -> str:
    """Turn a schema error into a message that says what is expected."""
    where = "/".join(str(part) for part in error.absolute_path)
    description = error.schema.get("description") if isinstance(error.schema, dict) else None
    if error.validator == "pattern" and description:
        message = f"{error.instance!r} is not valid. Expected {description}"
    elif error.validator == "not" and not error.absolute_path:
        message = "`rename` is only allowed when `type` is `rename`."
    else:
        message = error.message
    return f"{where}: {message}" if where else message


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


def mentions(code: str, name: str) -> bool:
    """Whether ``code`` uses ``name`` as a whole name, not as part of a longer one."""
    return re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", code) is not None


def check_notice(
    root: Path,
    path: Path,
    validator: Draft202012Validator,
    seen_ids: dict[str, Path],
    today: dt.date,
) -> list[Problem]:
    """Check one notice file. Add its ID to ``seen_ids`` if it matches the schema."""
    rel = path.relative_to(root)
    text = path.read_text(encoding="utf-8")
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        reason = getattr(error, "problem", None) or str(error)
        return [Problem(rel, mark.line + 1 if mark else 1, f"Invalid YAML: {reason}")]
    if len(documents) != 1:
        return [Problem(rel, 1, "A notice file must contain exactly one YAML document.")]

    tree = yaml.compose(text, Loader=yaml.SafeLoader)
    problems = [
        Problem(rel, key.start_mark.line + 1, f"Duplicate key: {key.value}")
        for key in duplicate_keys(tree)
    ]
    notice: Any = documents[0]
    errors = sorted(
        validator.iter_errors(notice), key=lambda e: [str(part) for part in e.absolute_path]
    )
    problems += [Problem(rel, line_of(tree, list(e.absolute_path)), describe(e)) for e in errors]
    if problems:
        return problems

    def at(*path: str) -> int:
        return line_of(tree, path)

    notice_id: str = notice["id"]
    package = notice["package"]
    expected = NOTICES_DIR / package["ecosystem"].lower() / package["name"] / f"{notice_id}.yaml"
    if rel.as_posix() != expected.as_posix():
        problems.append(
            Problem(
                rel,
                at("id"),
                f"Must be stored at {expected.as_posix()}, from its id, "
                "package.ecosystem and package.name.",
            )
        )
    if notice_id in seen_ids:
        used_by = seen_ids[notice_id].as_posix()
        problems.append(Problem(rel, at("id"), f"ID {notice_id} is already used by {used_by}."))
    else:
        seen_ids[notice_id] = rel

    dates: dict[str, dt.date] = {}
    for field in ("published", "modified"):
        if field in notice:
            try:
                dates[field] = dt.date.fromisoformat(notice[field])
            except ValueError:
                message = f"{field}: {notice[field]!r} is not a real date."
                problems.append(Problem(rel, at(field), message))
    published = dates.get("published")
    if published is not None:
        if published > today + dt.timedelta(days=1):
            message = f"published: {published} is in the future."
            problems.append(Problem(rel, at("published"), message))
        if int(notice_id.split("-")[1]) != published.year:
            message = (
                f"The year in {notice_id} must be the year it was published ({published.year})."
            )
            problems.append(Problem(rel, at("id"), message))
        modified = dates.get("modified")
        if modified is not None and modified < published:
            message = "modified must not be before published."
            problems.append(Problem(rel, at("modified"), message))

    if "works_since" in notice and Version(notice["works_since"]) > Version(notice["breaks_in"]):
        message = (
            f"works_since ({notice['works_since']}) must not be later "
            f"than breaks_in ({notice['breaks_in']})."
        )
        problems.append(Problem(rel, at("works_since"), message))

    for which in ("before", "after"):
        try:
            compile(notice["example"][which], f"example.{which}", "exec")
        except SyntaxError as error:
            line = at("example", which) + (error.lineno or 1)
            message = f"example.{which} is not valid Python: {error.msg}"
            problems.append(Problem(rel, line, message))

    if notice["type"] == "rename":
        problems += _check_rename_examples(rel, notice, at)
    return problems


def _check_rename_examples(rel: Path, notice: Any, at: Any) -> list[Problem]:
    old, new = notice["rename"]["from"], notice["rename"]["to"]
    if old == new:
        return [Problem(rel, at("rename"), "rename.from and rename.to are the same.")]
    old_name, new_name = distinguishing_names(old, new)
    before, after = notice["example"]["before"], notice["example"]["after"]
    problems = []
    if not mentions(before, old_name):
        message = f"example.before must use the old name {old_name}."
        problems.append(Problem(rel, at("example", "before"), message))
    if not mentions(after, new_name):
        message = f"example.after must use the new name {new_name}."
        problems.append(Problem(rel, at("example", "after"), message))
    renamed_object = old.rpartition(".")[2] != new.rpartition(".")[2]
    if renamed_object and mentions(after, old_name):
        message = f"example.after must not use the old name {old_name}."
        problems.append(Problem(rel, at("example", "after"), message))
    return problems


def check_readme(root: Path, notices: dict[str, Path]) -> list[Problem]:
    """Check that every notice shown in README.md matches its file exactly."""
    readme = root / README_FILE
    if not readme.is_file():
        return []
    text = readme.read_text(encoding="utf-8")
    problems = []
    for match in README_YAML_BLOCK.finditer(text):
        block = match.group(1)
        id_match = NOTICE_ID_LINE.search(block)
        if id_match is None:
            continue
        line = text.count("\n", 0, match.start()) + 1
        notice_id = id_match.group(1)
        path = notices.get(notice_id)
        if path is None:
            message = f"The example shows {notice_id}, but there is no valid notice with that ID."
            problems.append(Problem(README_FILE, line, message))
        elif block != (root / path).read_text(encoding="utf-8"):
            message = f"The example must match {path.as_posix()} exactly."
            problems.append(Problem(README_FILE, line, message))
    return problems


def validate(root: Path, today: dt.date | None = None) -> tuple[list[Problem], int]:
    """Check the whole registry under ``root``. Return the problems and the number of notices."""
    if today is None:
        today = dt.datetime.now(dt.UTC).date()
    schema = json.loads((root / SCHEMA_FILE).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    problems: list[Problem] = []
    seen_ids: dict[str, Path] = {}
    count = 0
    for path in sorted(p for p in (root / NOTICES_DIR).rglob("*") if p.is_file()):
        if path.suffix == ".yml":
            message = "Use the .yaml extension for notices."
            problems.append(Problem(path.relative_to(root), 1, message))
        elif path.suffix == ".yaml":
            count += 1
            problems += check_notice(root, path, validator, seen_ids, today)
    problems += check_readme(root, seen_ids)
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
