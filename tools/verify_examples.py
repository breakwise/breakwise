#!/usr/bin/env python3
"""Run each notice's examples against real versions of its package.

    python tools/verify_examples.py --all
    python tools/verify_examples.py notices/pypi/pydantic-ai/BFX-2026-0001.yaml
    python tools/verify_examples.py --changed-since origin/main

A notice must first pass tools/validate_notices.py. Then its examples are run
on real releases of its package:

- on the last stable release before ``breaks_in``, ``example.before`` must run,
  and so must ``example.after`` when ``works_since`` is not later than that release;
- on ``breaks_in``, ``example.before`` must fail and ``example.after`` must run.
  For a rename, the exception that ends ``example.before`` must name the old
  name, so that a failure for another reason does not count;
- on ``works_since``, when it is set, ``example.after`` must run.

Releases and their upload times come from PyPI's JSON Simple API (PEP 691 and
PEP 700). Each version is installed with ``uv`` together with its dependencies
as they were one day after it was uploaded (``--exclude-newer``), so that later
releases of other packages cannot break old versions.

Examples run in a separate Python process that gets none of this process's
environment variables, so no secrets, only fake API keys for common AI
providers. Inside that process, network connections and DNS lookups are
blocked and recorded. An example that fails after a network attempt is
reported as such, even if it caught the error, because the attempt may be the
cause. This stops accidental network use; it is not a security boundary. In
CI the job also runs with read-only permissions and no secrets, and pull
requests from outside contributors need a maintainer's approval to run. An
example that runs too long is stopped together with every process it started.

With ``--changed-since REF``, only the notices added or changed since the
current commit's history left REF are checked, or all of them if this tool,
its dependencies or the workflow changed.

It runs on Linux and macOS. The exit code is 1 if any check fails, otherwise 0.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import functools
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from validate_notices import (
    NOTICES_DIR,
    InvalidNoticeError,
    distinguishing_names,
    github_error,
    load_validator,
    read_notice,
    registry_files,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
# Process groups, killpg and the virtual environment layout used here are POSIX.
POSIX = os.name == "posix"

PYPI_SIMPLE = "https://pypi.org/simple/{name}/"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
USER_AGENT = "bumpfix-verify-examples (+https://github.com/bumpfix/bumpfix)"
PYPI_ATTEMPTS = 3
PYPI_TIMEOUT = 60.0

# Dependencies are installed as they were this long after a release was uploaded.
# The margin covers projects that publish several packages for one release.
DEPENDENCY_WINDOW = dt.timedelta(days=1)
INSTALL_TIMEOUT = 900.0
EXAMPLE_TIMEOUT = 120.0
# How long to wait for an example's output after it was stopped. A process that
# left its process group can keep the output open, so the wait must end.
STOPPED_OUTPUT_TIMEOUT = 5.0
SUMMARY_LENGTH = 200

FAKE_KEY = "bumpfix-fake-key"
FAKE_KEY_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "CO_API_KEY",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "HF_TOKEN",
    "MISTRAL_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "XAI_API_KEY",
)

# A change to any of these files can change every result, so all notices are verified.
RUN_ALL_WHEN_CHANGED = frozenset(
    {
        "tools/verify_examples.py",
        "tools/validate_notices.py",
        "requirements-dev.txt",
        ".github/workflows/checks.yml",
    }
)

NETWORK_BLOCKED = "bumpfix: network access is blocked while running examples"
# Runs an example with the network blocked. It records each network attempt and
# the exception that ends the example, as Python prints it.
GUARD_SCRIPT = f"""\
import runpy
import socket
import sys
import traceback

_example, _network_log, _error_file = sys.argv[1:4]


def _blocked(*args, **kwargs):
    with open(_network_log, "a", encoding="utf-8") as log:
        log.write("blocked\\n")
    raise OSError({NETWORK_BLOCKED!r})


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
socket.gethostbyname = _blocked
socket.gethostbyname_ex = _blocked

sys.argv = [_example]
try:
    runpy.run_path(_example, run_name="__main__")
except BaseException as error:
    with open(_error_file, "w", encoding="utf-8") as out:
        out.write("".join(traceback.format_exception_only(error)))
    raise
"""

Fetcher = Callable[[str], dict[Version, dt.datetime]]
Installer = Callable[[str, Version, str, str, Path], Path]


class VerifyError(Exception):
    """A notice cannot be verified, for example because a version is not on PyPI."""


@dataclass(frozen=True)
class Check:
    """Run one example on one version and expect it to run or to fail."""

    version: Version
    example: str
    should_run: bool


@dataclass(frozen=True)
class Run:
    """How an example ended.

    ``returncode`` is None if the example ran too long and was stopped.
    ``error`` is the exception that ended it, as Python prints it, e.g.
    ``ImportError: cannot import name 'X' from 'y'``, or "" if none did.
    """

    returncode: int | None
    stderr: str = ""
    error: str = ""
    used_network: bool = False

    @property
    def summary(self) -> str:
        """One line that says how the example failed."""
        if self.error.strip():
            text = self.error.strip().splitlines()[0]
        else:
            lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
            text = lines[-1] if lines else f"exit code {self.returncode}"
        if len(text) <= SUMMARY_LENGTH:
            return text
        return text[: SUMMARY_LENGTH - 3] + "..."


@dataclass(frozen=True)
class Result:
    check: Check
    passed: bool
    detail: str


def version_of(filename: str) -> Version | None:
    """Return the version in the name of a wheel or source distribution, or None."""
    try:
        if filename.endswith(".whl"):
            return parse_wheel_filename(filename)[1]
        return parse_sdist_filename(filename)[1]
    except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion):
        return None


def parse_files(files: Iterable[Mapping[str, Any]]) -> dict[Version, dt.datetime]:
    """Map every release that has files which are not yanked to its latest upload time.

    ``files`` are the file entries of a project page in PyPI's JSON Simple API.
    Files that are neither wheels nor source distributions, such as eggs, are skipped.
    """
    releases: dict[Version, dt.datetime] = {}
    for file in files:
        version = version_of(file["filename"])
        uploaded = file.get("upload-time")
        if version is None or not uploaded or file.get("yanked"):
            continue
        upload_time = dt.datetime.fromisoformat(uploaded)
        releases[version] = max(upload_time, releases.get(version, upload_time))
    return releases


def fetch_releases(
    name: str, attempts: int = PYPI_ATTEMPTS, sleep: Callable[[float], None] = time.sleep
) -> dict[Version, dt.datetime]:
    """Return the releases of a package on PyPI and when they were uploaded.

    This is the data uv uses for ``--exclude-newer``. When PyPI cannot be
    reached or is busy, the request is repeated after a growing pause.
    """
    body = _read_project_page(name, attempts, sleep)
    try:
        return parse_files(json.loads(body)["files"])
    except (ValueError, KeyError, TypeError) as error:
        raise VerifyError(f"PyPI sent an unexpected response for {name}: {error!r}") from error


def _read_project_page(name: str, attempts: int, sleep: Callable[[float], None]) -> bytes:
    request = urllib.request.Request(  # noqa: S310 - a fixed https URL
        PYPI_SIMPLE.format(name=name), headers={"Accept": SIMPLE_JSON, "User-Agent": USER_AGENT}
    )
    reason = ""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=PYPI_TIMEOUT) as response:  # noqa: S310 - the same URL
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as error:
            if error.code == HTTPStatus.NOT_FOUND:
                raise VerifyError(f"{name} is not on PyPI.") from error
            busy = error.code == HTTPStatus.TOO_MANY_REQUESTS
            if not busy and error.code < HTTPStatus.INTERNAL_SERVER_ERROR:
                raise VerifyError(
                    f"PyPI refused the request for {name}: HTTP {error.code}."
                ) from error
            reason = f"HTTP {error.code}"
        except (OSError, http.client.HTTPException) as error:
            reason = str(getattr(error, "reason", "") or error) or type(error).__name__
        if attempt < attempts:
            sleep(2.0**attempt)
    raise VerifyError(f"Could not read {name} from PyPI after {attempts} attempts: {reason}")


def plan_checks(
    releases: Mapping[Version, dt.datetime], breaks_in: Version, works_since: Version | None
) -> list[Check]:
    """Decide which examples to run on which versions."""
    for field, version in (("breaks_in", breaks_in), ("works_since", works_since)):
        if version is not None and version not in releases:
            raise VerifyError(f"{field} {version} is not a release on PyPI, or it was yanked.")
    earlier = [v for v in releases if v < breaks_in and not v.is_prerelease]
    if not earlier:
        raise VerifyError(f"There is no stable release before breaks_in {breaks_in}.")
    previous = max(earlier)

    checks = [Check(previous, "before", should_run=True)]
    if works_since is not None and works_since <= previous:
        checks.append(Check(previous, "after", should_run=True))
    checks += [
        Check(breaks_in, "before", should_run=False),
        Check(breaks_in, "after", should_run=True),
    ]
    if works_since is not None and works_since not in (previous, breaks_in):
        checks.append(Check(works_since, "after", should_run=True))
    return checks


def exclude_newer(uploaded: dt.datetime) -> str:
    """Return the cut-off for dependencies of a release, in RFC 3339 (UTC)."""
    cutoff = (uploaded + DEPENDENCY_WINDOW).astimezone(dt.UTC)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def install(package: str, version: Version, cutoff: str, python: str, target: Path) -> Path:
    """Create a virtual environment with one version of a package. Return its interpreter."""
    uv = shutil.which("uv")
    if uv is None:
        raise VerifyError("uv is not installed. Install requirements-dev.txt first.")
    # --no-config: uv settings on this machine must not change what gets installed.
    _run_tool([uv, "venv", "--no-config", "--quiet", "--python", python, str(target)])
    interpreter = target / "bin" / "python"
    install_command = [uv, "pip", "install", "--no-config", "--quiet", "--python", str(interpreter)]
    _run_tool([*install_command, "--exclude-newer", cutoff, f"{package}=={version}"])
    return interpreter


def _run_tool(command: list[str]) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed tool, arguments from the notice schema
            command, capture_output=True, text=True, timeout=INSTALL_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise VerifyError(f"Timed out: {' '.join(command)}") from error
    if completed.returncode != 0:
        raise VerifyError(f"Failed: {' '.join(command)}\n{completed.stderr.strip()}")


def run_example(
    interpreter: Path, code: str, workdir: Path, timeout: float = EXAMPLE_TIMEOUT
) -> Run:
    """Run example code offline, with fake API keys and nothing else from the environment."""
    workdir.mkdir(exist_ok=True)
    example = workdir / "example.py"
    guard = workdir / "run_example.py"
    network_log = workdir / "network.log"
    error_file = workdir / "error.txt"
    example.write_text(code, encoding="utf-8")
    guard.write_text(GUARD_SCRIPT, encoding="utf-8")
    env = {"PATH": os.defpath, "HOME": str(workdir), "LANG": "C.UTF-8"}
    env.update(dict.fromkeys(FAKE_KEY_VARIABLES, FAKE_KEY))
    # -I ignores PYTHON* variables and user site-packages; -X utf8 fixes the output encoding.
    command = [str(interpreter), "-I", "-X", "utf8", str(guard), str(example)]
    with subprocess.Popen(  # noqa: S603 - runs the notice's example, which is the point
        [*command, str(network_log), str(error_file)],
        cwd=workdir,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    ) as process:
        try:
            _, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop(process)
            return Run(None, used_network=network_log.exists())
    error = error_file.read_text(encoding="utf-8", errors="replace") if error_file.exists() else ""
    return Run(process.returncode, stderr, error, network_log.exists())


def _stop(process: subprocess.Popen[str]) -> None:
    """Kill a process and every process it started, then collect what is left of its output."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.communicate(timeout=STOPPED_OUTPUT_TIMEOUT)


def evaluate(  # noqa: PLR0911 - one return for each way an example can end
    check: Check, run: Run, expected_error: str | None = None
) -> Result:
    """Compare how an example ended with what the notice says should happen.

    A failure only counts as expected if the exception that ended the example
    contains ``expected_error``.
    """
    if run.returncode is None:
        return Result(check, False, "did not finish in time, so it was stopped")
    if run.returncode == 0:
        if not check.should_run:
            return Result(check, False, "should fail on this version, but it ran")
        note = " (a network call it made was blocked and ignored)" if run.used_network else ""
        return Result(check, True, f"runs{note}")
    if run.used_network:
        message = "failed after it tried to use the network; examples must run offline"
        return Result(check, False, f"{message}: {run.summary}")
    if check.should_run:
        return Result(check, False, f"should run, but failed: {run.summary}")
    if expected_error is not None and expected_error not in run.error:
        return Result(check, False, f"fails, but not because of {expected_error}: {run.summary}")
    return Result(check, True, f"fails as expected: {run.summary}")


@functools.cache
def _validator() -> Draft202012Validator:
    return load_validator(REPO_ROOT)


def _read_valid_notice(path: Path) -> dict[str, Any]:
    try:
        return read_notice(path, path, _validator()).data
    except InvalidNoticeError as error:
        first = error.problems[0]
        message = f"The notice is not valid (line {first.line}: {first.message})"
        raise VerifyError(f"{message}. Run tools/validate_notices.py.") from error


def verify_notice(
    path: Path,
    python: str,
    fetch: Fetcher = fetch_releases,
    installer: Installer = install,
) -> list[Result]:
    """Install the versions a notice needs and run its examples on them."""
    notice = _read_valid_notice(path)
    name = notice["package"]["name"]
    breaks_in = Version(notice["breaks_in"])
    works_since = Version(notice["works_since"]) if "works_since" in notice else None
    expected_error = None
    if notice["type"] == "rename":
        expected_error = distinguishing_names(notice["rename"]["from"], notice["rename"]["to"])[0]

    releases = fetch(name)
    checks = plan_checks(releases, breaks_in, works_since)
    results = []
    with tempfile.TemporaryDirectory(prefix="bumpfix-") as tmp_name:
        tmp = Path(tmp_name).resolve()
        interpreters: dict[Version, Path] = {}
        for number, check in enumerate(checks):
            if check.version not in interpreters:
                cutoff = exclude_newer(releases[check.version])
                target = tmp / f"venv-{check.version}"
                interpreters[check.version] = installer(name, check.version, cutoff, python, target)
            code = notice["example"][check.example]
            run = _shorten_paths(
                run_example(interpreters[check.version], code, tmp / f"run-{number}"), tmp
            )
            results.append(evaluate(check, run, None if check.should_run else expected_error))
    return results


def _shorten_paths(run: Run, tmp: Path) -> Run:
    """Temporary paths in error messages are noise, so they are shortened."""
    return dataclasses.replace(
        run,
        stderr=run.stderr.replace(str(tmp), "<tmp>"),
        error=run.error.replace(str(tmp), "<tmp>"),
    )


def all_notices(root: Path) -> list[Path]:
    return [path for path in registry_files(root) if path.suffix == ".yaml"]


def changed_files(root: Path, ref: str) -> list[str]:
    """Files added, changed or renamed since ``ref`` (compared with their common ancestor)."""
    git = shutil.which("git")
    if git is None:
        raise VerifyError("git is not installed.")
    completed = subprocess.run(  # noqa: S603 - fixed git command
        [git, "diff", "-z", "--name-only", "--diff-filter=ACMR", f"{ref}...HEAD"],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        raise VerifyError(f"git diff against {ref} failed: {completed.stderr.strip()}")
    return [name for name in completed.stdout.split("\0") if name]


def notices_to_verify(root: Path, changed: Iterable[str]) -> list[Path]:
    """All notices if the tooling changed, otherwise only the changed notices."""
    changed = set(changed)
    if changed & RUN_ALL_WHEN_CHANGED:
        return all_notices(root)
    prefix = NOTICES_DIR.as_posix() + "/"
    return sorted(
        root / name for name in changed if name.startswith(prefix) and name.endswith(".yaml")
    )


def select_notices(args: argparse.Namespace, root: Path) -> list[Path]:
    """The notice files chosen on the command line."""
    if args.all:
        return all_notices(root)
    if args.changed_since is not None:
        return notices_to_verify(root, changed_files(root, args.changed_since))
    paths = [Path(path).resolve() for path in args.notices]
    missing = [path.as_posix() for path in paths if not path.is_file()]
    if missing:
        raise VerifyError(f"Not found: {', '.join(missing)}")
    return paths


def report(paths: Sequence[Path], python: str, root: Path, in_github: bool) -> int:
    """Verify each notice, print the results and return the number of failures."""
    failures = 0
    for path in paths:
        shown = path.relative_to(root) if path.is_relative_to(root) else path
        print(f"\n{shown.as_posix()}")
        try:
            results = verify_notice(path, python)
        except VerifyError as error:
            failures += 1
            print(f"  FAIL  {error}")
            if in_github:
                print(github_error(shown, str(error)))
            continue
        for result in results:
            status = "ok  " if result.passed else "FAIL"
            what = f"{result.check.version}  example.{result.check.example}"
            print(f"  {status}  {what}: {result.detail}")
            if not result.passed:
                failures += 1
                if in_github:
                    print(github_error(shown, f"{what}: {result.detail}"))
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run each notice's examples against real versions of its package."
    )
    parser.add_argument("notices", nargs="*", type=Path, help="notice files to verify")
    parser.add_argument("--all", action="store_true", help="verify every notice")
    parser.add_argument(
        "--changed-since", metavar="REF", help="verify the notices changed since a git ref"
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter for the examples (default: the one running this script)",
    )
    args = parser.parse_args(argv)
    if [bool(args.notices), args.all, args.changed_since is not None].count(True) != 1:
        parser.error("give notice files, --all, or --changed-since REF")
    if not POSIX:
        print("FAIL  tools/verify_examples.py runs on Linux and macOS.")
        return 1

    try:
        paths = select_notices(args, REPO_ROOT)
    except VerifyError as error:
        print(f"FAIL  {error}")
        return 1
    if not paths:
        print("No notices to verify.")
        return 0

    failures = report(paths, args.python, REPO_ROOT, os.environ.get("GITHUB_ACTIONS") == "true")
    if failures:
        print(f"\n{failures} check(s) failed.")
        return 1
    print(f"\nAll examples behave as their notices say ({len(paths)} notice(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
