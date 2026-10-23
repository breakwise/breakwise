"""Tests for the breakwise command and the notices that come with it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import validate_notices
from breakwise import __version__, cli, notices

REPO_ROOT = Path(__file__).resolve().parent.parent

RENAME = """\
---
schema_version: "0.2"
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
schema_version: "0.2"
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
schema_version: "0.2"
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


@pytest.fixture
def bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Notices laid out as in the package, in place of the ones that come with it."""
    root = tmp_path / "notices"
    for rel, text in (
        ("pypi/pydantic-ai/BW-2026-0001.yaml", RENAME),
        ("pypi/demo/BW-2026-0002.yaml", REMOVAL),
        ("pypi/pydantic-ai/BW-2026-0003.yaml", KEYWORD_RENAME),
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
