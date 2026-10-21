"""Tests for tools/verify_examples.py. They run without network access."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest
from packaging.version import Version

import verify_examples as ve

UTC = dt.UTC
REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = Path(sys.executable)


def releases(*versions: str) -> dict[Version, dt.datetime]:
    upload = dt.datetime(2026, 1, 1, tzinfo=UTC)
    return {Version(v): upload for v in versions}


def plan(available: dict[Version, dt.datetime], breaks_in: str, works_since: str | None = None):
    since = Version(works_since) if works_since else None
    checks = ve.plan_checks(available, Version(breaks_in), since)
    return [(str(c.version), c.example, c.should_run) for c in checks]


def test_parse_releases_skips_yanked_and_empty_releases() -> None:
    data = {
        "1.0.0": [
            {"upload_time_iso_8601": "2026-01-01T10:00:00.000000Z", "yanked": False},
            {"upload_time_iso_8601": "2026-01-01T10:05:00.000000Z", "yanked": False},
        ],
        "1.1.0": [{"upload_time_iso_8601": "2026-02-01T10:00:00.000000Z", "yanked": True}],
        "1.2.0": [],
        "not-a-version": [{"upload_time_iso_8601": "2026-03-01T10:00:00Z", "yanked": False}],
    }
    assert ve.parse_releases(data) == {
        Version("1.0.0"): dt.datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
    }


def test_plan_without_works_since() -> None:
    available = releases("1.0.0", "1.1.0", "2.0.0rc1", "2.0.0", "2.1.0")
    assert plan(available, "2.0.0") == [
        ("1.1.0", "before", True),
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
    ]


def test_plan_does_not_repeat_versions() -> None:
    available = releases("1.0.0", "1.1.0", "2.0.0")
    assert plan(available, "2.0.0", "1.1.0") == [
        ("1.1.0", "before", True),
        ("1.1.0", "after", True),
        ("2.0.0", "before", False),
        ("2.0.0", "after", True),
    ]
    assert plan(available, "2.0.0", "2.0.0") == [
        ("1.1.0", "before", True),
        ("2.0.0", "before", False),
        ("2.0.0", "after", True),
    ]


def test_plan_checks_a_pre_release_works_since_on_its_own() -> None:
    available = releases("1.0.0", "2.0.0b1", "2.0.0")
    assert ("2.0.0b1", "after", True) in plan(available, "2.0.0", "2.0.0b1")


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


def test_examples_cannot_use_the_network(tmp_path: Path) -> None:
    code = "import socket\nsocket.create_connection(('example.com', 80), timeout=5)\n"
    run = ve.run_example(PYTHON, code, tmp_path)
    assert run.returncode != 0
    result = ve.evaluate(ve.Check(Version("1.0"), "after", should_run=True), run)
    assert not result.passed
    assert "network" in result.detail


def test_examples_cannot_look_up_hosts(tmp_path: Path) -> None:
    run = ve.run_example(
        PYTHON, "import socket\nsocket.getaddrinfo('example.com', 443)\n", tmp_path
    )
    assert ve.NETWORK_BLOCKED in run.stderr


def test_examples_get_fake_keys_and_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUMPFIX_TEST_SECRET", "do-not-leak")
    code = (
        "import os\n"
        "assert 'BUMPFIX_TEST_SECRET' not in os.environ\n"
        f"assert os.environ['OPENAI_API_KEY'] == {ve.FAKE_KEY!r}\n"
    )
    assert ve.run_example(PYTHON, code, tmp_path).returncode == 0


def test_examples_that_hang_are_stopped(tmp_path: Path) -> None:
    run = ve.run_example(PYTHON, "import time\ntime.sleep(30)\n", tmp_path, timeout=0.5)
    assert run.returncode is None
    assert not ve.evaluate(ve.Check(Version("1.0"), "before", should_run=False), run).passed


@pytest.mark.parametrize(
    ("should_run", "returncode", "passed", "detail"),
    [
        (True, 0, True, "runs"),
        (True, 1, False, "should run, but failed: ImportError: cannot import name 'X'"),
        (False, 1, True, "fails as expected: ImportError: cannot import name 'X'"),
        (False, 0, False, "should fail on this version, but it ran"),
    ],
)
def test_evaluate(should_run: bool, returncode: int, passed: bool, detail: str) -> None:
    check = ve.Check(Version("2.0.0"), "before", should_run=should_run)
    stderr = "Traceback (most recent call last):\nImportError: cannot import name 'X'\n"
    result = ve.evaluate(check, ve.Run(returncode, stderr if returncode else ""))
    assert (result.passed, result.detail) == (passed, detail)


def test_all_notices_are_verified_when_the_tooling_changes() -> None:
    every = ve.all_notices(REPO_ROOT)
    assert ve.notices_to_verify(REPO_ROOT, ["tools/verify_examples.py"]) == every
    assert ve.notices_to_verify(REPO_ROOT, ["requirements-dev.txt", "README.md"]) == every


def test_only_changed_notices_are_verified_otherwise() -> None:
    notice = "notices/pypi/pydantic-ai/BFX-2026-0001.yaml"
    assert ve.notices_to_verify(REPO_ROOT, [notice, "README.md"]) == [REPO_ROOT / notice]
    assert ve.notices_to_verify(REPO_ROOT, ["README.md", "notices/LICENSE.md"]) == []


def test_main_needs_exactly_one_kind_of_target() -> None:
    with pytest.raises(SystemExit):
        ve.main([])
    with pytest.raises(SystemExit):
        ve.main(["--all", "--changed-since", "origin/main"])
