"""The ``breakwise`` command."""

from __future__ import annotations

import argparse
import errno
import os
import sys
import textwrap
from collections.abc import Sequence
from typing import TextIO

from packaging.version import Version

from breakwise import __version__
from breakwise.notices import Notice, NoticeError, find

ATTRIBUTION = "Data from the Breakwise registry, CC BY 4.0."
# The exit code when the reader closes the output before it ends, as in `| head -1`.
# Shells report the same code, 128 + SIGPIPE, for a Unix tool stopped this way.
CLOSED_OUTPUT = 141


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        _write_any_text(stream)
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
    try:
        print(render(notice))
        sys.stdout.flush()
    except OSError as error:
        if not _is_closed_output(error):
            raise
        _discard_stdout()
        return CLOSED_OUTPUT
    return 0


def _write_any_text(stream: TextIO) -> None:
    """Make a standard stream write any notice without an error.

    Files and pipes get UTF-8, whatever the platform or PYTHONIOENCODING say. On
    Windows they would otherwise get the ANSI code page, which lacks most characters.
    A terminal keeps its encoding and shows "?" for a character it lacks.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    if stream.isatty():
        reconfigure(errors="replace")
    else:
        reconfigure(encoding="utf-8", errors="replace")


def _is_closed_output(error: OSError) -> bool:
    # Windows reports a closed pipe as EINVAL when the output is flushed.
    return isinstance(error, BrokenPipeError) or (
        error.errno == errno.EINVAL and sys.platform == "win32"
    )


def _discard_stdout() -> None:
    """Point stdout at devnull, so that the flush when Python exits can't fail again.

    This is what the Python docs recommend for a closed pipe.
    """
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, sys.stdout.fileno())
    os.close(devnull)


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
        rename = data["rename"]
        names = f"{rename['from']} -> {rename['to']}"
        if "keyword_of" in rename:
            names = f"the keyword argument {names} of {', '.join(rename['keyword_of'])}"
        change += f" {names}"
    since = data.get("works_since")
    if not since:
        works_since = "not known, so the fix waits for the upgrade"
    elif Version(since) < Version(data["breaks_in"]):
        works_since = f"{since}, so the fix can come before the upgrade"
    else:
        # The validator allows works_since to equal breaks_in, but not to come after it.
        works_since = (
            f"{since}, the release that breaks the old code, so the fix comes with the upgrade"
        )

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
