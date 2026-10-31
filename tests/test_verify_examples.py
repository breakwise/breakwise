"""Tests for tools/verify_examples.py. They run without network access."""

from __future__ import annotations

import contextlib
import datetime as dt
import email.message
import http.client
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

import verify_examples as ve

UTC = dt.UTC
REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_NOTICE = REPO_ROOT / "notices/pypi/pydantic-ai/BW-2026-0001.yaml"
PYTHON = Path(sys.executable)
PYTHON_VERSION = Version(platform.python_version())
UPLOADED = dt.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

posix_only = pytest.mark.skipif(not ve.POSIX, reason="the verifier runs on Linux and macOS")


def releases(*versions: str) -> dict[Version, ve.Release]:
    return {Version(v): ve.Release(UPLOADED) for v in versions}


def plan(
    available: dict[Version, ve.Release], breaks_in: str, works_since: str | None = None
) -> list[tuple[str, str, bool]]:
    since = Version(works_since) if works_since else None
    checks = ve.plan_checks(available, Version(breaks_in), since)
    return [(str(c.version), c.example, c.should_run) for c in checks]


# --- PyPI ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "version"),
    [
        ("pydantic_ai-1.0.0-py3-none-any.whl", "1.0.0"),
        ("pydantic_ai-1.0.0.tar.gz", "1.0.0"),
        ("pydantic-ai-0.7.6.tar.gz", "0.7.6"),
        ("demo-2.0.zip", "2.0"),
        ("demo-1.0-py2.7.egg", None),
        ("demo-1.0.exe", None),
        ("demo-not.a.version.tar.gz", None),
        ("demo-1.0-bad.whl", None),
    ],
)
def test_version_of_reads_wheel_and_sdist_names(filename: str, version: str | None) -> None:
    found = ve.version_of(filename)
    assert (str(found) if found else None) == version


def test_parse_files_keeps_the_latest_upload_of_each_release() -> None:
    files: list[dict[str, Any]] = [
        {"filename": "demo-1.0.0.tar.gz", "upload-time": "2026-01-01T10:00:00.000000Z"},
        {"filename": "demo-1.0.0-py3-none-any.whl", "upload-time": "2026-01-01T10:05:00Z"},
        {"filename": "demo-1.0-py2-none-any.whl", "upload-time": "2026-01-01T09:00:00Z"},
        {"filename": "demo-1.1.0.tar.gz", "upload-time": "2026-02-01T10:00:00Z", "yanked": True},
        {"filename": "demo-1.2.0.tar.gz", "upload-time": "2026-03-01T10:00:00Z", "yanked": "bug"},
        {"filename": "demo-1.3.0.tar.gz", "upload-time": "2026-04-01T10:00:00Z", "yanked": False},
        {"filename": "demo-1.4.0.tar.gz"},
        {"filename": "demo-1.5.0-py2.7.egg", "upload-time": "2026-05-01T10:00:00Z"},
    ]
    assert ve.parse_files(files) == {
        Version("1.0.0"): ve.Release(dt.datetime(2026, 1, 1, 10, 5, tzinfo=UTC)),
        Version("1.3.0"): ve.Release(dt.datetime(2026, 4, 1, 10, 0, tzinfo=UTC)),
    }


def test_parse_files_keeps_the_requires_python_of_every_file() -> None:
    files: list[dict[str, Any]] = [
        {
            "filename": "demo-2.0.0-py3-none-any.whl",
            "upload-time": "2026-01-01T10:00:00Z",
            "requires-python": ">=3.13",
        },
        {"filename": "demo-2.0.0.tar.gz", "upload-time": "2026-01-01T10:05:00Z"},
        {
            "filename": "demo-2.1.0.tar.gz",
            "upload-time": "2026-02-01T10:00:00Z",
            "requires-python": ">=3.13",
        },
        {
            "filename": "demo-2.2.0.tar.gz",
            "upload-time": "2026-03-01T10:00:00Z",
            "requires-python": "",
        },
    ]
    assert ve.parse_files(files) == {
        # A file that states no Requires-Python is kept as None.
        Version("2.0.0"): ve.Release(
            dt.datetime(2026, 1, 1, 10, 5, tzinfo=UTC), frozenset({">=3.13", None})
        ),
        Version("2.1.0"): ve.Release(
            dt.datetime(2026, 2, 1, 10, 0, tzinfo=UTC), frozenset({">=3.13"})
        ),
        Version("2.2.0"): ve.Release(dt.datetime(2026, 3, 1, 10, 0, tzinfo=UTC)),
    }


class FakePyPI:
    """Stands in for urllib.request.urlopen: fails with the given errors, then answers."""

    def __init__(self, *errors: Exception, body: bytes | None = None) -> None:
        self.errors = list(errors)
        self.body = body
        self.calls = 0

    def __call__(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        self.calls += 1
        assert request.full_url == "https://pypi.org/simple/demo/"
        assert request.get_header("Accept") == "application/vnd.pypi.simple.v1+json"
        assert timeout == ve.PYPI_TIMEOUT
        if self.errors:
            raise self.errors.pop(0)
        if self.body is not None:
            return io.BytesIO(self.body)
        files = [{"filename": "demo-1.0.0.tar.gz", "upload-time": "2026-01-01T12:00:00Z"}]
        return io.BytesIO(json.dumps({"meta": {"api-version": "1.4"}, "files": files}).encode())


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://pypi.org", code, "error", email.message.Message(), None)


def test_fetch_releases_retries_until_pypi_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    errors = (urllib.error.URLError("connection reset"), http_error(500), http_error(429))
    pypi = FakePyPI(*errors)
    monkeypatch.setattr(urllib.request, "urlopen", pypi)
    waits: list[float] = []
    assert ve.fetch_releases("demo", attempts=4, sleep=waits.append) == releases("1.0.0")
    assert (pypi.calls, waits) == (4, [2.0, 4.0, 8.0])


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (http_error(502), "HTTP 502"),
        (urllib.error.URLError("Name or service not known"), "Name or service not known"),
        (TimeoutError(), "TimeoutError"),
        (http.client.IncompleteRead(b"partial"), "IncompleteRead"),
    ],
)
def test_fetch_releases_gives_up_after_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str
) -> None:
    pypi = FakePyPI(*[error] * 3)
    monkeypatch.setattr(urllib.request, "urlopen", pypi)
    waits: list[float] = []
    with pytest.raises(ve.VerifyError) as raised:
        ve.fetch_releases("demo", sleep=waits.append)
    assert str(raised.value).startswith(f"Could not read demo from PyPI after 3 attempts: {reason}")
    assert (pypi.calls, waits) == (3, [2.0, 4.0])


@pytest.mark.parametrize(
    ("code", "message"),
    [(404, "demo is not on PyPI."), (403, "PyPI refused the request for demo: HTTP 403.")],
)
def test_fetch_releases_does_not_retry_client_errors(
    monkeypatch: pytest.MonkeyPatch, code: int, message: str
) -> None:
    pypi = FakePyPI(http_error(code))
    monkeypatch.setattr(urllib.request, "urlopen", pypi)
    with pytest.raises(ve.VerifyError) as raised:
        ve.fetch_releases("demo", sleep=lambda _: None)
    assert (str(raised.value), pypi.calls) == (message, 1)


def test_fetch_releases_closes_every_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # An HTTPError holds PyPI's response. Python 3.14 warns when one is left open.
    errors = [http_error(500), http_error(429), http_error(404)]
    monkeypatch.setattr(urllib.request, "urlopen", FakePyPI(*errors))
    with pytest.raises(ve.VerifyError, match="demo is not on PyPI"):
        ve.fetch_releases("demo", sleep=lambda _: None)
    assert [error.closed for error in errors] == [True, True, True]


@pytest.mark.parametrize("body", [b"<html>not json</html>", b'{"meta": {}}', b'{"files": [1]}'])
def test_fetch_releases_reports_unexpected_answers(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", FakePyPI(body=body))
    with pytest.raises(ve.VerifyError, match="PyPI sent an unexpected response for demo"):
        ve.fetch_releases("demo")


# --- Planning -----------------------------------------------------------------


def test_plan_without_works_since() -> None:
    available = releases("1.0.0", "1.1.0", "2.0.0rc1", "2.0.0", "2.1.0")
    assert plan(available, "2.0.0") == [
        ("1.1.0", "before", True),
        ("2.0.0rc1", "before", True),
        ("2.0.0", "before", False),
        ("2.0.0", "after", True),
    ]


def test_plan_with_works_since_before_the_previous_release() -> None:
    available = releases("0.7.5", "0.7.6", "1.107.9", "2.0.0")
    assert plan(available, "2.0.0", "0.7.6") == [
        ("1.107.9", "before", True),
        ("1.107.9", "after", True),
        ("2.0.0", "before", False),
        ("2.0.0", "after", True),
        ("0.7.6", "after", True),
        ("0.7.5", "after", False),
    ]


def test_plan_does_not_repeat_versions() -> None:
    available = releases("1.0.0", "1.1.0", "2.0.0")
    assert plan(available, "2.0.0", "1.1.0") == [
        ("1.1.0", "before", True),
        ("1.1.0", "after", True),
        ("2.0.0", "before", False),
        ("2.0.0", "after", True),
        ("1.0.0", "after", False),
    ]
    assert plan(available, "2.0.0", "2.0.0") == [
        ("1.1.0", "before", True),
        ("2.0.0", "before", False),
        ("2.0.0", "after", True),
        ("1.1.0", "after", False),
    ]


def test_plan_checks_a_pre_release_works_since_on_its_own() -> None:
    available = releases("1.0.0", "2.0.0b1", "2.0.0")
    assert plan(available, "2.0.0", "2.0.0b1") == [
        ("1.0.0", "before", True),
        ("2.0.0b1", "before", True),
        ("2.0.0", "before", False),
        ("2.0.0", "after", True),
        ("2.0.0b1", "after", True),
        ("1.0.0", "after", False),
    ]


def test_plan_runs_before_on_the_release_just_before_breaks_in() -> None:
    # Only the nearest pre-release: if 2.0.0b1 still runs the old code, so did 2.0.0a1.
    available = releases("1.9.0", "2.0.0a1", "2.0.0b1", "2.0.0")
    assert plan(available, "2.0.0") == [
        ("1.9.0", "before", True),
        ("2.0.0b1", "before", True),
        ("2.0.0", "before", False),
        ("2.0.0", "after", True),
    ]


def test_plan_has_nothing_before_a_works_since_that_is_the_first_release() -> None:
    available = releases("1.0.0", "2.0.0")
    assert plan(available, "2.0.0", "1.0.0") == [
        ("1.0.0", "before", True),
        ("1.0.0", "after", True),
        ("2.0.0", "before", False),
        ("2.0.0", "after", True),
    ]


@pytest.mark.parametrize(
    ("available", "breaks_in", "works_since", "message"),
    [
        (("1.0.0",), "2.0.0", None, "breaks_in 2.0.0 is not a release on PyPI"),
        (("1.0.0", "2.0.0"), "2.0.0", "1.5.0", "works_since 1.5.0 is not a release on PyPI"),
        (("2.0.0rc1", "2.0.0"), "2.0.0", None, "no stable release before breaks_in 2.0.0"),
    ],
)
def test_plan_reports_versions_it_cannot_use(
    available: tuple[str, ...], breaks_in: str, works_since: str | None, message: str
) -> None:
    with pytest.raises(ve.VerifyError, match=message):
        plan(releases(*available), breaks_in, works_since)


def full_plan(
    available: dict[Version, ve.Release],
    breaks_in: str,
    latest: str | None = None,
    restored_in: str | None = None,
) -> list[tuple[str, str, bool, bool]]:
    checks = ve.plan_checks(
        available,
        Version(breaks_in),
        None,
        Version(latest) if latest else None,
        Version(restored_in) if restored_in else None,
    )
    return [(str(c.version), c.example, c.should_run, c.latest) for c in checks]


BREAKS = [("1.9.0", "before", True, False), ("2.0.0", "before", False, False)]
BREAKS += [("2.0.0", "after", True, False)]


def test_plan_with_restored_in() -> None:
    available = releases("1.9.0", "2.0.0", "2.1.0", "2.2.0")
    assert full_plan(available, "2.0.0", restored_in="2.2.0") == [
        *BREAKS,
        # example.before still fails just before restored_in, and runs on it.
        ("2.1.0", "before", False, False),
        ("2.2.0", "before", True, False),
        ("2.2.0", "after", True, False),
    ]


def test_plan_with_restored_in_right_after_breaks_in() -> None:
    # The release just before restored_in is breaks_in, which is checked already.
    available = releases("1.9.0", "2.0.0", "2.1.0")
    assert full_plan(available, "2.0.0", restored_in="2.1.0") == [
        *BREAKS,
        ("2.1.0", "before", True, False),
        ("2.1.0", "after", True, False),
    ]


@pytest.mark.parametrize(
    ("latest", "restored_in", "last"),
    [
        pytest.param(
            "2.1.0",
            "3.0.0",
            [
                ("2.5.0", "before", False, False),
                ("3.0.0", "before", True, False),
                ("3.0.0", "after", True, False),
                ("2.1.0", "before", False, True),
                ("2.1.0", "after", True, True),
            ],
            id="the latest release is before restored_in",
        ),
        pytest.param(
            "3.0.0",
            "2.5.0",
            [
                ("2.1.0", "before", False, False),
                ("2.5.0", "before", True, False),
                ("2.5.0", "after", True, False),
                ("3.0.0", "before", True, True),
                ("3.0.0", "after", True, True),
            ],
            id="the latest release is after restored_in",
        ),
        pytest.param(
            "3.0.0",
            "3.0.0",
            [
                ("2.5.0", "before", False, False),
                ("3.0.0", "before", True, False),
                ("3.0.0", "after", True, False),
            ],
            id="the latest release is restored_in",
        ),
    ],
)
def test_plan_of_the_latest_release_follows_restored_in(
    latest: str, restored_in: str, last: list[tuple[str, str, bool, bool]]
) -> None:
    available = releases("1.9.0", "2.0.0", "2.1.0", "2.5.0", "3.0.0")
    assert full_plan(available, "2.0.0", latest, restored_in) == [*BREAKS, *last]


def test_plan_does_not_repeat_the_latest_release_when_restored_in_plans_it() -> None:
    # The latest release is the one just before restored_in, where example.before fails.
    available = releases("1.9.0", "2.0.0", "2.1.0", "2.5.0")
    assert full_plan(available, "2.0.0", "2.1.0", "2.5.0") == [
        *BREAKS,
        ("2.1.0", "before", False, False),
        ("2.5.0", "before", True, False),
        ("2.5.0", "after", True, False),
        ("2.1.0", "after", True, True),
    ]


def test_restored_in_must_be_a_release_on_pypi() -> None:
    with pytest.raises(ve.VerifyError) as raised:
        full_plan(releases("1.9.0", "2.0.0"), "2.0.0", restored_in="2.5.0")
    assert str(raised.value) == "restored_in 2.5.0 is not a release on PyPI, or it was yanked."


def test_dependencies_are_cut_off_one_day_after_the_release() -> None:
    uploaded = dt.datetime(2025, 8, 20, 15, 31, 24, 123456, tzinfo=UTC)
    assert ve.exclude_newer(uploaded) == "2025-08-21T15:31:24Z"
    in_riyadh = dt.datetime(2025, 8, 20, 18, 31, 24, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert ve.exclude_newer(in_riyadh) == "2025-08-21T15:31:24Z"


# --- The latest release -------------------------------------------------------

PY312 = Version("3.12.15")


def requiring(spec: str, *versions: str) -> dict[Version, ve.Release]:
    return {Version(v): ve.Release(UPLOADED, frozenset({spec})) for v in versions}


@pytest.mark.parametrize(
    ("available", "breaks_in", "latest"),
    [
        pytest.param(releases("1.0", "2.0", "2.1", "2.2"), "2.0", "2.2", id="latest stable"),
        pytest.param(
            releases("1.0", "2.0", "2.1", "2.2b1"), "2.0", "2.1", id="stable over newer pre-release"
        ),
        pytest.param(
            releases("1.0", "2.0b1", "2.0b2", "2.0rc1"), "2.0b1", "2.0rc1", id="only pre-releases"
        ),
        pytest.param(
            releases("1.0", "2.0b1", "2.0b2", "2.0"), "2.0b1", "2.0", id="stable after pre-release"
        ),
        pytest.param(
            releases("1.0", "2.0", "2.1b1"), "2.0", None, id="only pre-releases after stable"
        ),
        pytest.param(releases("1.0", "2.0"), "2.0", None, id="nothing after breaks_in"),
    ],
)
def test_latest_release(
    available: dict[Version, ve.Release], breaks_in: str, latest: str | None
) -> None:
    expected = Version(latest) if latest else None
    assert ve.latest_release(available, Version(breaks_in), PY312) == (expected, None)


def test_a_latest_release_that_does_not_accept_the_python_is_passed_over() -> None:
    available = {**releases("1.0", "2.0", "2.1"), **requiring(">=3.13", "2.2", "2.3")}
    assert ve.latest_release(available, Version("2.0"), PY312) == (
        Version("2.1"),
        ve.Result(
            ve.LatestNote(Version("2.3")),
            True,
            "2.3 requires Python >=3.13, so the latest release checked is 2.1",
        ),
    )


def test_without_a_release_that_accepts_the_python_the_latest_is_only_noted() -> None:
    available = {**releases("1.0", "2.0"), **requiring(">=3.13", "2.1")}
    assert ve.latest_release(available, Version("2.0"), PY312) == (
        None,
        ve.Result(
            ve.LatestNote(Version("2.1")),
            True,
            "not checked: no release after 2.0 accepts Python 3.12.15; 2.1 requires Python >=3.13",
        ),
    )


@pytest.mark.parametrize(
    ("requires_python", "accepted"),
    [
        pytest.param(frozenset({None}), True, id="no Requires-Python"),
        pytest.param(frozenset({">=3.11"}), True, id="accepted"),
        pytest.param(frozenset({">=3.13"}), False, id="not accepted"),
        pytest.param(frozenset({">=3.13", None}), True, id="another file states none"),
        pytest.param(frozenset({">=3.13", "<3.13"}), True, id="another file accepts it"),
        pytest.param(
            frozenset({"not a specifier"}), True, id="a Requires-Python that can't be read"
        ),
    ],
)
def test_a_release_accepts_a_python_when_one_of_its_files_does(
    requires_python: frozenset[str | None], accepted: bool
) -> None:
    available = {**releases("1.0", "2.0"), Version("2.1"): ve.Release(UPLOADED, requires_python)}
    chosen, _ = ve.latest_release(available, Version("2.0"), PY312)
    assert (chosen == Version("2.1")) is accepted


def test_the_latest_release_is_planned_last() -> None:
    available = releases("1.0", "1.5", "2.0", "2.5")
    checks = ve.plan_checks(available, Version("2.0"), Version("1.5"), Version("2.5"))
    assert [(str(c.version), c.example, c.should_run) for c in checks if c.latest] == [
        ("2.5", "before", False),
        ("2.5", "after", True),
    ]
    assert checks[-2:] == [c for c in checks if c.latest]


@pytest.fixture
def fresh_examples_python() -> Iterator[None]:
    """examples_python() keeps its answer for the run, so each test starts without one."""
    ve.examples_python.cache_clear()
    yield
    ve.examples_python.cache_clear()


@pytest.mark.usefixtures("fresh_examples_python")
def test_examples_python_asks_an_environment_once_for_each_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run_tool(command: list[str]) -> str:
        commands.append(command)
        return "3.12.15\n" if command[1] == "-I" else ""

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ve, "_run_tool", run_tool)
    assert ve.examples_python("3.12") == ve.examples_python("3.12") == Version("3.12.15")
    assert len(commands) == 2
    assert commands[0][:6] == ["/usr/bin/uv", "venv", "--no-config", "--quiet", "--python", "3.12"]
    assert Path(commands[1][0]).parts[-2:] == ("bin", "python")
    assert commands[1][1:] == ["-I", "-c", "import platform; print(platform.python_version())"]


@pytest.mark.usefixtures("fresh_examples_python")
def test_examples_python_reports_an_answer_that_is_not_a_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ve, "_run_tool", lambda command: "not a version\n")
    with pytest.raises(ve.VerifyError, match="gave 'not a version' as its version"):
        ve.examples_python("3.12")


@posix_only
@pytest.mark.usefixtures("fresh_examples_python")
def test_examples_python_finds_the_version_of_a_real_python() -> None:
    assert ve.examples_python(sys.executable) == PYTHON_VERSION


# --- Installing ---------------------------------------------------------------

FAKE_UV = """\
#!{python}
import os, sys
from pathlib import Path

with open(os.environ["FAKE_UV_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "venv":
    (Path(sys.argv[-1]) / "bin").mkdir(parents=True)
elif os.environ.get("FAKE_UV_FAIL"):
    sys.exit("No solution found when resolving dependencies")
"""


@pytest.fixture
def fake_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake uv first on PATH. Return the file where it logs its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV.format(python=sys.executable), encoding="utf-8")
    uv.chmod(0o755)
    log = tmp_path / "uv.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UV_LOG", str(log))
    return log


@posix_only
def test_install_pins_the_version_and_its_dependency_date(fake_uv: Path, tmp_path: Path) -> None:
    target = tmp_path / "env"
    interpreter = ve.install("demo", Version("1.2.3"), "2026-01-02T12:00:00Z", "python3", target)
    assert interpreter == target / "bin" / "python"
    venv_call, pip_call = fake_uv.read_text(encoding="utf-8").splitlines()
    assert venv_call == f"venv --no-config --quiet --python python3 {target}"
    assert pip_call == (
        f"pip install --no-config --quiet --python {interpreter} "
        "--default-index https://pypi.org/simple --exclude-newer 2026-01-02T12:00:00Z demo==1.2.3"
    )


INDEX_VARIABLES = ("UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL")


@pytest.fixture
def tool_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str]]]:
    """Record each tool the verifier runs, with its environment, in a machine with uv settings."""
    for name in INDEX_VARIABLES:
        monkeypatch.setenv(name, "http://127.0.0.1:9/simple")
    monkeypatch.setenv("UV_PYTHON", "3.99")
    monkeypatch.setenv("UV_CACHE_DIR", "/cache/uv")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    runs: list[tuple[list[str], dict[str, str]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        runs.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, stdout="3.12.15\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return runs


def assert_no_uv_settings_but_the_kept_ones(env: dict[str, str]) -> None:
    assert not [name for name in INDEX_VARIABLES if name in env]
    assert "UV_PYTHON" not in env
    # Where uv keeps its files and how the machine connects stay.
    assert (env["UV_CACHE_DIR"], env["HTTPS_PROXY"]) == ("/cache/uv", "http://proxy.example:3128")


def test_install_comes_from_pypi_whatever_the_uv_variables_say(
    tool_runs: list[tuple[list[str], dict[str, str]]], tmp_path: Path
) -> None:
    ve.install("demo", Version("1.2.3"), "2026-01-02T12:00:00Z", "python3", tmp_path / "env")
    assert [command[1] for command, _ in tool_runs] == ["venv", "pip"]
    pip_command = tool_runs[1][0]
    index = pip_command.index("--default-index")
    assert pip_command[index + 1] == "https://pypi.org/simple"
    for _, env in tool_runs:
        assert_no_uv_settings_but_the_kept_ones(env)


@pytest.mark.usefixtures("fresh_examples_python")
def test_examples_python_runs_without_the_uv_variables_too(
    tool_runs: list[tuple[list[str], dict[str, str]]],
) -> None:
    assert ve.examples_python("3.12") == Version("3.12.15")
    assert len(tool_runs) == 2
    for _, env in tool_runs:
        assert_no_uv_settings_but_the_kept_ones(env)


@posix_only
def test_install_reports_why_it_failed(
    fake_uv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_UV_FAIL", "1")
    with pytest.raises(ve.VerifyError) as raised:
        ve.install("demo", Version("1.0"), "2026-01-02T12:00:00Z", "python3", tmp_path / "env")
    assert str(raised.value).startswith("Failed: ")
    assert str(raised.value).endswith("\nNo solution found when resolving dependencies")


def test_install_needs_uv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ve.VerifyError, match="uv is not installed"):
        ve.install("demo", Version("1.0"), "2026-01-02T12:00:00Z", "python3", tmp_path / "env")


def test_install_steps_that_hang_are_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ve, "INSTALL_TIMEOUT", 0.5)
    with pytest.raises(ve.VerifyError, match="Timed out: "):
        ve._run_tool([sys.executable, "-c", "import time; time.sleep(10)"])


# --- Running examples ---------------------------------------------------------


@posix_only
def test_the_exception_that_ends_an_example_is_recorded(tmp_path: Path) -> None:
    code = "try:\n    1 / 0\nexcept ZeroDivisionError:\n    pass\nraise ImportError('no X here')\n"
    run = ve.run_example(PYTHON, code, tmp_path)
    assert (run.returncode, run.error, run.used_network) == (1, "ImportError: no X here\n", False)
    assert run.summary == "ImportError: no X here"


@posix_only
def test_a_failure_on_a_line_that_mentions_the_old_name_is_not_enough(tmp_path: Path) -> None:
    # The traceback quotes the failing line, so it contains the old name, but the error does not.
    run = ve.run_example(PYTHON, "Old = 1\nprint(Old / 0)\n", tmp_path)
    assert "Old" in run.stderr
    check = ve.Check(Version("2.0"), "before", should_run=False)
    result = ve.evaluate(check, run, ve.Failure(named="Old"))
    assert (result.passed, result.detail) == (
        False,
        "fails, but not because of Old: ZeroDivisionError: division by zero",
    )


@posix_only
def test_examples_cannot_use_the_network(tmp_path: Path) -> None:
    code = "import socket\nsocket.create_connection(('example.com', 80), timeout=5)\n"
    run = ve.run_example(PYTHON, code, tmp_path)
    assert run.used_network
    assert ve.NETWORK_BLOCKED in run.error
    result = ve.evaluate(ve.Check(Version("1.0"), "after", should_run=True), run)
    assert not result.passed
    assert result.detail.startswith(
        "failed after it tried to use the network; examples must run offline: OSError: "
    )


@posix_only
@pytest.mark.parametrize(
    "call",
    [
        "socket.getaddrinfo('example.com', 443)",
        "socket.gethostbyname('example.com')",
        "socket.gethostbyname_ex('example.com')",
        "socket.socket().connect(('192.0.2.1', 80))",
        "socket.socket().connect_ex(('192.0.2.1', 80))",
    ],
)
def test_every_way_to_reach_the_network_is_blocked(tmp_path: Path, call: str) -> None:
    run = ve.run_example(PYTHON, f"import socket\n{call}\n", tmp_path)
    assert run.used_network
    assert ve.NETWORK_BLOCKED in run.error


@posix_only
def test_network_use_is_caught_even_when_the_error_is_reworded(tmp_path: Path) -> None:
    code = (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection(('example.com', 80))\n"
        "except OSError:\n"
        "    sys.exit('ImportError: cannot import name OpenAIModel')\n"
    )
    run = ve.run_example(PYTHON, code, tmp_path)
    assert ve.NETWORK_BLOCKED not in run.stderr
    check = ve.Check(Version("2.0"), "before", should_run=False)
    result = ve.evaluate(check, run, ve.Failure(named="OpenAIModel"))
    assert not result.passed
    assert "tried to use the network" in result.detail


@posix_only
def test_a_blocked_call_that_the_example_ignores_is_reported_but_allowed(tmp_path: Path) -> None:
    code = (
        "import socket\ntry:\n    socket.getaddrinfo('example.com', 443)\nexcept OSError:\n    pass"
    )
    result = ve.evaluate(
        ve.Check(Version("1.0"), "after", should_run=True), ve.run_example(PYTHON, code, tmp_path)
    )
    assert (result.passed, result.detail) == (
        True,
        "runs (a network call it made was blocked and ignored)",
    )


@posix_only
def test_examples_get_fake_keys_and_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BREAKWISE_TEST_SECRET", "do-not-leak")
    code = (
        "import os, sys\n"
        "assert 'BREAKWISE_TEST_SECRET' not in os.environ\n"
        f"assert os.environ['OPENAI_API_KEY'] == {ve.FAKE_KEY!r}\n"
        f"assert os.environ['GROK_API_KEY'] == {ve.FAKE_KEY!r}\n"
        f"assert os.environ['HOME'] == {str(tmp_path)!r}\n"
        "assert sys.flags.isolated and sys.flags.utf8_mode\n"
    )
    run = ve.run_example(PYTHON, code, tmp_path)
    assert (run.returncode, run.stderr, run.used_network) == (0, "", False)


@posix_only
def test_output_that_is_not_utf8_does_not_break_the_run(tmp_path: Path) -> None:
    code = "import sys\nsys.stderr.buffer.write(b'bad \\xff byte\\n')\nsys.exit(3)\n"
    run = ve.run_example(PYTHON, code, tmp_path)
    assert (run.returncode, run.stderr, run.summary) == (3, "bad � byte\n", "SystemExit: 3")


def _alive(pid: int) -> bool:
    """Whether a process runs. A zombie, which has ended but was not collected yet, does not."""
    try:
        os.kill(pid, 0)
        # Linux only; the state follows the name in parentheses.
        stat = Path(f"/proc/{pid}/stat").read_text()
    except ProcessLookupError:
        # The process ended, also when that happens between kill() and the read.
        return False
    except FileNotFoundError:
        # Without /proc, as on macOS, kill() is all there is. On Linux the process just ended.
        return not Path("/proc").is_dir()
    return stat.rpartition(")")[2].split()[0] != "Z"


def _wait_until_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


@pytest.mark.parametrize(
    ("kill_error", "stat", "has_proc", "alive"),
    [
        pytest.param(None, "42 (sleep (60)) S 1 42", True, True, id="runs"),
        pytest.param(None, "42 (python3) Z 1 42", True, False, id="zombie"),
        pytest.param(ProcessLookupError(), "", True, False, id="ended before kill"),
        pytest.param(None, ProcessLookupError(), True, False, id="ended before the read"),
        pytest.param(None, FileNotFoundError(), True, False, id="ended before the open"),
        pytest.param(None, FileNotFoundError(), False, True, id="no /proc, as on macOS"),
    ],
)
def test_alive_counts_a_process_that_vanishes_at_any_moment_as_ended(
    monkeypatch: pytest.MonkeyPatch,
    kill_error: OSError | None,
    stat: str | OSError,
    has_proc: bool,
    alive: bool,
) -> None:
    # A real process can't be made to end at an exact moment, so the system calls are faked.
    def kill(pid: int, _signal: int) -> None:
        assert pid == 42
        if kill_error is not None:
            raise kill_error

    def read_text(path: Path, *_args: object, **_kwargs: object) -> str:
        assert path == Path("/proc/42/stat")
        if isinstance(stat, OSError):
            raise stat
        return stat

    def is_dir(path: Path) -> bool:
        assert path == Path("/proc")
        return has_proc

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "is_dir", is_dir)
    assert _alive(42) is alive


@posix_only
def test_a_hanging_example_is_stopped_with_every_process_it_started(tmp_path: Path) -> None:
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('child.pid', 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    run = ve.run_example(PYTHON, code, tmp_path, timeout=2)
    assert run.returncode is None
    assert time.monotonic() - started < 15
    assert _wait_until_gone(int((tmp_path / "child.pid").read_text()))
    result = ve.evaluate(ve.Check(Version("1.0"), "before", should_run=False), run)
    assert (result.passed, result.detail) == (False, "did not finish in time, so it was stopped")


@posix_only
def test_a_process_that_leaves_the_group_cannot_hold_the_run_open(tmp_path: Path) -> None:
    # The grandchild starts its own session, so it survives the kill and keeps stderr open.
    escapee = (
        "import os, time; os.setsid(); "
        "open('escapee.pid', 'w').write(str(os.getpid())); time.sleep(30)"
    )
    code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {escapee!r}])\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    try:
        run = ve.run_example(PYTHON, code, tmp_path, timeout=1)
        assert run.returncode is None
        assert time.monotonic() - started < 1 + ve.STOPPED_OUTPUT_TIMEOUT + 5
    finally:
        # The escapee may not have written its PID, may have created the file but not
        # written it yet, or may have ended already.
        with contextlib.suppress(FileNotFoundError, ProcessLookupError, ValueError):
            os.kill(int((tmp_path / "escapee.pid").read_text()), 9)


@pytest.mark.parametrize(
    ("should_run", "returncode", "error", "expected", "passed", "detail"),
    [
        (True, 0, "", None, True, "runs"),
        (True, 1, "ImportError: X\n", None, False, "should run, but failed: ImportError: X"),
        (False, 0, "", None, False, "should fail on this version, but it ran"),
        (False, 1, "TypeError: f() got 2\n", None, True, "fails as expected: TypeError: f() got 2"),
        (
            False,
            1,
            "ImportError: cannot import name 'OpenAIModel'\n",
            ve.Failure(named="OpenAIModel"),
            True,
            "fails as expected: ImportError: cannot import name 'OpenAIModel'",
        ),
        (
            False,
            1,
            "ZeroDivisionError: division by zero\n",
            ve.Failure(named="OpenAIModel"),
            False,
            "fails, but not because of OpenAIModel: ZeroDivisionError: division by zero",
        ),
        pytest.param(
            False,
            1,
            "TypeError: Agent.__init__() got an unexpected keyword argument 'mcp_servers'\n",
            ve.Failure(error="TypeError", message="unexpected keyword argument 'mcp_servers'"),
            True,
            "fails as expected: TypeError: Agent.__init__() got an unexpected keyword argument "
            "'mcp_servers'",
            id="fails_with matches",
        ),
        pytest.param(
            False,
            1,
            "ModuleNotFoundError: No module named 'pydantic_ai'\n",
            ve.Failure(error="TypeError"),
            False,
            "fails, but with ModuleNotFoundError instead of TypeError: "
            "ModuleNotFoundError: No module named 'pydantic_ai'",
            id="another exception",
        ),
        pytest.param(
            False,
            1,
            "TypeError: f() takes 1 positional argument but 2 were given\n",
            ve.Failure(error="TypeError", message="mcp_servers"),
            False,
            "fails with TypeError, but its message doesn't contain 'mcp_servers': "
            "TypeError: f() takes 1 positional argument but 2 were given",
            id="another message",
        ),
        pytest.param(
            False,
            3,
            "",
            ve.Failure(error="TypeError"),
            False,
            "fails, but with no exception instead of TypeError: exit code 3",
            id="no exception",
        ),
        pytest.param(
            False,
            1,
            "pydantic_ai.exceptions.UserError: Unknown keyword arguments: `toolsets`\n",
            ve.Failure(error="pydantic_ai.exceptions.UserError", message="toolsets"),
            True,
            "fails as expected: pydantic_ai.exceptions.UserError: Unknown keyword arguments: "
            "`toolsets`",
            id="an exception with its module",
        ),
        pytest.param(
            False,
            1,
            "ImportError: cannot import name 'Other'\n",
            ve.Failure(named="OpenAIModel", error="ImportError"),
            False,
            "fails, but not because of OpenAIModel: ImportError: cannot import name 'Other'",
            id="a rename checks its name first",
        ),
    ],
)
def test_evaluate(
    should_run: bool,
    returncode: int,
    error: str,
    expected: ve.Failure | None,
    passed: bool,
    detail: str,
) -> None:
    check = ve.Check(Version("2.0.0"), "before", should_run=should_run)
    # In these errors, the type's name is what comes before the first ":".
    run = ve.Run(returncode, "", error, error_type=error.partition(":")[0])
    result = ve.evaluate(check, run, expected)
    assert (result.passed, result.detail) == (passed, detail)


@posix_only
@pytest.mark.parametrize(
    ("code", "name"),
    [
        pytest.param("int('x')\n", "ValueError", id="a builtin"),
        pytest.param(
            "import json\njson.loads('')\n", "json.decoder.JSONDecodeError", id="a module"
        ),
        pytest.param("class Late(Exception): pass\nraise Late\n", "Late", id="the example's own"),
        pytest.param(
            "class Outer:\n    class Inner(Exception): pass\nraise Outer.Inner('x')\n",
            "Outer.Inner",
            id="a nested class",
        ),
        pytest.param("x = 1\n", "", id="no exception"),
    ],
)
def test_the_type_of_an_exception_is_named_as_python_prints_it(
    tmp_path: Path, code: str, name: str
) -> None:
    run = ve.run_example(PYTHON, code, tmp_path)
    assert ve.exception_name(run) == name


@posix_only
def test_a_syntax_error_in_a_library_is_named_by_its_type(tmp_path: Path) -> None:
    # Python prints the file and the line of code before the type's name.
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    code = "import sys\nsys.path.insert(0, '.')\nimport broken\n"
    run = ve.run_example(PYTHON, code, tmp_path)
    assert run.error.startswith('  File "')
    assert ve.exception_name(run) == "SyntaxError"
    check = ve.Check(Version("2.0"), "before", should_run=False)
    result = ve.evaluate(check, run, ve.Failure(error="SyntaxError"))
    assert result.passed


def test_run_summary_is_one_short_line() -> None:
    assert ve.Run(1).summary == "exit code 1"
    assert ve.Run(1, "Traceback\n  line\nValueError: x\n\n").summary == "ValueError: x"
    assert ve.Run(1, "ignored\n", "KeyError: 'a'\nnote\n").summary == "KeyError: 'a'"
    assert ve.Run(1, "E" * 300).summary == "E" * 197 + "..."
    assert ve.Run(1, "E" * 200).summary == "E" * 200


SIGNATURE = ("demo.make", "new")


@pytest.mark.parametrize(
    ("probe", "passed", "detail"),
    [
        (ve.Probe(signatures={SIGNATURE: True}), True, "takes new as an explicit parameter"),
        (
            ve.Probe(failure="did not finish in time, so it was stopped"),
            False,
            "did not finish in time, so it was stopped",
        ),
        (
            ve.Probe(signatures={SIGNATURE: False}),
            False,
            (
                "has no explicit parameter new, and taking it through **kwargs does not count. "
                "Check the versions, or remove demo.make from rename.keyword_of."
            ),
        ),
        (
            ve.Probe(signatures={SIGNATURE: "ModuleNotFoundError: No module named 'demo'"}),
            False,
            "could not be checked: ModuleNotFoundError: No module named 'demo'",
        ),
        (ve.Probe(), False, "could not be checked: the probe has no result for it"),
    ],
)
def test_evaluate_signature(probe: ve.Probe, passed: bool, detail: str) -> None:
    check = ve.SignatureCheck(Version("2.0.0"), *SIGNATURE)
    result = ve.evaluate_signature(check, probe)
    assert (result.check, result.passed, result.detail) == (check, passed, detail)


@posix_only
@pytest.mark.parametrize(
    ("target", "keyword", "found"),
    [
        ("json.dumps", "indent", True),  # keyword-only
        ("textwrap.wrap", "width", True),  # positional or keyword
        ("argparse.ArgumentParser", "prog", True),  # a class, by its __init__
        ("textwrap.TextWrapper.wrap", "text", True),  # a method
        ("textwrap.wrap", "break_long_words", False),  # only through **kwargs
        ("json.dumps", "missing", False),
        ("builtins.divmod", "x", False),  # positional-only
        (
            "textwrap.missing",
            "text",
            "AttributeError: module 'textwrap' has no attribute 'missing'",
        ),
        ("missing_module.f", "text", "ModuleNotFoundError: No module named 'missing_module'"),
    ],
)
def test_the_probe_accepts_only_explicit_keyword_parameters(
    tmp_path: Path, target: str, keyword: str, found: bool | str
) -> None:
    probe = ve.run_probe(PYTHON, [], [(target, keyword)], tmp_path / "probe")
    assert probe == ve.Probe({}, {(target, keyword): found})


@posix_only
def test_the_probe_resolves_paths_as_imports_and_getattr_do(tmp_path: Path) -> None:
    paths = [
        "json.dumps",
        "textwrap.TextWrapper.wrap",  # a method of a class
        "email.mime.text.MIMEText",  # a module that its package doesn't import
        "os.path",
        "json.missing",
        "missing_module.f",
    ]
    probe = ve.run_probe(PYTHON, paths, [], tmp_path / "probe")
    assert probe.paths == {
        "json.dumps": None,
        "textwrap.TextWrapper.wrap": None,
        "email.mime.text.MIMEText": None,
        "os.path": None,
        "json.missing": "AttributeError: module 'json' has no attribute 'missing'",
        "missing_module.f": "ModuleNotFoundError: No module named 'missing_module'",
    }
    assert (probe.failure, probe.used_network) == ("", False)


@pytest.mark.parametrize(
    ("run", "written", "failure"),
    [
        (ve.Run(None), None, "did not finish in time, so it was stopped"),
        (ve.Run(1, "", "SystemExit: 3\n"), None, "could not be checked: SystemExit: 3"),
        (ve.Run(0), None, "could not be checked: the probe wrote no results"),
        (ve.Run(0), "not JSON", "could not be checked: the probe wrote results that are not valid"),
        (ve.Run(0), "[1]", "could not be checked: the probe wrote results that are not valid"),
        (
            ve.Run(0),
            '{"paths": {}}',
            "could not be checked: the probe wrote results that are not valid",
        ),
    ],
)
def test_a_probe_that_fails_says_why(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run: ve.Run,
    written: str | None,
    failure: str,
) -> None:
    def fake_run(interpreter: Path, code: str, workdir: Path, timeout: float) -> ve.Run:
        assert code == ve.PROBE_SCRIPT
        if written is not None:
            (workdir / ve.PROBE_RESULT).write_text(written, encoding="utf-8")
        return run

    monkeypatch.setattr(ve, "run_example", fake_run)
    assert ve.run_probe(PYTHON, ["a.b"], [], tmp_path / "probe") == ve.Probe(failure=failure)


def test_a_probe_reads_what_it_wrote_and_whether_it_used_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(interpreter: Path, code: str, workdir: Path, timeout: float) -> ve.Run:
        request = json.loads((workdir / ve.PROBE_REQUEST).read_text(encoding="utf-8"))
        assert request == {"paths": ["a.b"], "signatures": [["a.f", "x"]]}
        result = {
            "paths": {"a.b": None},
            "signatures": [{"target": "a.f", "keyword": "x", "explicit": True, "error": None}],
        }
        (workdir / ve.PROBE_RESULT).write_text(json.dumps(result), encoding="utf-8")
        return ve.Run(0, used_network=True)

    monkeypatch.setattr(ve, "run_example", fake_run)
    probe = ve.run_probe(PYTHON, ["a.b"], [("a.f", "x")], tmp_path / "probe")
    assert probe == ve.Probe({"a.b": None}, {("a.f", "x"): True}, used_network=True)


GONE = "AttributeError: module 'demo' has no attribute 'Old'"


@pytest.mark.parametrize(
    ("should_exist", "probe", "passed", "detail"),
    [
        (True, ve.Probe({"demo.Old": None}), True, "demo.Old exists"),
        (
            True,
            ve.Probe({"demo.Old": GONE}),
            False,
            f"demo.Old does not exist on 1.0, where example.before runs ({GONE})",
        ),
        (False, ve.Probe({"demo.Old": GONE}), True, "demo.Old is gone"),
        (
            False,
            ve.Probe({"demo.Old": None}),
            False,
            "demo.Old still exists on 1.0, where example.before runs",
        ),
        (
            False,
            ve.Probe({"demo.Old": GONE}, used_network=True),
            False,
            f"could not be checked: it failed after a network call was blocked ({GONE})",
        ),
        (True, ve.Probe(failure="did not finish in time"), False, "did not finish in time"),
        (True, ve.Probe(), False, "could not be checked: the probe has no result for it"),
    ],
)
def test_evaluate_path(should_exist: bool, probe: ve.Probe, passed: bool, detail: str) -> None:
    check = ve.PathCheck(
        Version("1.0"), "rename.from", "demo.Old", should_exist, "example.before runs"
    )
    result = ve.evaluate_path(check, probe)
    assert (result.check, result.passed, result.detail) == (check, passed, detail)


def path_plan(
    rename: dict[str, Any], available: dict[Version, ve.Release], breaks_in: str, works_since: str
) -> list[tuple[str, str, str, bool, str]]:
    checks = ve.plan_checks(available, Version(breaks_in), Version(works_since))
    return [
        (str(c.version), c.field, c.path, c.should_exist, c.where)
        for c in ve.plan_path_checks(rename, checks)
    ]


def test_rename_paths_are_checked_where_the_examples_need_them() -> None:
    rename = {"from": "demo.Old", "to": "demo.New"}
    assert path_plan(rename, releases("1.0", "1.5", "1.9", "2.0rc1", "2.0"), "2.0", "1.5") == [
        ("1.9", "rename.from", "demo.Old", True, "example.before runs"),
        ("1.9", "rename.to", "demo.New", True, "example.after runs"),
        # The pre-release just before breaks_in still runs example.before.
        ("2.0rc1", "rename.from", "demo.Old", True, "example.before runs"),
        ("2.0", "rename.from", "demo.Old", False, "example.before must fail"),
        ("2.0", "rename.to", "demo.New", True, "example.after runs"),
        ("1.5", "rename.to", "demo.New", True, "example.after runs"),
        # On 1.0, example.after must fail, so nothing needs a path there.
    ]


def test_callables_of_a_keyword_are_checked_where_an_example_runs() -> None:
    rename = {"keyword_of": ["demo.make", "demo.Maker.run"], "from": "old", "to": "new"}
    assert path_plan(rename, releases("1.0", "1.5", "1.9", "2.0"), "2.0", "1.5") == [
        ("1.9", "rename.keyword_of", "demo.make", True, "both examples run"),
        ("1.9", "rename.keyword_of", "demo.Maker.run", True, "both examples run"),
        ("2.0", "rename.keyword_of", "demo.make", True, "example.after runs"),
        ("2.0", "rename.keyword_of", "demo.Maker.run", True, "example.after runs"),
        ("1.5", "rename.keyword_of", "demo.make", True, "example.after runs"),
        ("1.5", "rename.keyword_of", "demo.Maker.run", True, "example.after runs"),
    ]


KEYWORD_ITEM = {"path": "demo.connect", "keyword": "timeout"}


def affects_plan(
    kind: str, affects: list[Any], restored_in: str | None = None
) -> list[tuple[str, str, str, bool, str, str]]:
    restored = Version(restored_in) if restored_in else None
    available = releases("1.9.0", "2.0.0", "2.1.0")
    checks = ve.plan_checks(available, Version("2.0.0"), None, restored_in=restored)
    return [
        (str(c.version), c.field, c.path, c.should_exist, c.where, c.hint)
        for c in ve.plan_affects_checks(kind, affects, checks)
    ]


RUNS = "example.before runs"
FAILS = "example.before must fail"


def test_paths_in_affects_of_a_removal() -> None:
    # The path alone is gone where example.before fails. With a keyword, it isn't.
    assert affects_plan("removal", ["demo.legacy", KEYWORD_ITEM]) == [
        ("1.9.0", "affects[0]", "demo.legacy", True, RUNS, ""),
        ("1.9.0", "affects[1]", "demo.connect", True, RUNS, ""),
        ("2.0.0", "affects[0]", "demo.legacy", False, FAILS, ""),
        ("2.0.0", "affects[1]", "demo.connect", True, FAILS, ve.REMOVAL_HINT),
    ]


@pytest.mark.parametrize("kind", ["signature", "behavior"])
def test_paths_in_affects_of_a_change_in_use(kind: str) -> None:
    # The code still uses every path where example.before fails, just in another way.
    assert affects_plan(kind, ["demo.legacy", KEYWORD_ITEM]) == [
        ("1.9.0", "affects[0]", "demo.legacy", True, RUNS, ""),
        ("1.9.0", "affects[1]", "demo.connect", True, RUNS, ""),
        ("2.0.0", "affects[0]", "demo.legacy", True, FAILS, ve.REMOVAL_HINT),
        ("2.0.0", "affects[1]", "demo.connect", True, FAILS, ve.REMOVAL_HINT),
    ]


def test_paths_in_affects_resolve_again_from_restored_in() -> None:
    assert affects_plan("removal", ["demo.legacy"], restored_in="2.1.0") == [
        ("1.9.0", "affects[0]", "demo.legacy", True, RUNS, ""),
        ("2.0.0", "affects[0]", "demo.legacy", False, FAILS, ""),
        ("2.1.0", "affects[0]", "demo.legacy", True, RUNS, ""),
    ]


def test_keywords_in_affects_are_explicit_parameters_where_before_runs() -> None:
    available = releases("1.9.0", "2.0.0", "2.1.0")
    checks = ve.plan_checks(available, Version("2.0.0"), None, restored_in=Version("2.1.0"))
    signatures = ve.plan_affects_signatures(["demo.legacy", KEYWORD_ITEM], checks)
    assert [(str(s.version), s.target, s.keyword, s.field) for s in signatures] == [
        ("1.9.0", "demo.connect", "timeout", "affects[1]"),
        ("2.1.0", "demo.connect", "timeout", "affects[1]"),
    ]


def test_a_path_that_must_still_exist_says_that_the_change_may_be_a_removal() -> None:
    check = ve.PathCheck(Version("2.0"), "affects[0]", "demo.Old", True, FAILS, ve.REMOVAL_HINT)
    result = ve.evaluate_path(check, ve.Probe({"demo.Old": GONE}))
    assert not result.passed
    assert result.detail == (
        f"demo.Old does not exist on 2.0, where example.before must fail ({GONE}). "
        "If it was removed, the type may be removal."
    )


def test_a_keyword_in_affects_without_an_explicit_parameter_names_the_item() -> None:
    check = ve.SignatureCheck(Version("2.0.0"), "demo.connect", "timeout", "affects[1]")
    probe = ve.Probe(signatures={("demo.connect", "timeout"): False})
    assert ve.evaluate_signature(check, probe).detail == (
        "has no explicit parameter timeout, and taking it through **kwargs does not count. "
        "Check the versions, or change affects[1]."
    )


@pytest.mark.parametrize(
    ("notice", "before", "after"),
    [
        pytest.param(
            {"type": "rename", "rename": {"from": "a.Old", "to": "a.New"}, "example": {}},
            ve.Failure(named="Old"),
            ve.Failure(named="New"),
            id="a rename",
        ),
        pytest.param(
            {
                "type": "rename",
                "rename": {"keyword_of": ["a.f"], "from": "old", "to": "new"},
                "example": {},
            },
            ve.Failure(named="old"),
            ve.Failure(named="new"),
            id="a renamed keyword",
        ),
        pytest.param(
            {
                "type": "rename",
                "rename": {"from": "a.Old", "to": "a.New"},
                "example": {"fails_with": {"before": {"error": "ImportError"}}},
            },
            ve.Failure(named="Old", error="ImportError"),
            ve.Failure(named="New"),
            id="a rename with fails_with",
        ),
        pytest.param(
            {
                "type": "removal",
                "example": {
                    "fails_with": {
                        "before": {"error": "AttributeError", "message": "legacy"},
                        "after": {"error": "AttributeError"},
                    }
                },
            },
            ve.Failure(error="AttributeError", message="legacy"),
            ve.Failure(error="AttributeError"),
            id="a removal",
        ),
    ],
)
def test_expected_failures(notice: dict[str, Any], before: ve.Failure, after: ve.Failure) -> None:
    assert ve.expected_failures(notice) == {"before": before, "after": after}


WITHDRAWN = 'withdrawn:\n  date: "2026-10-25"\n  reason: It says the same as another notice.\n'
WITHDRAWN_DETAIL = "withdrawn on 2026-10-25: It says the same as another notice. Not verified."


def write_withdrawn_notice(tmp_path: Path) -> Path:
    notice = write_notice(tmp_path)
    text = notice.read_text(encoding="utf-8").replace("references:", WITHDRAWN + "references:")
    notice.write_text(text, encoding="utf-8")
    return notice


def test_a_withdrawn_notice_is_not_verified(tmp_path: Path) -> None:
    def fetch(name: str) -> dict[Version, ve.Release]:
        raise AssertionError("a withdrawn notice doesn't need PyPI")

    results = ve.verify_notice(
        write_withdrawn_notice(tmp_path), sys.executable, fetch=fetch, python_version=PYTHON_VERSION
    )
    withdrawn = ve.Withdrawn("2026-10-25", "It says the same as another notice.")
    assert results == [ve.Result(withdrawn, True, WITHDRAWN_DETAIL)]


def test_the_report_shows_a_withdrawn_notice_on_a_line_of_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notice = write_withdrawn_notice(tmp_path)
    assert ve.report([notice], sys.executable, tmp_path, in_github=False) == 0
    assert capsys.readouterr().out == f"\n{notice.name}\n  skip  {WITHDRAWN_DETAIL}\n"


# --- A whole notice, offline --------------------------------------------------

FAKE_VERSIONS = {
    "1.0.0": "class Old: pass\n",
    "1.5.0": "class Old: pass\nclass New: pass\n",
    "1.9.0": "class Old: pass\nclass New: pass\n",
    "2.0.0": "class New: pass\n",
}

FAKE_NOTICE = """\
---
schema_version: "0.3"
id: BW-2026-9001
status: draft
published: "2026-10-21"
package:
  ecosystem: PyPI
  name: fakelib
type: rename
breaks_in: "{breaks_in}"
works_since: "{works_since}"
summary: Old was renamed to New, and the old name was removed in 2.0.0.
rename:
  from: fakelib.Old
  to: fakelib.New
example:
  before: |
    from fakelib import Old

    Old()
  after: |
    from fakelib import New

    New()
references:
  - https://example.com/changelog
"""


class FakeIndex:
    """Installs a tiny package called fakelib whose contents depend on the version."""

    def __init__(
        self, versions: dict[str, str] = FAKE_VERSIONS, requires: dict[str, str] | None = None
    ) -> None:
        self.versions = versions
        # The Requires-Python of some versions, as PyPI states it for their files.
        self.requires = requires or {}
        self.installed: list[tuple[str, str, str]] = []

    def releases(self, name: str) -> dict[Version, ve.Release]:
        assert name == "fakelib"
        return {
            Version(v): ve.Release(UPLOADED, frozenset({self.requires.get(v)}))
            for v in self.versions
        }

    def install(
        self, package: str, version: Version, cutoff: str, python: str, target: Path
    ) -> Path:
        self.installed.append((package, str(version), cutoff))
        subprocess.run([python, "-m", "venv", "--without-pip", str(target)], check=True)
        site = target / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        (site / "site-packages" / "fakelib.py").write_text(self.versions[str(version)])
        return target / "bin" / "python"


def write_notice(tmp_path: Path, breaks_in: str = "2.0.0", works_since: str = "1.5.0") -> Path:
    notice = tmp_path / "BW-2026-9001.yaml"
    text = FAKE_NOTICE.format(breaks_in=breaks_in, works_since=works_since)
    notice.write_text(text, encoding="utf-8")
    return notice


def verify(notice: Path, index: FakeIndex) -> list[str]:
    results = ve.verify_notice(
        notice,
        sys.executable,
        fetch=index.releases,
        installer=index.install,
        python_version=PYTHON_VERSION,
    )
    return [line(result) for result in results]


def line(result: ve.Result) -> str:
    """A result as one line, like the report but shorter."""
    if isinstance(result.check, ve.Withdrawn):
        return f"skip {result.detail}"
    return f"{status(result)} {result.check.version} {result.check.label}: {result.detail}"


def status(result: ve.Result) -> str:
    if isinstance(result.check, ve.LatestNote):
        return "note"
    return "ok" if result.passed else "FAIL"


def version(result: ve.Result) -> Version:
    """The version of a result's check. A withdrawn notice has no checks with versions."""
    assert not isinstance(result.check, ve.Withdrawn)
    return result.check.version


@posix_only
def test_a_correct_notice_passes_every_check(tmp_path: Path) -> None:
    index = FakeIndex()
    site = f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    assert verify(write_notice(tmp_path), index) == [
        "ok 1.9.0 example.before: runs",
        "ok 1.9.0 example.after: runs",
        (
            "ok 2.0.0 example.before: fails as expected: ImportError: cannot import name 'Old' "
            f"from 'fakelib' (<tmp>/venv-2.0.0/{site}/fakelib.py)"
        ),
        "ok 2.0.0 example.after: runs",
        "ok 1.5.0 example.after: runs",
        (
            "ok 1.0.0 example.after: fails as expected: ImportError: cannot import name 'New' "
            f"from 'fakelib' (<tmp>/venv-1.0.0/{site}/fakelib.py)"
        ),
        "ok 1.9.0 rename.from: fakelib.Old exists",
        "ok 1.9.0 rename.to: fakelib.New exists",
        "ok 2.0.0 rename.from: fakelib.Old is gone",
        "ok 2.0.0 rename.to: fakelib.New exists",
        "ok 1.5.0 rename.to: fakelib.New exists",
    ]
    # One environment per version, each with dependencies as of a day after the upload.
    # The paths are checked in the same environments, so nothing more is installed.
    assert index.installed == [
        ("fakelib", "1.9.0", "2026-01-02T12:00:00Z"),
        ("fakelib", "2.0.0", "2026-01-02T12:00:00Z"),
        ("fakelib", "1.5.0", "2026-01-02T12:00:00Z"),
        ("fakelib", "1.0.0", "2026-01-02T12:00:00Z"),
    ]


@posix_only
@pytest.mark.parametrize(
    ("breaks_in", "works_since", "expected"),
    [
        (
            "1.9.0",
            "1.5.0",
            [
                "FAIL 1.9.0 example.before: should fail on this version, but it ran",
                (
                    "FAIL 1.9.0 rename.from: fakelib.Old still exists on 1.9.0, "
                    "where example.before must fail"
                ),
            ],
        ),
        (
            "2.0.0",
            "1.0.0",
            [
                "FAIL 1.0.0 example.after: should run, but failed: ImportError",
                (
                    "FAIL 1.0.0 rename.to: fakelib.New does not exist on 1.0.0, "
                    "where example.after runs "
                    "(AttributeError: module 'fakelib' has no attribute 'New')"
                ),
            ],
        ),
        (
            "2.0.0",
            "1.9.0",
            ["FAIL 1.5.0 example.after: should fail on this version, but it ran"],
        ),
    ],
)
def test_a_notice_with_wrong_versions_fails(
    tmp_path: Path, breaks_in: str, works_since: str, expected: list[str]
) -> None:
    lines = verify(write_notice(tmp_path, breaks_in, works_since), FakeIndex())
    failures = [line for line in lines if line.startswith("FAIL")]
    assert len(failures) == len(expected)
    for failure, start in zip(failures, expected, strict=True):
        assert failure.startswith(start)


@posix_only
def test_breaks_in_must_be_the_first_release_that_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pre-release already removed Old, so 2.0.0 is not the first release that breaks.
    monkeypatch.setitem(FAKE_VERSIONS, "2.0.0b1", "class New: pass\n")
    lines = verify(write_notice(tmp_path), FakeIndex())
    failures = [line for line in lines if line.startswith("FAIL")]
    assert len(failures) == 2
    assert failures[0].startswith(
        "FAIL 2.0.0b1 example.before: should run, but failed: ImportError: cannot import name 'Old'"
    )
    assert failures[1] == (
        "FAIL 2.0.0b1 rename.from: fakelib.Old does not exist on 2.0.0b1, where example.before "
        "runs (AttributeError: module 'fakelib' has no attribute 'Old')"
    )


@posix_only
def test_a_rename_without_works_since_checks_rename_to_on_breaks_in(tmp_path: Path) -> None:
    notice = write_notice(tmp_path)
    text = notice.read_text(encoding="utf-8").replace('works_since: "1.5.0"\n', "")
    notice.write_text(text, encoding="utf-8")
    lines = verify(notice, FakeIndex())
    assert [line.split(":")[0] for line in lines] == [
        "ok 1.9.0 example.before",
        "ok 2.0.0 example.before",
        "ok 2.0.0 example.after",
        "ok 1.9.0 rename.from",
        "ok 2.0.0 rename.from",
        "ok 2.0.0 rename.to",
    ]


@posix_only
@pytest.mark.parametrize(
    ("field", "path", "expected"),
    [
        (
            "from",
            "fakelib.sub.Old",
            [
                (
                    "FAIL 1.9.0 rename.from: fakelib.sub.Old does not exist on 1.9.0, where "
                    "example.before runs (AttributeError: module 'fakelib' has no attribute 'sub')"
                )
            ],
        ),
        (
            "to",
            "fakelib.sub.New",
            [
                f"FAIL {version} rename.to: fakelib.sub.New does not exist on {version}, where "
                "example.after runs (AttributeError: module 'fakelib' has no attribute 'sub')"
                for version in ("1.9.0", "2.0.0", "1.5.0")
            ],
        ),
    ],
)
def test_a_rename_path_in_a_module_that_does_not_exist_fails(
    tmp_path: Path, field: str, path: str, expected: list[str]
) -> None:
    # The examples import the right names, so only the path in the notice is wrong.
    notice = write_notice(tmp_path)
    old = f"  {field}: fakelib.{path.rpartition('.')[2]}\n"
    text = notice.read_text(encoding="utf-8").replace(old, f"  {field}: {path}\n")
    notice.write_text(text, encoding="utf-8")
    lines = verify(notice, FakeIndex())
    assert [line for line in lines if line.startswith("FAIL")] == expected


@posix_only
def test_an_old_name_that_is_only_a_stub_on_breaks_in_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # example.before fails on 2.0.0 with an error that names Old, but Old still resolves.
    stub = (
        "class New: pass\n"
        "class Old:\n"
        "    def __init__(self):\n"
        "        raise ImportError('Old was removed')\n"
    )
    monkeypatch.setitem(FAKE_VERSIONS, "2.0.0", stub)
    lines = verify(write_notice(tmp_path), FakeIndex())
    assert [line for line in lines if line.startswith("FAIL")] == [
        "FAIL 2.0.0 rename.from: fakelib.Old still exists on 2.0.0, where example.before must fail"
    ]


LATEST = (
    "This is the latest release, so the library may have changed after breaks_in: "
    "it may have brought the old name back or broken the new code."
)


@posix_only
def test_the_latest_release_after_breaks_in_is_checked_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VERSIONS, "3.0.0", "class New: pass\n")
    index = FakeIndex()
    lines = verify(write_notice(tmp_path), index)
    assert all(line.startswith("ok") for line in lines)
    latest = [line.split(": ")[0] for line in lines if " 3.0.0 " in line]
    assert latest == [
        "ok 3.0.0 example.before",
        "ok 3.0.0 example.after",
        "ok 3.0.0 rename.from",
        "ok 3.0.0 rename.to",
    ]
    assert lines[-2:] == [
        "ok 3.0.0 rename.from: fakelib.Old is gone",
        "ok 3.0.0 rename.to: fakelib.New exists",
    ]
    assert index.installed[-1] == ("fakelib", "3.0.0", "2026-01-02T12:00:00Z")


@posix_only
@pytest.mark.parametrize(
    ("code", "failures"),
    [
        pytest.param(
            "class Old: pass\nclass New: pass\n",
            [
                f"FAIL 3.0.0 example.before: should fail on this version, but it ran. {LATEST}",
                (
                    "FAIL 3.0.0 rename.from: fakelib.Old still exists on 3.0.0, where "
                    f"example.before must fail. {LATEST}"
                ),
            ],
            id="the old name came back",
        ),
        pytest.param(
            "class Newer: pass\n",
            [
                "FAIL 3.0.0 example.after: should run, but failed: ImportError: cannot import name",
                (
                    "FAIL 3.0.0 rename.to: fakelib.New does not exist on 3.0.0, where "
                    "example.after runs (AttributeError: module 'fakelib' has no attribute "
                    f"'New'). {LATEST}"
                ),
            ],
            id="the new code broke",
        ),
    ],
)
def test_a_change_on_the_latest_release_fails_and_says_what_it_may_mean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, failures: list[str]
) -> None:
    monkeypatch.setitem(FAKE_VERSIONS, "3.0.0", code)
    lines = verify(write_notice(tmp_path), FakeIndex())
    found = [line for line in lines if line.startswith("FAIL")]
    assert len(found) == len(failures)
    for line, start in zip(found, failures, strict=True):
        assert line.startswith(start)
        assert line.endswith(LATEST)


@posix_only
def test_a_newer_release_for_another_python_is_passed_over_with_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VERSIONS, "3.0.0", "class New: pass\n")
    monkeypatch.setitem(FAKE_VERSIONS, "3.1.0", "class New: pass\n")
    index = FakeIndex(requires={"3.1.0": ">=99"})
    # Without python_version, the verifier asks examples_python() before it plans.
    asked: list[str] = []

    def examples_python(python: str) -> Version:
        asked.append(python)
        return PYTHON_VERSION

    monkeypatch.setattr(ve, "examples_python", examples_python)
    results = ve.verify_notice(
        write_notice(tmp_path), sys.executable, fetch=index.releases, installer=index.install
    )
    assert asked == [sys.executable]
    assert all(result.passed for result in results)
    assert {str(version(r)) for r in results if version(r) > Version("2.0.0")} == {"3.0.0", "3.1.0"}
    assert line(results[-1]) == (
        "note 3.1.0 latest release: "
        "3.1.0 requires Python >=99, so the latest release checked is 3.0.0"
    )
    assert "3.1.0" not in [installed for _, installed, _ in index.installed]


@posix_only
def test_no_latest_release_for_the_python_is_noted_and_does_not_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VERSIONS, "3.0.0", "class Old: pass\nclass New: pass\n")
    lines = verify(write_notice(tmp_path), FakeIndex(requires={"3.0.0": ">=99"}))
    assert not [line for line in lines if line.startswith("FAIL")]
    assert lines[-1] == (
        f"note 3.0.0 latest release: not checked: no release after 2.0.0 accepts Python "
        f"{PYTHON_VERSION}; 3.0.0 requires Python >=99"
    )
    assert not [line for line in lines[:-1] if " 3.0.0 " in line]


@posix_only
def test_after_must_fail_because_of_the_new_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VERSIONS, "1.0.0", "raise RuntimeError('broken')\n")
    lines = verify(write_notice(tmp_path), FakeIndex())
    assert [line for line in lines if line.startswith("FAIL")] == [
        "FAIL 1.0.0 example.after: fails, but not because of New: RuntimeError: broken"
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("ecosystem: PyPI", "ecosystem: npm"), "line 7: package.ecosystem: 'npm' is not one of"),
        (('breaks_in: "2.0.0"', 'breaks_in: "two"'), "line 10: breaks_in: 'two' is not valid"),
        (('published: "2026-10-21"', "published: 2026-02-30"), "line 5: Write 2026-02-30 in"),
        (("example:", "examples:"), "line 2: 'example' is a required property"),
    ],
)
def test_only_valid_notices_are_verified(
    tmp_path: Path, change: tuple[str, str], message: str
) -> None:
    notice = write_notice(tmp_path)
    notice.write_text(notice.read_text(encoding="utf-8").replace(*change), encoding="utf-8")
    with pytest.raises(ve.VerifyError) as raised:
        ve.verify_notice(notice, sys.executable, fetch=FakeIndex().releases)
    assert str(raised.value).startswith(f"The notice is not valid ({message}")
    assert str(raised.value).endswith(". Run tools/validate_notices.py.")


FAILS_WITH_IMPORT_ERROR = (
    "    before:\n      error: ImportError\n    after:\n      error: ImportError\n"
)


def write_removal_notice(tmp_path: Path, fails_with: str = FAILS_WITH_IMPORT_ERROR) -> Path:
    """The fake rename, written as a removal of fakelib.Old with affects and fails_with."""
    notice = write_notice(tmp_path)
    text = notice.read_text(encoding="utf-8").replace("type: rename", "type: removal")
    text = text.replace(
        "rename:\n  from: fakelib.Old\n  to: fakelib.New\n",
        "instructions: Use New instead.\naffects:\n  - fakelib.Old\n",
    )
    text = text.replace("    New()\n", f"    New()\n  fails_with:\n{fails_with}")
    notice.write_text(text, encoding="utf-8")
    return notice


@posix_only
def test_a_correct_removal_passes_every_check(tmp_path: Path) -> None:
    lines = verify(write_removal_notice(tmp_path), FakeIndex())
    assert [line.split(": ")[0] for line in lines] == [
        "ok 1.9.0 example.before",
        "ok 1.9.0 example.after",
        "ok 2.0.0 example.before",
        "ok 2.0.0 example.after",
        "ok 1.5.0 example.after",
        "ok 1.0.0 example.after",
        "ok 1.9.0 affects[0]",
        "ok 2.0.0 affects[0]",
    ]
    assert lines[-2:] == [
        "ok 1.9.0 affects[0]: fakelib.Old exists",
        "ok 2.0.0 affects[0]: fakelib.Old is gone",
    ]


@posix_only
@pytest.mark.parametrize(
    ("fails_with", "failure"),
    [
        pytest.param(
            "    before:\n      error: TypeError\n    after:\n      error: ImportError\n",
            "FAIL 2.0.0 example.before: fails, but with ImportError instead of TypeError: "
            "ImportError: cannot import name 'Old'",
            id="before with another exception",
        ),
        pytest.param(
            "    before:\n      error: ImportError\n      message: cannot import name 'Older'\n"
            "    after:\n      error: ImportError\n",
            "FAIL 2.0.0 example.before: fails with ImportError, but its message doesn't contain "
            "\"cannot import name 'Older'\": ImportError: cannot import name 'Old'",
            id="before with another message",
        ),
        pytest.param(
            "    before:\n      error: ImportError\n    after:\n      error: NameError\n",
            "FAIL 1.0.0 example.after: fails, but with ImportError instead of NameError: "
            "ImportError: cannot import name 'New'",
            id="after with another exception",
        ),
    ],
)
def test_the_failure_of_a_removal_must_match_fails_with(
    tmp_path: Path, fails_with: str, failure: str
) -> None:
    lines = verify(write_removal_notice(tmp_path, fails_with), FakeIndex())
    found = [line for line in lines if line.startswith("FAIL")]
    assert len(found) == 1
    assert found[0].startswith(failure)


@posix_only
def test_a_path_alone_of_a_removal_must_be_gone_where_before_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Old is still there, but the example fails for the reason its fails_with gives.
    monkeypatch.setitem(FAKE_VERSIONS, "2.0.0", "class Old:\n    def __init__(self, x): pass\n")
    fails_with = "    before:\n      error: TypeError\n    after:\n      error: ImportError\n"
    lines = verify(write_removal_notice(tmp_path, fails_with), FakeIndex())
    assert [line for line in lines if line.startswith("FAIL") and "affects" in line] == [
        "FAIL 2.0.0 affects[0]: fakelib.Old still exists on 2.0.0, where example.before must fail"
    ]


def write_signature_notice(tmp_path: Path) -> Path:
    """The fake keyword rename, written as a signature change with a keyword in affects."""
    notice = write_keyword_notice(tmp_path)
    text = notice.read_text(encoding="utf-8").replace("type: rename", "type: signature")
    text = text.replace(
        "rename:\n  keyword_of:\n    - fakelib.make\n  from: old\n  to: new\n",
        "instructions: Pass new instead of old.\n"
        "affects:\n  - path: fakelib.make\n    keyword: old\n",
    )
    text = text.replace(
        "    make(new=1)\n",
        "    make(new=1)\n  fails_with:\n"
        "    before:\n      error: TypeError\n      message: argument 'old'\n"
        "    after:\n      error: TypeError\n      message: argument 'new'\n",
    )
    notice.write_text(text, encoding="utf-8")
    return notice


@posix_only
def test_a_correct_signature_change_with_a_keyword_in_affects_passes(tmp_path: Path) -> None:
    lines = verify(write_signature_notice(tmp_path), FakeIndex(KEYWORD_VERSIONS))
    assert all(line.startswith("ok") for line in lines)
    assert lines[6:] == [
        "ok 1.9.0 affects[0]: fakelib.make exists",
        # The callable is still there where example.before fails: only how it is called changed.
        "ok 2.0.0 affects[0]: fakelib.make exists",
        "ok 1.9.0 signature of fakelib.make: takes old as an explicit parameter",
    ]


@posix_only
@pytest.mark.parametrize(
    ("version", "code", "failure"),
    [
        pytest.param(
            "2.0.0",
            "def other(): pass\n",
            "FAIL 2.0.0 affects[0]: fakelib.make does not exist on 2.0.0, where example.before "
            "must fail (AttributeError: module 'fakelib' has no attribute 'make'). "
            "If it was removed, the type may be removal.",
            id="the callable is gone",
        ),
        pytest.param(
            "1.9.0",
            "def make(**kwargs): pass\n",
            "FAIL 1.9.0 signature of fakelib.make: has no explicit parameter old, and taking it "
            "through **kwargs does not count. Check the versions, or change affects[0].",
            id="the keyword only through **kwargs",
        ),
    ],
)
def test_a_keyword_in_affects_is_checked_on_the_real_releases(
    tmp_path: Path, version: str, code: str, failure: str
) -> None:
    index = FakeIndex({**KEYWORD_VERSIONS, version: code})
    lines = verify(write_signature_notice(tmp_path), index)
    assert failure in lines


def add_restored_in(notice: Path, version: str) -> Path:
    text = notice.read_text(encoding="utf-8")
    text = text.replace('breaks_in: "2.0.0"\n', f'breaks_in: "2.0.0"\nrestored_in: "{version}"\n')
    notice.write_text(text, encoding="utf-8")
    return notice


@posix_only
def test_restored_in_brings_the_old_code_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VERSIONS, "2.1.0", "class New: pass\n")
    monkeypatch.setitem(FAKE_VERSIONS, "2.2.0", "class Old: pass\nclass New: pass\n")
    lines = verify(add_restored_in(write_notice(tmp_path), "2.2.0"), FakeIndex())
    assert all(line.startswith("ok") for line in lines)
    # 2.2.0 is also the latest release, so it isn't checked twice.
    assert [line.split(": ")[0] for line in lines if " 2.1.0 " in line or " 2.2.0 " in line] == [
        "ok 2.1.0 example.before",
        "ok 2.2.0 example.before",
        "ok 2.2.0 example.after",
        "ok 2.1.0 rename.from",
        "ok 2.2.0 rename.from",
        "ok 2.2.0 rename.to",
    ]
    assert "ok 2.2.0 rename.from: fakelib.Old exists" in lines


@posix_only
@pytest.mark.parametrize(
    ("latest", "failures"),
    [
        pytest.param("class Old: pass\nclass New: pass\n", [], id="the old code still works"),
        pytest.param(
            "class New: pass\n",
            [
                "FAIL 3.0.0 example.before: should run, but failed: ImportError",
                "FAIL 3.0.0 rename.from: fakelib.Old does not exist on 3.0.0, where",
            ],
            id="the old code broke again",
        ),
    ],
)
def test_after_restored_in_the_old_code_must_run_on_the_latest_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, latest: str, failures: list[str]
) -> None:
    monkeypatch.setitem(FAKE_VERSIONS, "2.1.0", "class Old: pass\nclass New: pass\n")
    monkeypatch.setitem(FAKE_VERSIONS, "3.0.0", latest)
    lines = verify(add_restored_in(write_notice(tmp_path), "2.1.0"), FakeIndex())
    assert "ok 3.0.0 example.before: runs" in lines or failures
    found = [line for line in lines if line.startswith("FAIL")]
    assert len(found) == len(failures)
    for line, start in zip(found, failures, strict=True):
        assert line.startswith(start)
        assert line.endswith(LATEST)


# --- A renamed keyword argument, offline --------------------------------------

KEYWORD_VERSIONS = {
    "1.0.0": "def make(*, old=None): pass\n",
    "1.5.0": "def make(*, old=None, new=None): pass\n",
    "1.9.0": "def make(*, old=None, new=None): pass\n",
    "2.0.0": "def make(*, new=None): pass\n",
}

KEYWORD_NOTICE = """\
---
schema_version: "0.3"
id: BW-2026-9002
status: draft
published: "2026-10-23"
package:
  ecosystem: PyPI
  name: fakelib
type: rename
breaks_in: "2.0.0"
{works_since}summary: The old argument of make was renamed to new, and old was removed in 2.0.0.
rename:
  keyword_of:
    - fakelib.make
  from: old
  to: new
example:
  before: |
    from fakelib import make

    make(old=1)
  after: |
    from fakelib import make

    make(new=1)
references:
  - https://example.com/changelog
"""


def write_keyword_notice(tmp_path: Path, works_since: str | None = "1.5.0") -> Path:
    notice = tmp_path / "BW-2026-9002.yaml"
    line = "" if works_since is None else f'works_since: "{works_since}"\n'
    notice.write_text(KEYWORD_NOTICE.format(works_since=line), encoding="utf-8")
    return notice


@posix_only
def test_a_correct_keyword_rename_passes_every_check(tmp_path: Path) -> None:
    index = FakeIndex(KEYWORD_VERSIONS)
    lines = verify(write_keyword_notice(tmp_path), index)
    assert lines[:2] == ["ok 1.9.0 example.before: runs", "ok 1.9.0 example.after: runs"]
    # Newer Pythons may add a suggestion such as "Did you mean 'new'?" to the error.
    assert lines[2].startswith(
        "ok 2.0.0 example.before: fails as expected: "
        "TypeError: make() got an unexpected keyword argument 'old'"
    )
    assert lines[3:5] == ["ok 2.0.0 example.after: runs", "ok 1.5.0 example.after: runs"]
    assert lines[5].startswith(
        "ok 1.0.0 example.after: fails as expected: "
        "TypeError: make() got an unexpected keyword argument 'new'"
    )
    assert lines[6:] == [
        "ok 1.9.0 rename.keyword_of: fakelib.make exists",
        "ok 2.0.0 rename.keyword_of: fakelib.make exists",
        "ok 1.5.0 rename.keyword_of: fakelib.make exists",
        "ok 2.0.0 signature of fakelib.make: takes new as an explicit parameter",
        "ok 1.5.0 signature of fakelib.make: takes new as an explicit parameter",
    ]
    # The paths and signatures are checked in the environments the examples ran in.
    assert [installed for _, installed, _ in index.installed] == [
        "1.9.0",
        "2.0.0",
        "1.5.0",
        "1.0.0",
    ]


@posix_only
def test_without_works_since_only_breaks_in_has_its_signature_checked(tmp_path: Path) -> None:
    lines = verify(write_keyword_notice(tmp_path, works_since=None), FakeIndex(KEYWORD_VERSIONS))
    assert [line.split(":")[0] for line in lines] == [
        "ok 1.9.0 example.before",
        "ok 2.0.0 example.before",
        "ok 2.0.0 example.after",
        "ok 1.9.0 rename.keyword_of",
        "ok 2.0.0 rename.keyword_of",
        "ok 2.0.0 signature of fakelib.make",
    ]


@posix_only
def test_a_keyword_taken_only_through_kwargs_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # example.after runs on 1.5.0, but make ignores the new argument there.
    monkeypatch.setitem(KEYWORD_VERSIONS, "1.5.0", "def make(*, old=None, **kwargs): pass\n")
    lines = verify(write_keyword_notice(tmp_path), FakeIndex(KEYWORD_VERSIONS))
    assert [line for line in lines if line.startswith("FAIL")] == [
        (
            "FAIL 1.5.0 signature of fakelib.make: has no explicit parameter new, and taking it "
            "through **kwargs does not count. Check the versions, or remove fakelib.make from "
            "rename.keyword_of."
        )
    ]


@posix_only
def test_an_explicit_keyword_next_to_kwargs_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        KEYWORD_VERSIONS, "1.5.0", "def make(*, old=None, new=None, **kwargs): pass\n"
    )
    lines = verify(write_keyword_notice(tmp_path), FakeIndex(KEYWORD_VERSIONS))
    assert all(line.startswith("ok") for line in lines)
    assert len(lines) == 11


@posix_only
def test_every_callable_in_keyword_of_is_checked(tmp_path: Path) -> None:
    notice = write_keyword_notice(tmp_path)
    text = notice.read_text(encoding="utf-8")
    text = text.replace("    - fakelib.make\n", "    - fakelib.make\n    - fakelib.build\n")
    notice.write_text(text, encoding="utf-8")
    lines = verify(notice, FakeIndex(KEYWORD_VERSIONS))
    missing = "(AttributeError: module 'fakelib' has no attribute 'build')"
    assert [line for line in lines if "rename.keyword_of" in line] == [
        "ok 1.9.0 rename.keyword_of: fakelib.make exists",
        (
            f"FAIL 1.9.0 rename.keyword_of: fakelib.build does not exist on 1.9.0, where both "
            f"examples run {missing}"
        ),
        "ok 2.0.0 rename.keyword_of: fakelib.make exists",
        (
            f"FAIL 2.0.0 rename.keyword_of: fakelib.build does not exist on 2.0.0, where "
            f"example.after runs {missing}"
        ),
        "ok 1.5.0 rename.keyword_of: fakelib.make exists",
        (
            f"FAIL 1.5.0 rename.keyword_of: fakelib.build does not exist on 1.5.0, where "
            f"example.after runs {missing}"
        ),
    ]
    assert [line for line in lines if "signature" in line] == [
        "ok 2.0.0 signature of fakelib.make: takes new as an explicit parameter",
        (
            "FAIL 2.0.0 signature of fakelib.build: could not be checked: "
            "AttributeError: module 'fakelib' has no attribute 'build'"
        ),
        "ok 1.5.0 signature of fakelib.make: takes new as an explicit parameter",
        (
            "FAIL 1.5.0 signature of fakelib.build: could not be checked: "
            "AttributeError: module 'fakelib' has no attribute 'build'"
        ),
    ]


@posix_only
def test_before_must_fail_because_of_the_old_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = "def make(*, new=None, **kwargs):\n    if kwargs:\n        raise ValueError('no')\n"
    monkeypatch.setitem(KEYWORD_VERSIONS, "2.0.0", broken)
    lines = verify(write_keyword_notice(tmp_path), FakeIndex(KEYWORD_VERSIONS))
    assert [line for line in lines if line.startswith("FAIL")] == [
        "FAIL 2.0.0 example.before: fails, but not because of old: ValueError: no"
    ]


@posix_only
@pytest.mark.parametrize(
    ("code", "signature"),
    [
        pytest.param(
            "def make(*, new=None): pass\n",
            "ok 3.0.0 signature of fakelib.make: takes new as an explicit parameter",
            id="new is still explicit",
        ),
        pytest.param(
            "def make(**kwargs): pass\n",
            "FAIL 3.0.0 signature of fakelib.make: has no explicit parameter new, and taking it "
            "through **kwargs does not count. Check the versions, or remove fakelib.make from "
            f"rename.keyword_of. {LATEST}",
            id="new went into **kwargs",
        ),
    ],
)
def test_a_renamed_keyword_is_checked_on_the_latest_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, signature: str
) -> None:
    monkeypatch.setitem(KEYWORD_VERSIONS, "3.0.0", code)
    lines = verify(write_keyword_notice(tmp_path), FakeIndex(KEYWORD_VERSIONS))
    assert "ok 3.0.0 rename.keyword_of: fakelib.make exists" in lines
    assert lines[-1] == signature


@posix_only
@pytest.mark.parametrize(
    ("write", "versions", "asked"),
    [
        pytest.param(
            write_notice,
            FAKE_VERSIONS,
            {
                "1.9.0": (["fakelib.Old", "fakelib.New"], []),
                "2.0.0": (["fakelib.Old", "fakelib.New"], []),
                "1.5.0": (["fakelib.New"], []),
            },
            id="a renamed name",
        ),
        pytest.param(
            write_keyword_notice,
            KEYWORD_VERSIONS,
            {
                "1.9.0": (["fakelib.make"], []),
                "2.0.0": (["fakelib.make"], [("fakelib.make", "new")]),
                "1.5.0": (["fakelib.make"], [("fakelib.make", "new")]),
            },
            id="a renamed keyword",
        ),
    ],
)
def test_each_probe_asks_only_for_what_its_release_needs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: Callable[[Path], Path],
    versions: dict[str, str],
    asked: dict[str, tuple[list[str], list[tuple[str, str]]]],
) -> None:
    requests: dict[str, tuple[list[str], list[tuple[str, str]]]] = {}
    run_probe = ve.run_probe

    def recording(
        interpreter: Path,
        paths: Sequence[str],
        signatures: Sequence[tuple[str, str]],
        workdir: Path,
        timeout: float = ve.EXAMPLE_TIMEOUT,
    ) -> ve.Probe:
        # FakeIndex puts each release in <tmp>/venv-<version>/bin/python.
        version = interpreter.parent.parent.name.removeprefix("venv-")
        requests[version] = (list(paths), list(signatures))
        return run_probe(interpreter, paths, signatures, workdir, timeout)

    monkeypatch.setattr(ve, "run_probe", recording)
    lines = verify(write(tmp_path), FakeIndex(versions))
    assert all(line.startswith("ok") for line in lines)
    assert requests == asked


# --- Choosing notices ---------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return completed.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def commit(repo: Path, message: str, **files: str) -> None:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def test_changed_files_handles_unusual_file_names(git_repo: Path) -> None:
    commit(git_repo, "base", **{"README.md": "one\n", "old.txt": "old\n"})
    base = git(git_repo, "rev-parse", "HEAD")

    unusual = git_repo / "notices" / "pypi" / "démo lib" / "BW-2026-0002.yaml"
    unusual.parent.mkdir(parents=True)
    unusual.write_text("id: x\n", encoding="utf-8")
    (git_repo / "old.txt").unlink()
    commit(git_repo, "change", **{"README.md": "two\n"})

    assert sorted(ve.changed_files(git_repo, base)) == [
        "README.md",
        "notices/pypi/démo lib/BW-2026-0002.yaml",
    ]
    with pytest.raises(ve.VerifyError, match="git diff against no-such-ref failed"):
        ve.changed_files(git_repo, "no-such-ref")


def test_changed_files_ignores_changes_made_on_the_base_branch(git_repo: Path) -> None:
    commit(git_repo, "base", **{"a.txt": "a\n"})
    git(git_repo, "checkout", "-q", "-b", "feature")
    commit(git_repo, "feature", **{"feature.txt": "f\n"})
    git(git_repo, "checkout", "-q", "main")
    commit(git_repo, "main moved on", **{"main.txt": "m\n"})
    git(git_repo, "checkout", "-q", "feature")
    assert ve.changed_files(git_repo, "main") == ["feature.txt"]


def test_changed_files_needs_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ve.VerifyError, match="git is not installed"):
        ve.changed_files(REPO_ROOT, "HEAD")


def test_all_notices_skips_markdown_and_hidden_files(tmp_path: Path) -> None:
    for name in ("notices/pypi/a/BW-2026-0001.yaml", "notices/LICENSE.md", "notices/.x/B.yaml"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    assert ve.all_notices(tmp_path) == [tmp_path / "notices/pypi/a/BW-2026-0001.yaml"]


# Written out here, not read from the code, so that a file added to the list or removed
# from it needs a deliberate change to this test too.
TOOLING = {
    "tools/verify_examples.py",
    "tools/validate_notices.py",
    "requirements-dev.txt",
    ".github/workflows/checks.yml",
}


def test_all_notices_are_verified_when_the_tooling_changes() -> None:
    assert ve.RUN_ALL_WHEN_CHANGED == TOOLING
    every = ve.all_notices(REPO_ROOT)
    assert every
    for tool in sorted(TOOLING):
        assert (REPO_ROOT / tool).is_file(), f"{tool} is not in the repository"
        assert ve.notices_to_verify(REPO_ROOT, [tool, "README.md"]) == every


def test_only_changed_notices_are_verified_otherwise() -> None:
    notice = "notices/pypi/pydantic-ai/BW-2026-0001.yaml"
    assert ve.notices_to_verify(REPO_ROOT, [notice, "README.md"]) == [REPO_ROOT / notice]
    assert ve.notices_to_verify(REPO_ROOT, ["README.md", "notices/LICENSE.md"]) == []
    assert ve.notices_to_verify(REPO_ROOT, ["docs/notices/x.yaml"]) == []


# --- Command line -------------------------------------------------------------


@pytest.mark.parametrize("argv", [[], ["--all", "--changed-since", "origin/main"], ["x", "--all"]])
def test_main_needs_exactly_one_kind_of_target(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        ve.main(argv)
    assert raised.value.code == 2


def test_posix_is_detected() -> None:
    assert ve.POSIX is (os.name == "posix")
    if sys.platform in {"linux", "darwin"}:
        assert ve.POSIX


def test_main_refuses_to_run_outside_posix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ve, "POSIX", False)
    assert ve.main(["--all"]) == 1
    assert capsys.readouterr().out == "FAIL  tools/verify_examples.py runs on Linux and macOS.\n"


@posix_only
def test_main_reports_missing_files(capsys: pytest.CaptureFixture[str]) -> None:
    assert ve.main(["no/such/notice.yaml"]) == 1
    assert capsys.readouterr().out.startswith("FAIL  Not found: ")


@posix_only
def test_main_says_when_there_is_nothing_to_verify(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ve, "changed_files", lambda root, ref: ["README.md"])
    assert ve.main(["--changed-since", "origin/main"]) == 0
    assert capsys.readouterr().out == "No notices to verify.\n"


@posix_only
def test_main_with_all_verifies_every_notice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    verified: list[Path] = []

    def fine(path: Path, python: str) -> list[ve.Result]:
        verified.append(path)
        return []

    monkeypatch.setattr(ve, "verify_notice", fine)
    assert ve.main(["--all"]) == 0
    assert verified == ve.all_notices(REPO_ROOT)
    assert verified
    assert capsys.readouterr().out.endswith(f"({len(verified)} notice(s)).\n")


class ChangedSince:
    """``changed_files`` and ``verify_notice`` for a pull request that changed ``changed``."""

    def __init__(self, changed: list[str], passed: bool = True) -> None:
        self.changed = changed
        self.passed = passed
        self.refs: list[str] = []
        self.verified: list[Path] = []

    def changed_files(self, root: Path, ref: str) -> list[str]:
        assert root == REPO_ROOT
        self.refs.append(ref)
        return self.changed

    def verify_notice(self, path: Path, python: str) -> list[ve.Result]:
        self.verified.append(path)
        check = ve.Check(Version("2.0"), "before", should_run=False)
        return [ve.Result(check, self.passed, "fails as expected" if self.passed else "it ran")]


@posix_only
def test_main_verifies_the_notices_a_pull_request_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notice = "notices/pypi/pydantic-ai/BW-2026-0001.yaml"
    pull_request = ChangedSince([notice, "README.md"])
    monkeypatch.setattr(ve, "changed_files", pull_request.changed_files)
    monkeypatch.setattr(ve, "verify_notice", pull_request.verify_notice)
    assert ve.main(["--changed-since", "origin/main"]) == 0
    assert pull_request.refs == ["origin/main"]
    assert pull_request.verified == [REPO_ROOT / notice]
    assert capsys.readouterr().out.endswith(
        "\nAll examples behave as their notices say (1 notice(s)).\n"
    )


@posix_only
def test_main_verifies_every_notice_when_a_pull_request_changes_a_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pull_request = ChangedSince(["tools/validate_notices.py"])
    monkeypatch.setattr(ve, "changed_files", pull_request.changed_files)
    monkeypatch.setattr(ve, "verify_notice", pull_request.verify_notice)
    assert ve.main(["--changed-since", "origin/main"]) == 0
    every = ve.all_notices(REPO_ROOT)
    assert pull_request.verified == every
    assert len(every) >= 2
    assert capsys.readouterr().out.endswith(f"({len(every)} notice(s)).\n")


@posix_only
def test_main_fails_when_a_notice_a_pull_request_changes_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notice = "notices/pypi/pydantic-ai/BW-2026-0001.yaml"
    pull_request = ChangedSince([notice], passed=False)
    monkeypatch.setattr(ve, "changed_files", pull_request.changed_files)
    monkeypatch.setattr(ve, "verify_notice", pull_request.verify_notice)
    assert ve.main(["--changed-since", "origin/main"]) == 1
    assert pull_request.verified == [REPO_ROOT / notice]
    out = capsys.readouterr().out
    assert "  FAIL  2.0  example.before: it ran\n" in out
    assert out.endswith("\n1 check(s) failed.\n")


@posix_only
@pytest.mark.parametrize("in_github", [True, False])
def test_main_prints_results_and_annotations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], in_github: bool
) -> None:
    def fake_verify(path: Path, python: str) -> list[ve.Result]:
        return [
            ve.Result(ve.Check(Version("1.0"), "before", should_run=True), True, "runs"),
            ve.Result(ve.Check(Version("2.0"), "before", should_run=False), False, "it ran"),
            ve.Result(ve.SignatureCheck(Version("2.0"), "demo.make", "new"), False, "no new"),
            ve.Result(ve.LatestNote(Version("3.0")), True, "3.0 requires Python >=3.13"),
        ]

    monkeypatch.setattr(ve, "verify_notice", fake_verify)
    monkeypatch.setenv("GITHUB_ACTIONS", "true" if in_github else "false")
    # One notice, so that the counts don't depend on how many notices the registry holds.
    assert ve.main([str(REAL_NOTICE)]) == 1
    out = capsys.readouterr().out
    rel = REAL_NOTICE.relative_to(REPO_ROOT).as_posix()
    assert (
        f"\n{rel}\n  ok    1.0  example.before: runs\n  FAIL  2.0  example.before: it ran\n" in out
    )
    assert "\n  FAIL  2.0  signature of demo.make: no new\n" in out
    # A note is neither a pass nor a failure, so it gets no annotation.
    assert "\n  note  3.0  latest release: 3.0 requires Python >=3.13\n" in out
    assert (f"::error file={rel}::2.0  example.before: it ran\n" in out) is in_github
    assert (f"::error file={rel}::2.0  signature of demo.make: no new\n" in out) is in_github
    assert out.count("::error") == 2 * int(in_github)
    assert out.endswith("\n2 check(s) failed.\n")


@posix_only
@pytest.mark.parametrize("in_github", [True, False])
def test_main_counts_notices_that_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], in_github: bool
) -> None:
    def broken(path: Path, python: str) -> list[ve.Result]:
        raise ve.VerifyError("demo is not on PyPI.")

    monkeypatch.setattr(ve, "verify_notice", broken)
    monkeypatch.setenv("GITHUB_ACTIONS", "true" if in_github else "false")
    assert ve.main([str(REAL_NOTICE)]) == 1
    out = capsys.readouterr().out
    rel = REAL_NOTICE.relative_to(REPO_ROOT).as_posix()
    assert "  FAIL  demo is not on PyPI.\n" in out
    assert out.endswith("\n1 check(s) failed.\n")
    assert (f"::error file={rel}::demo is not on PyPI.\n" in out) is in_github


@posix_only
def test_main_succeeds_when_every_check_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def fine(path: Path, python: str) -> list[ve.Result]:
        assert python == "/usr/bin/python-for-examples"
        return [ve.Result(ve.Check(Version("1.0"), "before", should_run=True), True, "runs")]

    monkeypatch.setattr(ve, "verify_notice", fine)
    outside = tmp_path / "notice.yaml"
    outside.write_text("x\n", encoding="utf-8")
    argv = [str(REAL_NOTICE), str(outside), "--python", "/usr/bin/python-for-examples"]
    assert ve.main(argv) == 0
    out = capsys.readouterr().out
    # Notices in the repository are shown by their path in it, others by their full path.
    assert f"\n{REAL_NOTICE.relative_to(REPO_ROOT).as_posix()}\n" in out
    assert f"\n{outside.as_posix()}\n" in out
    assert out.endswith("\nAll examples behave as their notices say (2 notice(s)).\n")
