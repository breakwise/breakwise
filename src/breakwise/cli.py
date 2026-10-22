"""The ``breakwise`` command."""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections.abc import Sequence

from breakwise import __version__
from breakwise.notices import Notice, NoticeError, find

ATTRIBUTION = "Data from the Breakwise registry, CC BY 4.0."


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="breakwise", description="Breaking changes in Python libraries, with tested fixes."
    )
    parser.add_argument("--version", action="version", version=f"breakwise {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    explain = commands.add_parser(
        "explain", help="show a notice", description="Show a notice. This works offline."
    )
    explain.add_argument("notice_id", metavar="ID", help="a notice ID, such as BW-2026-0001")
    args = parser.parse_args(argv)

    try:
        notice = find(args.notice_id)
    except NoticeError as error:
        print(f"breakwise: {error}", file=sys.stderr)
        return 1
    print(render(notice))
    return 0


def render(notice: Notice) -> str:
    """The text that ``breakwise explain`` prints for a notice."""
    data = notice.data
    package = data["package"]
    status = data["status"]
    if status == "draft":
        status = "draft, not yet checked by a maintainer"
    where = f"{package['name']} on {package['ecosystem']}"
    if package.get("aliases"):
        where += f" (also {', '.join(package['aliases'])})"
    change = data["type"]
    if change == "rename":
        change += f" {data['rename']['from']} -> {data['rename']['to']}"
    if data.get("works_since"):
        works_since = f"{data['works_since']}, so the fix can come before the upgrade"
    else:
        works_since = "not known, so the fix waits for the upgrade"

    lines = [
        f"{data['id']} ({status})",
        data["summary"],
        "",
        f"Package:     {where}",
        f"Change:      {change}",
        f"Breaks in:   {data['breaks_in']}",
        f"Works since: {works_since}",
    ]
    if "instructions" in data:
        lines += ["", "Instructions:", _indent(data["instructions"])]
    lines += [
        "",
        "Before:",
        _indent(data["example"]["before"]),
        "After:",
        _indent(data["example"]["after"]),
        "",
        "References:",
        *(f"  {reference}" for reference in data["references"]),
        "",
        f"Notice: {notice.url}",
        ATTRIBUTION,
    ]
    return "\n".join(lines)


def _indent(text: str) -> str:
    return textwrap.indent(text.rstrip("\n"), "    ")
