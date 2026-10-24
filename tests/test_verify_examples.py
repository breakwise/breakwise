"""Tests for tools/verify_examples.py. They run without network access."""

from __future__ import annotations

import contextlib
import datetime as dt
import email.message
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

import verify_examples as ve

UTC = dt.UTC
REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_NOTICE = REPO_ROOT / "notices/pypi/pydantic-ai/BW-2026-0001.yaml"
PYTHON = Path(sys.executable)
UPLOADED = dt.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

posix_only = pytest.mark.skipif(not ve.POSIX, reason="the verifier runs on Linux and macOS")


def releases(*versions: str) -> dict[Version, dt.datetime]:
    return {Version(v): UPLOADED for v in versions}


def plan(
    available: dict[Version, dt.datetime], breaks_in: str, works_since: str | None = None
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
        Version("1.0.0"): dt.datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
        Version("1.3.0"): dt.datetime(2026, 4, 1, 10, 0, tzinfo=UTC),
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


def test_dependencies_are_cut_off_one_day_after_the_release() -> None:
    uploaded = dt.datetime(2025, 8, 20, 15, 31, 24, 123456, tzinfo=UTC)
    assert ve.exclude_newer(uploaded) == "2025-08-21T15:31:24Z"
    in_riyadh = dt.datetime(2025, 8, 20, 18, 31, 24, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert ve.exclude_newer(in_riyadh) == "2025-08-21T15:31:24Z"


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
        "--exclude-newer 2026-01-02T12:00:00Z demo==1.2.3"
    )


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
    result = ve.evaluate(ve.Check(Version("2.0"), "before", should_run=False), run, "Old")
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
    result = ve.evaluate(ve.Check(Version("2.0"), "before", should_run=False), run, "OpenAIModel")
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
    ("should_run", "returncode", "error", "expected_error", "passed", "detail"),
    [
        (True, 0, "", None, True, "runs"),
        (True, 1, "ImportError: X\n", None, False, "should run, but failed: ImportError: X"),
        (False, 0, "", None, False, "should fail on this version, but it ran"),
        (False, 1, "TypeError: f() got 2\n", None, True, "fails as expected: TypeError: f() got 2"),
        (
            False,
            1,
            "ImportError: cannot import name 'OpenAIModel'\n",
            "OpenAIModel",
            True,
            "fails as expected: ImportError: cannot import name 'OpenAIModel'",
        ),
        (
            False,
            1,
            "ZeroDivisionError: division by zero\n",
            "OpenAIModel",
            False,
            "fails, but not because of OpenAIModel: ZeroDivisionError: division by zero",
        ),
    ],
)
def test_evaluate(
    should_run: bool,
    returncode: int,
    error: str,
    expected_error: str | None,
    passed: bool,
    detail: str,
) -> None:
    check = ve.Check(Version("2.0.0"), "before", should_run=should_run)
    result = ve.evaluate(check, ve.Run(returncode, "", error), expected_error)
    assert (result.passed, result.detail) == (passed, detail)


def test_run_summary_is_one_short_line() -> None:
    assert ve.Run(1).summary == "exit code 1"
    assert ve.Run(1, "Traceback\n  line\nValueError: x\n\n").summary == "ValueError: x"
    assert ve.Run(1, "ignored\n", "KeyError: 'a'\nnote\n").summary == "KeyError: 'a'"
    assert ve.Run(1, "E" * 300).summary == "E" * 197 + "..."
    assert ve.Run(1, "E" * 200).summary == "E" * 200


@pytest.mark.parametrize(
    ("returncode", "error", "passed", "detail"),
    [
        (0, "", True, "takes new as an explicit parameter"),
        (None, "", False, "did not finish in time, so it was stopped"),
        (
            ve.NO_PARAMETER,
            "SystemExit: 3\n",
            False,
            (
                "has no explicit parameter new, and taking it through **kwargs does not count. "
                "Check the versions, or remove demo.make from rename.keyword_of."
            ),
        ),
        (
            1,
            "ModuleNotFoundError: No module named 'demo'\n",
            False,
            "could not be checked: ModuleNotFoundError: No module named 'demo'",
        ),
    ],
)
def test_evaluate_signature(returncode: int | None, error: str, passed: bool, detail: str) -> None:
    check = ve.SignatureCheck(Version("2.0.0"), "demo.make", "new")
    result = ve.evaluate_signature(check, ve.Run(returncode, "", error))
    assert (result.check, result.passed, result.detail) == (check, passed, detail)


@posix_only
@pytest.mark.parametrize(
    ("target", "keyword", "returncode"),
    [
        ("json.dumps", "indent", 0),  # keyword-only
        ("textwrap.wrap", "width", 0),  # positional or keyword
        ("argparse.ArgumentParser", "prog", 0),  # a class, by its __init__
        ("textwrap.TextWrapper.wrap", "text", 0),  # a method
        ("textwrap.wrap", "break_long_words", ve.NO_PARAMETER),  # only through **kwargs
        ("json.dumps", "missing", ve.NO_PARAMETER),
        ("builtins.divmod", "x", ve.NO_PARAMETER),  # positional-only
        ("textwrap.missing", "text", 1),
        ("missing_module.f", "text", 1),
    ],
)
def test_signature_script_accepts_only_explicit_keyword_parameters(
    tmp_path: Path, target: str, keyword: str, returncode: int
) -> None:
    run = ve.run_example(PYTHON, ve.signature_script(target, keyword), tmp_path)
    assert run.returncode == returncode, run.error


# --- A whole notice, offline --------------------------------------------------

FAKE_VERSIONS = {
    "1.0.0": "class Old: pass\n",
    "1.5.0": "class Old: pass\nclass New: pass\n",
    "1.9.0": "class Old: pass\nclass New: pass\n",
    "2.0.0": "class New: pass\n",
}

FAKE_NOTICE = """\
---
schema_version: "0.2"
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

    def __init__(self, versions: dict[str, str] = FAKE_VERSIONS) -> None:
        self.versions = versions
        self.installed: list[tuple[str, str, str]] = []

    def releases(self, name: str) -> dict[Version, dt.datetime]:
        assert name == "fakelib"
        return releases(*self.versions)

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
        notice, sys.executable, fetch=index.releases, installer=index.install
    )
    return [
        f"{'ok' if r.passed else 'FAIL'} {r.check.version} {r.check.label}: {r.detail}"
        for r in results
    ]


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
    ]
    # One environment per version, each with dependencies as of a day after the upload.
    assert index.installed == [
        ("fakelib", "1.9.0", "2026-01-02T12:00:00Z"),
        ("fakelib", "2.0.0", "2026-01-02T12:00:00Z"),
        ("fakelib", "1.5.0", "2026-01-02T12:00:00Z"),
        ("fakelib", "1.0.0", "2026-01-02T12:00:00Z"),
    ]


@posix_only
@pytest.mark.parametrize(
    ("breaks_in", "works_since", "failure"),
    [
        ("1.9.0", "1.5.0", "FAIL 1.9.0 example.before: should fail on this version, but it ran"),
        ("2.0.0", "1.0.0", "FAIL 1.0.0 example.after: should run, but failed: ImportError"),
        ("2.0.0", "1.9.0", "FAIL 1.5.0 example.after: should fail on this version, but it ran"),
    ],
)
def test_a_notice_with_wrong_versions_fails(
    tmp_path: Path, breaks_in: str, works_since: str, failure: str
) -> None:
    lines = verify(write_notice(tmp_path, breaks_in, works_since), FakeIndex())
    failures = [line for line in lines if line.startswith("FAIL")]
    assert len(failures) == 1
    assert failures[0].startswith(failure)


@posix_only
def test_breaks_in_must_be_the_first_release_that_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pre-release already removed Old, so 2.0.0 is not the first release that breaks.
    monkeypatch.setitem(FAKE_VERSIONS, "2.0.0b1", "class New: pass\n")
    lines = verify(write_notice(tmp_path), FakeIndex())
    failures = [line for line in lines if line.startswith("FAIL")]
    assert len(failures) == 1
    assert failures[0].startswith(
        "FAIL 2.0.0b1 example.before: should run, but failed: ImportError: cannot import name 'Old'"
    )


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


@posix_only
def test_any_failure_counts_for_changes_other_than_renames(tmp_path: Path) -> None:
    notice = write_notice(tmp_path)
    text = notice.read_text(encoding="utf-8").replace("type: rename", "type: removal")
    text = text.replace(
        "rename:\n  from: fakelib.Old\n  to: fakelib.New\n", "instructions: Use New instead.\n"
    )
    notice.write_text(text, encoding="utf-8")
    lines = verify(notice, FakeIndex())
    assert [line.split(":")[0] for line in lines] == [
        "ok 1.9.0 example.before",
        "ok 1.9.0 example.after",
        "ok 2.0.0 example.before",
        "ok 2.0.0 example.after",
        "ok 1.5.0 example.after",
        "ok 1.0.0 example.after",
    ]


# --- A renamed keyword argument, offline --------------------------------------

KEYWORD_VERSIONS = {
    "1.0.0": "def make(*, old=None): pass\n",
    "1.5.0": "def make(*, old=None, new=None): pass\n",
    "1.9.0": "def make(*, old=None, new=None): pass\n",
    "2.0.0": "def make(*, new=None): pass\n",
}

KEYWORD_NOTICE = """\
---
schema_version: "0.2"
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
        "ok 2.0.0 signature of fakelib.make: takes new as an explicit parameter",
        "ok 1.5.0 signature of fakelib.make: takes new as an explicit parameter",
    ]
    # The signatures are checked in the environments the examples ran in.
    assert [version for _, version, _ in index.installed] == ["1.9.0", "2.0.0", "1.5.0", "1.0.0"]


@posix_only
def test_without_works_since_only_breaks_in_has_its_signature_checked(tmp_path: Path) -> None:
    lines = verify(write_keyword_notice(tmp_path, works_since=None), FakeIndex(KEYWORD_VERSIONS))
    assert [line.split(":")[0] for line in lines] == [
        "ok 1.9.0 example.before",
        "ok 2.0.0 example.before",
        "ok 2.0.0 example.after",
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
    assert len(lines) == 8


@posix_only
def test_every_callable_in_keyword_of_is_checked(tmp_path: Path) -> None:
    notice = write_keyword_notice(tmp_path)
    text = notice.read_text(encoding="utf-8")
    text = text.replace("    - fakelib.make\n", "    - fakelib.make\n    - fakelib.build\n")
    notice.write_text(text, encoding="utf-8")
    lines = verify(notice, FakeIndex(KEYWORD_VERSIONS))
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
