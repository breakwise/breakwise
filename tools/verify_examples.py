#!/usr/bin/env python3
"""Run each notice's examples against real versions of its package.

    python tools/verify_examples.py --all
    python tools/verify_examples.py notices/pypi/pydantic-ai/BFX-2026-0001.yaml
    python tools/verify_examples.py --changed-since origin/main

For each notice, versions are taken from PyPI and the examples are run on them:

- on the last stable release before ``breaks_in``, ``example.before`` must run,
  and so must ``example.after`` when ``works_since`` is not later than that release;
- on ``breaks_in``, ``example.before`` must fail and ``example.after`` must run;
- on ``works_since``, when it is set, ``example.after`` must run.

Each version is installed with ``uv`` together with its dependencies as they
were one day after it was released (``--exclude-newer``), so that later
releases of other packages cannot break old versions.

Examples run in a separate Python process that gets none of this process's
environment variables, so no secrets, only fake API keys for common AI
providers. Inside that process, network connections and DNS lookups are
blocked. The block stops accidental network use; it is not a security
boundary. In CI the job also runs with read-only permissions and no secrets,
and pull requests from outside contributors need a maintainer's approval to run.

With ``--changed-since REF``, only the notices added or changed since REF are
checked, or all of them if this tool, its dependencies or the workflow changed.

The exit code is 1 if any check fails, otherwise 0.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from packaging.version import InvalidVersion, Version

from validate_notices import NOTICES_DIR, github_error

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPI_JSON = "https://pypi.org/pypi/{name}/json"

# Dependencies are installed as they were this long after a release was uploaded.
# The margin covers projects that publish several packages for one release.
DEPENDENCY_WINDOW = dt.timedelta(days=1)
EXAMPLE_TIMEOUT = 120.0
INSTALL_TIMEOUT = 900.0

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
GUARD_SCRIPT = f"""\
import runpy
import socket
import sys


def _blocked(*args, **kwargs):
    raise OSError({NETWORK_BLOCKED!r})


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
socket.gethostbyname = _blocked
socket.gethostbyname_ex = _blocked

example = sys.argv[1]
sys.argv = [example]
runpy.run_path(example, run_name="__main__")
"""


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
    """How an example ended. ``returncode`` is None if it timed out."""

    returncode: int | None
    stderr: str

    @property
    def summary(self) -> str:
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        text = lines[-1] if lines else f"exit code {self.returncode}"
        return text if len(text) <= 200 else text[:197] + "..."


@dataclass(frozen=True)
class Result:
    check: Check
    passed: bool
    detail: str


def parse_releases(releases: dict[str, list[dict[str, object]]]) -> dict[Version, dt.datetime]:
    """Map every release that has files and is not yanked to its latest upload time."""
    result: dict[Version, dt.datetime] = {}
    for text, files in releases.items():
        available = [f for f in files if not f.get("yanked")]
        if not available:
            continue
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        result[version] = max(
            dt.datetime.fromisoformat(str(f["upload_time_iso_8601"]).replace("Z", "+00:00"))
            for f in available
        )
    return result


def fetch_releases(name: str) -> dict[Version, dt.datetime]:
    """Return the releases of a package on PyPI with their upload times."""
    request = urllib.request.Request(
        PYPI_JSON.format(name=name),
        headers={"Accept": "application/json", "User-Agent": "bumpfix-verify-examples"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise VerifyError(f"{name} is not on PyPI.") from error
        raise VerifyError(f"Could not read {name} from PyPI: {error}") from error
    except urllib.error.URLError as error:
        raise VerifyError(f"Could not reach PyPI: {error.reason}") from error
    return parse_releases(data["releases"])


def plan_checks(
    releases: dict[Version, dt.datetime], breaks_in: Version, works_since: Version | None
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
    cutoff = (uploaded + DEPENDENCY_WINDOW).astimezone(dt.UTC).replace(microsecond=0)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def install(package: str, version: Version, cutoff: str, python: str, target: Path) -> Path:
    """Create a virtual environment with one version of a package. Return its interpreter."""
    uv = shutil.which("uv")
    if uv is None:
        raise VerifyError("uv is not installed. Install requirements-dev.txt first.")
    _run_tool([uv, "venv", "--quiet", "--python", python, str(target)])
    interpreter = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run_tool(
        [
            uv,
            "pip",
            "install",
            "--quiet",
            "--python",
            str(interpreter),
            "--exclude-newer",
            cutoff,
            f"{package}=={version}",
        ]
    )
    return interpreter


def _run_tool(command: list[str]) -> None:
    try:
        completed = subprocess.run(
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
    workdir.mkdir(parents=True, exist_ok=True)
    example = workdir / "example.py"
    guard = workdir / "run_example.py"
    example.write_text(code, encoding="utf-8")
    guard.write_text(GUARD_SCRIPT, encoding="utf-8")
    env = {
        "PATH": os.defpath,
        "HOME": str(workdir),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(dict.fromkeys(FAKE_KEY_VARIABLES, FAKE_KEY))
    try:
        completed = subprocess.run(
            [str(interpreter), "-I", str(guard), str(example)],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Run(None, "")
    return Run(completed.returncode, completed.stderr)


def evaluate(check: Check, run: Run) -> Result:
    """Compare how an example ended with what the notice says should happen."""
    if run.returncode is None:
        return Result(check, False, f"timed out after {EXAMPLE_TIMEOUT:g} seconds")
    if NETWORK_BLOCKED in run.stderr:
        return Result(check, False, "tried to use the network; examples must run offline")
    if check.should_run:
        if run.returncode == 0:
            return Result(check, True, "runs")
        return Result(check, False, f"should run, but failed: {run.summary}")
    if run.returncode != 0:
        return Result(check, True, f"fails as expected: {run.summary}")
    return Result(check, False, "should fail on this version, but it ran")


def verify_notice(
    path: Path,
    python: str,
    fetch: Callable[[str], dict[Version, dt.datetime]] = fetch_releases,
) -> list[Result]:
    """Install the versions a notice needs and run its examples on them."""
    notice = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        package = notice["package"]
        name, ecosystem = package["name"], package["ecosystem"]
        breaks_in = Version(notice["breaks_in"])
        works_since = Version(notice["works_since"]) if "works_since" in notice else None
        examples = {which: notice["example"][which] for which in ("before", "after")}
    except (KeyError, TypeError, InvalidVersion) as error:
        message = f"The notice is not valid ({error}). Run tools/validate_notices.py."
        raise VerifyError(message) from error
    if ecosystem != "PyPI":
        raise VerifyError(f"Examples can only be verified for PyPI packages, not {ecosystem}.")

    releases = fetch(name)
    checks = plan_checks(releases, breaks_in, works_since)
    results = []
    with tempfile.TemporaryDirectory(prefix="bumpfix-") as tmp:
        interpreters: dict[Version, Path] = {}
        for number, check in enumerate(checks):
            if check.version not in interpreters:
                interpreters[check.version] = install(
                    name,
                    check.version,
                    exclude_newer(releases[check.version]),
                    python,
                    Path(tmp) / f"venv-{check.version}",
                )
            run = run_example(
                interpreters[check.version], examples[check.example], Path(tmp) / f"run-{number}"
            )
            # Temporary paths in error messages are noise, so they are shortened.
            run = Run(run.returncode, run.stderr.replace(tmp, "<tmp>"))
            results.append(evaluate(check, run))
    return results


def all_notices(root: Path) -> list[Path]:
    return sorted((root / NOTICES_DIR).rglob("*.yaml"))


def changed_files(root: Path, ref: str) -> list[str]:
    """Files added, changed or renamed since ``ref`` (compared with their common ancestor)."""
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{ref}...HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise VerifyError(f"git diff against {ref} failed: {completed.stderr.strip()}")
    return [line for line in completed.stdout.splitlines() if line]


def notices_to_verify(root: Path, changed: Iterable[str]) -> list[Path]:
    """All notices if the tooling changed, otherwise only the changed notices."""
    changed = set(changed)
    if changed & RUN_ALL_WHEN_CHANGED:
        return all_notices(root)
    prefix = NOTICES_DIR.as_posix() + "/"
    return sorted(
        root / name for name in changed if name.startswith(prefix) and name.endswith(".yaml")
    )


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

    root = REPO_ROOT
    try:
        if args.all:
            paths = all_notices(root)
        elif args.changed_since is not None:
            paths = notices_to_verify(root, changed_files(root, args.changed_since))
        else:
            paths = [path.resolve() for path in args.notices]
    except VerifyError as error:
        print(f"FAIL  {error}")
        return 1
    if not paths:
        print("No notices to verify.")
        return 0

    in_github = os.environ.get("GITHUB_ACTIONS") == "true"
    failures = 0
    for path in paths:
        shown = path.relative_to(root) if path.is_relative_to(root) else path
        print(f"\n{shown.as_posix()}")
        try:
            results = verify_notice(path, args.python)
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

    if failures:
        print(f"\n{failures} check(s) failed.")
        return 1
    print(f"\nAll examples behave as their notices say ({len(paths)} notice(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
