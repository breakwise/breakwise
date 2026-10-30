"""Tests for the breakwise command and the notices that come with it."""

from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import validate_notices
from breakwise import __version__, cli, notices

REPO_ROOT = Path(__file__).resolve().parent.parent

RENAME = """\
---
schema_version: "0.3"
id: BW-2026-0001
status: verified
published: "2026-10-21"
modified: "2026-10-22"
package:
  ecosystem: PyPI
  name: pydantic-ai
  aliases:
    - pydantic-ai-slim
type: rename
breaks_in: "2.0.0b1"
works_since: "0.7.6"
summary: OpenAIModel was renamed to OpenAIChatModel, and the old name was removed in 2.0.0b1.
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
"""

REMOVAL = """\
---
schema_version: "0.3"
id: BW-2026-0002
status: draft
published: "2026-10-22"
package:
  ecosystem: PyPI
  name: demo
type: removal
breaks_in: "2.0.0"
summary: demo.legacy() was removed in 2.0.0.
instructions: Call demo.modern() instead, and pass the timeout in seconds.
example:
  before: |
    import demo

    demo.legacy()
  after: |
    import demo

    demo.modern(timeout=1.0)
references:
  - https://example.com/changelog
"""

KEYWORD_RENAME = """\
---
schema_version: "0.3"
id: BW-2026-0003
status: draft
published: "2026-10-23"
package:
  ecosystem: PyPI
  name: pydantic-ai
type: rename
breaks_in: "2.0.0b1"
works_since: "0.4.4"
summary: The mcp_servers argument of Agent was renamed to toolsets, and the old name was removed.
rename:
  keyword_of:
    - pydantic_ai.Agent
  from: mcp_servers
  to: toolsets
example:
  before: |
    from pydantic_ai import Agent

    agent = Agent("test", mcp_servers=[])
  after: |
    from pydantic_ai import Agent

    agent = Agent("test", toolsets=[])
references:
  - https://pydantic.dev/docs/ai/changelog/
"""

NOTICES_URL = "https://github.com/breakwise/breakwise/blob/main/notices"

RENAME_OUTPUT = [
    "BW-2026-0001 (verified)",
    "OpenAIModel was renamed to OpenAIChatModel, and the old name was removed in 2.0.0b1.",
    "",
    "Package:     pydantic-ai on PyPI (also pydantic-ai-slim)",
    (
        "Change:      rename pydantic_ai.models.openai.OpenAIModel"
        " -> pydantic_ai.models.openai.OpenAIChatModel"
    ),
    "Breaks in:   2.0.0b1",
    "Works since: 0.7.6, so the fix can come before the upgrade",
    "",
    "Before:",
    "    from pydantic_ai.models.openai import OpenAIModel",
    "",
    '    model = OpenAIModel("gpt-5")',
    "After:",
    "    from pydantic_ai.models.openai import OpenAIChatModel",
    "",
    '    model = OpenAIChatModel("gpt-5")',
    "",
    "References:",
    "  https://pydantic.dev/docs/ai/changelog/",
    "",
    f"Notice: {NOTICES_URL}/pypi/pydantic-ai/BW-2026-0001.yaml",
    "Data from the Breakwise registry, CC BY 4.0.",
]

REMOVAL_OUTPUT = [
    "BW-2026-0002 (draft, not yet checked by a maintainer)",
    "demo.legacy() was removed in 2.0.0.",
    "",
    "Package:     demo on PyPI",
    "Change:      removal",
    "Breaks in:   2.0.0",
    "Works since: not known, so the fix waits for the upgrade",
    "",
    "Instructions:",
    "    Call demo.modern() instead, and pass the timeout in seconds.",
    "",
    "Before:",
    "    import demo",
    "",
    "    demo.legacy()",
    "After:",
    "    import demo",
    "",
    "    demo.modern(timeout=1.0)",
    "",
    "References:",
    "  https://example.com/changelog",
    "",
    f"Notice: {NOTICES_URL}/pypi/demo/BW-2026-0002.yaml",
    "Data from the Breakwise registry, CC BY 4.0.",
]

KEYWORD_RENAME_OUTPUT = [
    "BW-2026-0003 (draft, not yet checked by a maintainer)",
    "The mcp_servers argument of Agent was renamed to toolsets, and the old name was removed.",
    "",
    "Package:     pydantic-ai on PyPI",
    "Change:      rename the keyword argument mcp_servers -> toolsets of pydantic_ai.Agent",
    "Breaks in:   2.0.0b1",
    "Works since: 0.4.4, so the fix can come before the upgrade",
    "",
    "Before:",
    "    from pydantic_ai import Agent",
    "",
    '    agent = Agent("test", mcp_servers=[])',
    "After:",
    "    from pydantic_ai import Agent",
    "",
    '    agent = Agent("test", toolsets=[])',
    "",
    "References:",
    "  https://pydantic.dev/docs/ai/changelog/",
    "",
    f"Notice: {NOTICES_URL}/pypi/pydantic-ai/BW-2026-0003.yaml",
    "Data from the Breakwise registry, CC BY 4.0.",
]


# "→" is in neither cp1252, the code page Windows gives files and pipes, nor ASCII.
NON_ASCII = REMOVAL.replace("id: BW-2026-0002", "id: BW-2026-0004").replace(
    "summary: demo.legacy() was removed in 2.0.0.",
    "summary: demo.legacy() was removed in 2.0.0 → call demo.modern().",
)


@pytest.fixture
def bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Notices laid out as in the package, in place of the ones that come with it."""
    root = tmp_path / "notices"
    for rel, text in (
        ("pypi/pydantic-ai/BW-2026-0001.yaml", RENAME),
        ("pypi/demo/BW-2026-0002.yaml", REMOVAL),
        ("pypi/pydantic-ai/BW-2026-0003.yaml", KEYWORD_RENAME),
        ("pypi/demo/BW-2026-0004.yaml", NON_ASCII),
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    (root / "LICENSE.md").write_text("# License for notices\n", encoding="utf-8")
    monkeypatch.setattr(notices, "DATA", root)
    return root


def explain(capsys: pytest.CaptureFixture[str], notice_id: str) -> tuple[int, str, str]:
    code = cli.main(["explain", notice_id])
    out, err = capsys.readouterr()
    return code, out, err


@pytest.mark.usefixtures("bundled")
@pytest.mark.parametrize(
    ("notice_id", "output"),
    [
        ("BW-2026-0001", RENAME_OUTPUT),
        ("BW-2026-0002", REMOVAL_OUTPUT),
        ("BW-2026-0003", KEYWORD_RENAME_OUTPUT),
    ],
)
def test_explain_shows_a_notice(
    capsys: pytest.CaptureFixture[str], notice_id: str, output: list[str]
) -> None:
    assert explain(capsys, notice_id) == (0, "\n".join(output) + "\n", "")


def test_explain_names_every_callable_of_a_renamed_keyword(
    bundled: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notice = bundled / "pypi/pydantic-ai/BW-2026-0003.yaml"
    text = notice.read_text(encoding="utf-8").replace(
        "    - pydantic_ai.Agent\n", "    - pydantic_ai.Agent\n    - pydantic_ai.Agent.run\n"
    )
    notice.write_text(text, encoding="utf-8")
    _, out, _ = explain(capsys, "BW-2026-0003")
    assert (
        "Change:      rename the keyword argument mcp_servers -> toolsets"
        " of pydantic_ai.Agent, pydantic_ai.Agent.run\n"
    ) in out


WITHDRAWN = 'withdrawn:\n  date: "2026-10-25"\n  reason: It says the same as BW-2026-0001.\n'
WITHDRAWN_LINE = "Withdrawn on 2026-10-25: It says the same as BW-2026-0001."
RESTORED_IN = 'breaks_in: "2.0.0"\nrestored_in: "2.1.0"'
RESTORED_LINE = "Restored in: 2.1.0, where the old code works again"
AFFECTS = (
    "affects:\n"
    "  - demo.legacy\n"
    "  - path: demo.connect\n    keyword: timeout\n"
    "  - path: demo.connect\n    keyword: verify\n    when: passed\n"
    "  - path: demo.connect\n    keyword: retries\n    when: omitted\n"
)
AFFECTS_LINES = [
    "",
    "Affects:",
    "  demo.legacy",
    "  demo.connect, when timeout= is passed",
    "  demo.connect, when verify= is passed",
    "  demo.connect, when retries= is left out",
]


def edit_removal(bundled: Path, *changes: tuple[str, str]) -> None:
    notice = bundled / "pypi/demo/BW-2026-0002.yaml"
    text = notice.read_text(encoding="utf-8")
    for old, new in changes:
        assert old in text
        text = text.replace(old, new, 1)
    notice.write_text(text, encoding="utf-8")


def with_restored_in(output: list[str]) -> list[str]:
    at = output.index("Breaks in:   2.0.0") + 1
    return [*output[:at], RESTORED_LINE, *output[at:]]


def with_affects(output: list[str]) -> list[str]:
    # The section goes before the empty line that comes before Instructions.
    at = output.index("Instructions:") - 1
    return [*output[:at], *AFFECTS_LINES, *output[at:]]


def test_explain_says_when_a_notice_was_withdrawn(
    bundled: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edit_removal(bundled, ("references:", WITHDRAWN + "references:"))
    output = [WITHDRAWN_LINE, *REMOVAL_OUTPUT]
    assert explain(capsys, "BW-2026-0002") == (0, "\n".join(output) + "\n", "")


def test_explain_says_where_the_old_code_works_again(
    bundled: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edit_removal(bundled, ('breaks_in: "2.0.0"', RESTORED_IN))
    output = with_restored_in(REMOVAL_OUTPUT)
    assert explain(capsys, "BW-2026-0002") == (0, "\n".join(output) + "\n", "")


def test_explain_lists_what_the_change_affects(
    bundled: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edit_removal(bundled, ("example:", AFFECTS + "example:"))
    output = with_affects(REMOVAL_OUTPUT)
    assert explain(capsys, "BW-2026-0002") == (0, "\n".join(output) + "\n", "")


def test_explain_shows_withdrawn_restored_in_and_affects_together(
    bundled: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edit_removal(
        bundled,
        ("references:", WITHDRAWN + "references:"),
        ('breaks_in: "2.0.0"', RESTORED_IN),
        ("example:", AFFECTS + "example:"),
    )
    output = [WITHDRAWN_LINE, *with_affects(with_restored_in(REMOVAL_OUTPUT))]
    assert explain(capsys, "BW-2026-0002") == (0, "\n".join(output) + "\n", "")


@pytest.mark.usefixtures("bundled")
def test_ids_may_be_written_in_lowercase(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = explain(capsys, "bw-2026-0002")
    assert (code, out.splitlines()[0]) == (0, REMOVAL_OUTPUT[0])


@pytest.mark.usefixtures("bundled")
@pytest.mark.parametrize(
    ("notice_id", "message"),
    [
        (
            "BW-2026-9999",
            (
                "There is no notice BW-2026-9999 in the notices that come with breakwise "
                f"{__version__}. A newer release may have it."
            ),
        ),
        (
            "BW-2026-10000",
            (
                "There is no notice BW-2026-10000 in the notices that come with breakwise "
                f"{__version__}. A newer release may have it."
            ),
        ),
        ("../BW-2026-0001", "'../BW-2026-0001' is not a notice ID. IDs look like BW-2026-0001."),
        # Format 0.2: no extra leading zeros, and no number 0000.
        ("BW-2026-00001", "'BW-2026-00001' is not a notice ID. IDs look like BW-2026-0001."),
        ("BW-2026-0000", "'BW-2026-0000' is not a notice ID. IDs look like BW-2026-0001."),
        ("BW-2026-0001\n", "'BW-2026-0001\\n' is not a notice ID. IDs look like BW-2026-0001."),
    ],
)
def test_explain_says_why_it_shows_nothing(
    capsys: pytest.CaptureFixture[str], notice_id: str, message: str
) -> None:
    assert explain(capsys, notice_id) == (1, "", f"breakwise: {message}\n")


@pytest.mark.usefixtures("bundled")
@pytest.mark.parametrize("digits", [9, 300])
def test_an_id_with_more_than_eight_digits_is_not_a_notice_id(
    capsys: pytest.CaptureFixture[str], digits: int
) -> None:
    # Format 0.3 allows eight digits at most, so an ID never makes a file name too long.
    notice_id = "BW-2026-" + "1" * digits
    message = f"breakwise: {notice_id!r} is not a notice ID. IDs look like BW-2026-0001.\n"
    assert explain(capsys, notice_id) == (1, "", message)


@pytest.mark.usefixtures("bundled")
def test_a_file_system_error_means_the_notice_is_not_there(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Whether a name too long raises OSError depends on the Python and the platform.
    def too_long(_path: Path) -> bool:
        raise OSError(errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(Path, "is_file", too_long)
    code, _, err = explain(capsys, "BW-2026-0001")
    assert (code, err.startswith("breakwise: There is no notice BW-2026-0001 ")) == (1, True)


@pytest.mark.parametrize(
    ("breaks_in", "works_since", "line"),
    [
        ("2.0.0b1", "0.7.6", "0.7.6, so the fix can come before the upgrade"),
        # Versions compare as PEP 440 says, not as text: 0.9.0 comes before 0.10.0.
        ("0.10.0", "0.9.0", "0.9.0, so the fix can come before the upgrade"),
        (
            "2.0.0b1",
            "2.0.0b1",
            "2.0.0b1, the release that breaks the old code, so the fix comes with the upgrade",
        ),
        # 2.0 and 2.0.0 are the same release.
        (
            "2.0.0",
            "2.0",
            "2.0, the release that breaks the old code, so the fix comes with the upgrade",
        ),
        ("2.0.0b1", None, "not known, so the fix waits for the upgrade"),
    ],
)
def test_works_since_says_when_the_fix_can_come(
    bundled: Path,
    capsys: pytest.CaptureFixture[str],
    breaks_in: str,
    works_since: str | None,
    line: str,
) -> None:
    text = RENAME.replace('breaks_in: "2.0.0b1"', f'breaks_in: "{breaks_in}"')
    since = "" if works_since is None else f'works_since: "{works_since}"\n'
    text = text.replace('works_since: "0.7.6"\n', since)
    (bundled / "pypi/pydantic-ai/BW-2026-0001.yaml").write_text(text, encoding="utf-8")
    _, out, _ = explain(capsys, "BW-2026-0001")
    assert f"\nWorks since: {line}\n" in out


class Terminal(io.TextIOWrapper):
    """Standard output on a terminal."""

    def isatty(self) -> bool:
        return True


class ClosedPipe(io.TextIOWrapper):
    """Standard output whose reader has gone, so that every write fails with ``error``."""

    def __init__(self, fd: int, error: OSError) -> None:
        super().__init__(io.BytesIO(), encoding="utf-8")
        self.fd = fd
        self.error = error

    def fileno(self) -> int:
        return self.fd

    def write(self, _text: str) -> int:
        raise self.error


@pytest.mark.usefixtures("bundled")
def test_files_and_pipes_get_utf8_whatever_the_platform_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows gives a file or a pipe its ANSI code page, such as cp1252.
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    assert cli.main(["explain", "BW-2026-0004"]) == 0
    assert "removed in 2.0.0 → call demo.modern()." in raw.getvalue().decode("utf-8")


def test_error_messages_get_utf8_too(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="cp1252"))
    assert cli.main(["explain", "BW-2026-→"]) == 1
    sys.stderr.flush()
    assert raw.getvalue().decode("utf-8").splitlines() == [
        "breakwise: 'BW-2026-→' is not a notice ID. IDs look like BW-2026-0001."
    ]


@pytest.mark.usefixtures("bundled")
def test_a_terminal_shows_a_question_mark_for_a_character_it_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", Terminal(raw, encoding="ascii"))
    assert cli.main(["explain", "BW-2026-0004"]) == 0
    assert "removed in 2.0.0 ? call demo.modern()." in raw.getvalue().decode("ascii")


@pytest.mark.usefixtures("bundled")
def test_streams_that_cannot_be_reconfigured_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["explain", "BW-2026-0004"]) == 0
    assert "removed in 2.0.0 → call demo.modern()." in out.getvalue()


@pytest.mark.usefixtures("bundled")
@pytest.mark.parametrize(
    ("platform", "error"),
    [
        ("linux", BrokenPipeError(errno.EPIPE, "Broken pipe")),
        ("win32", BrokenPipeError(errno.EPIPE, "Broken pipe")),
        # Windows reports a closed pipe as EINVAL when the output is flushed.
        ("win32", OSError(errno.EINVAL, "Invalid argument")),
    ],
)
def test_a_closed_output_ends_quietly_with_141(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    platform: str,
    error: OSError,
) -> None:
    fd = os.open(tmp_path / "stdout", os.O_WRONLY | os.O_CREAT)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "stdout", ClosedPipe(fd, error))
    try:
        assert cli.main(["explain", "BW-2026-0001"]) == cli.CLOSED_OUTPUT == 141
        # The rest goes to devnull, so the flush when Python exits can't fail again.
        os.write(fd, b"flushed at exit")
    finally:
        os.close(fd)
    assert (tmp_path / "stdout").read_bytes() == b""
    assert capsys.readouterr().err == ""


@pytest.mark.usefixtures("bundled")
@pytest.mark.parametrize(
    ("platform", "error"),
    [
        # EINVAL means a closed pipe only on Windows.
        ("linux", OSError(errno.EINVAL, "Invalid argument")),
        ("win32", OSError(errno.EIO, "Input/output error")),
    ],
)
def test_other_output_errors_are_not_hidden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, error: OSError
) -> None:
    fd = os.open(tmp_path / "stdout", os.O_WRONLY | os.O_CREAT)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "stdout", ClosedPipe(fd, error))
    try:
        with pytest.raises(OSError, match=error.strerror):
            cli.main(["explain", "BW-2026-0001"])
    finally:
        os.close(fd)


def test_a_reader_that_leaves_early_gets_141_and_no_traceback(bundled: Path) -> None:
    # A real process whose pipe is closed before anything is read, as with `| head -c 0`.
    # Python ignores SIGPIPE, so this raises BrokenPipeError on Linux and macOS, and
    # OSError with EINVAL on Windows. -I ignores PYTHONIOENCODING and the rest of the
    # environment.
    code = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "from breakwise import cli, notices; notices.DATA = Path(sys.argv[2]); "
        "sys.exit(cli.main(['explain', 'BW-2026-0004']))"
    )
    read, write = os.pipe()
    os.close(read)
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", code, str(REPO_ROOT / "src"), str(bundled)],
            stdout=write,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    finally:
        os.close(write)
    assert (completed.returncode, completed.stderr) == (141, b"")


def test_notice_ids_follow_the_schema() -> None:
    # The package checks IDs without the schema, so the two patterns must not drift apart.
    schema = json.loads((REPO_ROOT / validate_notices.SCHEMA_FILE).read_text(encoding="utf-8"))
    assert notices.NOTICE_ID.pattern == schema["properties"]["id"]["pattern"]


def test_missing_notices_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(notices, "DATA", tmp_path / "missing")
    message = "The notices that come with breakwise are missing. Reinstall breakwise."
    assert explain(capsys, "BW-2026-0001") == (1, "", f"breakwise: {message}\n")


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])
    assert (raised.value.code, capsys.readouterr().out) == (0, f"breakwise {__version__}\n")


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main([])
    assert raised.value.code == 2


def test_the_package_reads_its_notices_where_the_build_puts_them() -> None:
    # pyproject.toml copies notices/ to breakwise/data/notices in the wheel.
    assert Path(str(notices.DATA)).parts[-3:] == ("breakwise", "data", "notices")
    notice = notices.find("BW-2026-0001", REPO_ROOT / "notices")
    assert notice.data["id"] == "BW-2026-0001"
    assert notice.url == f"{NOTICES_URL}/pypi/pydantic-ai/BW-2026-0001.yaml"


def test_the_wheel_holds_every_notice_where_the_package_reads_it(tmp_path: Path) -> None:
    # The real build, so that a change to pyproject.toml or to DATA can't go unnoticed.
    build = [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", "dist"]
    built = subprocess.run(
        [*build, str(REPO_ROOT)], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert built.returncode == 0, built.stdout + built.stderr
    (wheel,) = (tmp_path / "dist").glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        packaged = {
            name.removeprefix("breakwise/data/notices/"): archive.read(name)
            for name in names
            if name.startswith("breakwise/data/notices/")
        }
        archive.extractall(tmp_path / "unpacked")
    source = REPO_ROOT / "notices"
    files = {
        path.relative_to(source).as_posix(): path for path in source.rglob("*") if path.is_file()
    }
    # Every file of the registry, with the same bytes, and nothing else.
    assert sorted(packaged) == sorted(files)
    for rel, path in files.items():
        assert packaged[rel] == path.read_bytes(), f"{rel} differs in the wheel"
    licenses = f"breakwise-{__version__}.dist-info/licenses"
    assert {f"{licenses}/LICENSE", f"{licenses}/notices/LICENSE.md"} <= names

    # The package unpacked from the wheel finds every notice through DATA.
    ids = sorted(rel.rsplit("/", 1)[-1].removesuffix(".yaml") for rel in files if "/BW-" in rel)
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); from breakwise import notices; "
        "print(*(notices.find(notice_id).path for notice_id in sys.argv[2:]), sep='\\n')"
    )
    found = subprocess.run(
        [sys.executable, "-I", "-c", code, str(tmp_path / "unpacked"), *ids],
        capture_output=True,
        text=True,
        check=False,
    )
    assert found.returncode == 0, found.stderr
    assert sorted(found.stdout.splitlines()) == sorted(rel for rel in files if "/BW-" in rel)
