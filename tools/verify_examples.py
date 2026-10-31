#!/usr/bin/env python3
"""Run each notice's examples against real versions of its package.

    python tools/verify_examples.py --all
    python tools/verify_examples.py notices/pypi/pydantic-ai/BW-2026-0001.yaml
    python tools/verify_examples.py --changed-since origin/main

A notice must first pass tools/validate_notices.py. Then its examples are run
on real releases of its package:

- on the last stable release before ``breaks_in``, ``example.before`` must run,
  and so must ``example.after`` when ``works_since`` is not later than that release;
- on the release just before ``breaks_in``, when it is a pre-release,
  ``example.before`` must run too, so that ``breaks_in`` is the first release
  on which it fails;
- on ``breaks_in``, ``example.before`` must fail and ``example.after`` must run.
  For a rename, the exception that ends ``example.before`` must name the old
  name, so that a failure for another reason does not count;
- on ``works_since``, when it is set, ``example.after`` must run, and on the
  release just before it, ``example.after`` must fail, so that ``works_since``
  is the first release on which it runs. For a rename, that exception must
  name the new name;
- wherever an example must fail, its exception must match ``example.fails_with``
  when it is set, as it must for every type but rename: the name of its type as
  Python prints it, and a part of its message when one is given;
- ``restored_in``, when it is set, must be a release on PyPI. There
  ``example.before`` runs again, and so does ``example.after``. On the release
  just before it, ``example.before`` must still fail. On a latest release from
  ``restored_in`` on, ``example.before`` must run instead of fail;
- for a rename, the paths must resolve with ``pkgutil.resolve_name`` where the
  examples need them: ``rename.from`` on every release where ``example.before``
  runs, but not on ``breaks_in``, and ``rename.to`` on every release where
  ``example.after`` runs. For a renamed keyword argument, each callable in
  ``rename.keyword_of`` must resolve on every release where an example runs;
- for a renamed keyword argument, each callable in ``rename.keyword_of`` must
  also take ``rename.to`` as an explicit parameter on ``breaks_in`` and on
  ``works_since``, as ``inspect.signature`` shows it. Taking it only through
  ``**kwargs`` does not count, because then ``example.after`` can run although
  the callable ignores the argument;
- for every other type, every path in ``affects`` must resolve where
  ``example.before`` runs. Where it must fail, the path alone of a removal must
  be gone, and every other path must still resolve. Each keyword in ``affects``
  must be an explicit parameter of its callable where ``example.before`` runs;
- on the latest release after ``breaks_in`` that accepts the Python of the
  examples, everything is checked as on ``breaks_in``. That is the latest stable
  release, or the latest pre-release when ``breaks_in`` is a pre-release and no
  stable release follows it. A failure there means that the library may have
  changed after ``breaks_in``, for example by bringing the old name back. The
  latest release changes over time, so this result depends on when it runs.

The paths and signatures are read by one probe per release, which runs like an
example in the environment of that release.

A withdrawn notice is not verified: it gets one result that says so, and
nothing is installed or run.

Releases and their upload times come from PyPI's JSON Simple API (PEP 691 and
PEP 700). Each version is installed from PyPI with ``uv`` together with its
dependencies as they were one day after it was uploaded (``--exclude-newer``),
so that later releases of other packages cannot break old versions. Neither
uv's config files nor its ``UV_*`` variables can change where they come from.

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
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from packaging.specifiers import InvalidSpecifier, SpecifierSet
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

PYPI_INDEX = "https://pypi.org/simple"
PYPI_SIMPLE = PYPI_INDEX + "/{name}/"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
USER_AGENT = "breakwise-verify-examples (+https://github.com/breakwise/breakwise)"
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

FAKE_KEY = "breakwise-fake-key"
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

NETWORK_BLOCKED = "breakwise: network access is blocked while running examples"
# Runs an example with the network blocked. It records each network attempt and
# the exception that ends the example, as Python prints it.
GUARD_SCRIPT = f"""\
import runpy
import socket
import sys
import traceback

_example, _network_log, _error_file, _type_file = sys.argv[1:5]


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
    # The type's name as traceback prints it: SyntaxError prints lines of code first.
    kind = type(error)
    name = kind.__qualname__
    if kind.__module__ not in ("builtins", "__main__"):
        name = kind.__module__ + "." + name
    with open(_type_file, "w", encoding="utf-8") as out:
        out.write(name)
    raise
"""

LATEST_NOTE = (
    "This is the latest release, so the library may have changed after breaks_in: "
    "it may have brought the old name back or broken the new code."
)

# Resolves paths and reads signatures with one release installed. It runs like an example,
# so importing the package is offline too. It reads the request from PROBE_REQUEST and
# writes what it found to PROBE_RESULT, both JSON in its working directory.
PROBE_REQUEST = "probe-request.json"
PROBE_RESULT = "probe-result.json"
PROBE_SCRIPT = f"""\
import inspect
import json
import pkgutil
import traceback

EXPLICIT = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def error_of(error):
    return traceback.format_exception_only(error)[-1].strip()


with open({PROBE_REQUEST!r}, encoding="utf-8") as request_file:
    request = json.load(request_file)
paths = {{}}
for path in request["paths"]:
    try:
        pkgutil.resolve_name(path)
    except Exception as error:
        paths[path] = error_of(error)
    else:
        paths[path] = None
signatures = []
for target, keyword in request["signatures"]:
    entry = {{"target": target, "keyword": keyword, "explicit": None, "error": None}}
    try:
        parameter = inspect.signature(pkgutil.resolve_name(target)).parameters.get(keyword)
    except Exception as error:
        entry["error"] = error_of(error)
    else:
        entry["explicit"] = parameter is not None and parameter.kind in EXPLICIT
    signatures.append(entry)
with open({PROBE_RESULT!r}, "w", encoding="utf-8") as result_file:
    json.dump({{"paths": paths, "signatures": signatures}}, result_file)
"""


class VerifyError(Exception):
    """A notice cannot be verified, for example because a version is not on PyPI."""


@dataclass(frozen=True)
class Check:
    """Run one example on one version and expect it to run or to fail."""

    version: Version
    example: str
    should_run: bool
    # On the latest release after breaks_in, a failure may mean that the library changed.
    latest: bool = False

    @property
    def label(self) -> str:
        return f"example.{self.example}"


@dataclass(frozen=True)
class Failure:
    """What the failure of an example must show. An empty field asks for nothing.

    ``named`` must be in the exception, as the name of a rename must. ``error`` is
    the exception's name, and ``message`` a part of the exception, from
    ``example.fails_with``.
    """

    named: str = ""
    error: str = ""
    message: str = ""


@dataclass(frozen=True)
class SignatureCheck:
    """Check that a callable takes a keyword argument as an explicit parameter on one version.

    ``field`` is the field that names the callable and the keyword, such as ``affects[1]``.
    """

    version: Version
    target: str
    keyword: str
    field: str = "rename.keyword_of"

    @property
    def label(self) -> str:
        return f"signature of {self.target}"


@dataclass(frozen=True)
class PathCheck:
    """Check that a path of a rename resolves on one version, or that it doesn't.

    ``field`` is the field the path comes from, such as ``rename.from``, and
    ``where`` says why the version needs it, such as "example.before runs".
    """

    version: Version
    field: str
    path: str
    should_exist: bool
    where: str
    # Said after the failure of a path that should exist, such as what else the change may be.
    hint: str = ""

    @property
    def label(self) -> str:
        return self.field


@dataclass(frozen=True)
class Probe:
    """What the probe found with one release installed, or why it found nothing.

    ``paths`` maps each path to None if it resolves, or else to the error that
    resolving it raised. ``signatures`` maps a callable and a keyword to True if
    the callable takes the keyword as an explicit parameter, to False if not, or
    to the error. ``failure`` says why the probe found nothing, or is "".
    """

    paths: Mapping[str, str | None] = dataclasses.field(default_factory=dict)
    signatures: Mapping[tuple[str, str], bool | str] = dataclasses.field(default_factory=dict)
    failure: str = ""
    used_network: bool = False


@dataclass(frozen=True)
class Run:
    """How an example ended.

    ``returncode`` is None if the example ran too long and was stopped.
    ``error`` is the exception that ended it, as Python prints it, e.g.
    ``ImportError: cannot import name 'X' from 'y'``, or "" if none did, and
    ``error_type`` is the name of its type, as Python prints it there.
    """

    returncode: int | None
    stderr: str = ""
    error: str = ""
    used_network: bool = False
    error_type: str = ""

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
class LatestNote:
    """Why a newer release than the one checked as the latest is passed over, or none is."""

    version: Version

    @property
    def label(self) -> str:
        return "latest release"


@dataclass(frozen=True)
class Withdrawn:
    """A notice that was withdrawn, which is not verified."""

    date: str
    reason: str


@dataclass(frozen=True)
class Result:
    check: Check | SignatureCheck | PathCheck | LatestNote | Withdrawn
    passed: bool
    detail: str


@dataclass(frozen=True)
class Release:
    """When a release was uploaded, and the Requires-Python of each of its files.

    ``requires_python`` holds None for a file that states none.
    """

    uploaded: dt.datetime
    requires_python: frozenset[str | None] = frozenset({None})


Fetcher = Callable[[str], dict[Version, Release]]
Installer = Callable[[str, Version, str, str, Path], Path]


def version_of(filename: str) -> Version | None:
    """Return the version in the name of a wheel or source distribution, or None."""
    try:
        if filename.endswith(".whl"):
            return parse_wheel_filename(filename)[1]
        return parse_sdist_filename(filename)[1]
    except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion):
        return None


def parse_files(files: Iterable[Mapping[str, Any]]) -> dict[Version, Release]:
    """Map every release that has files which are not yanked to its latest upload time.

    ``files`` are the file entries of a project page in PyPI's JSON Simple API.
    Files that are neither wheels nor source distributions, such as eggs, are skipped.
    Each release also keeps the Requires-Python of its files.
    """
    releases: dict[Version, Release] = {}
    for file in files:
        version = version_of(file["filename"])
        uploaded = file.get("upload-time")
        if version is None or not uploaded or file.get("yanked"):
            continue
        upload_time = dt.datetime.fromisoformat(uploaded)
        requires = frozenset({file.get("requires-python") or None})
        if version in releases:
            known = releases[version]
            upload_time = max(upload_time, known.uploaded)
            requires |= known.requires_python
        releases[version] = Release(upload_time, requires)
    return releases


def fetch_releases(
    name: str, attempts: int = PYPI_ATTEMPTS, sleep: Callable[[float], None] = time.sleep
) -> dict[Version, Release]:
    """Return the releases of a package on PyPI, when they were uploaded, and their Python.

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
            # The error holds PyPI's response, so it must be closed like one.
            error.close()
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
    releases: Mapping[Version, Release],
    breaks_in: Version,
    works_since: Version | None,
    latest: Version | None = None,
    restored_in: Version | None = None,
) -> list[Check]:
    """Decide which examples to run on which versions.

    ``latest`` is the latest release after ``breaks_in`` to check, from latest_release().
    """
    fields = (("breaks_in", breaks_in), ("works_since", works_since), ("restored_in", restored_in))
    for field, version in fields:
        if version is not None and version not in releases:
            raise VerifyError(f"{field} {version} is not a release on PyPI, or it was yanked.")
    earlier = [v for v in releases if v < breaks_in]
    stable = [v for v in earlier if not v.is_prerelease]
    if not stable:
        raise VerifyError(f"There is no stable release before breaks_in {breaks_in}.")
    previous = max(stable)

    checks = [Check(previous, "before", should_run=True)]
    if works_since is not None and works_since <= previous:
        checks.append(Check(previous, "after", should_run=True))
    # breaks_in must be the first release that breaks, even after pre-releases.
    if max(earlier) != previous:
        checks.append(Check(max(earlier), "before", should_run=True))
    checks += [
        Check(breaks_in, "before", should_run=False),
        Check(breaks_in, "after", should_run=True),
    ]
    if works_since is not None:
        if works_since not in (previous, breaks_in):
            checks.append(Check(works_since, "after", should_run=True))
        # works_since must be the first release on which the new code runs.
        older = [v for v in releases if v < works_since]
        if older:
            checks.append(Check(max(older), "after", should_run=False))
    checks += _restored_checks(releases, breaks_in, restored_in)
    # The library may change after breaks_in, for example by bringing the old name back.
    checks += _latest_checks(checks, latest, restored_in)
    return checks


def _restored_checks(
    releases: Mapping[Version, Release], breaks_in: Version, restored_in: Version | None
) -> list[Check]:
    """``example.before`` runs again on ``restored_in``, and fails on the release just before it."""
    if restored_in is None:
        return []
    checks = []
    just_before = max(v for v in releases if v < restored_in)
    # breaks_in is checked already.
    if just_before != breaks_in:
        checks.append(Check(just_before, "before", should_run=False))
    return [
        *checks,
        Check(restored_in, "before", should_run=True),
        Check(restored_in, "after", should_run=True),
    ]


def _latest_checks(
    planned: Sequence[Check], latest: Version | None, restored_in: Version | None
) -> list[Check]:
    """On the latest release, ``example.before`` fails, or runs from ``restored_in`` on.

    ``example.after`` runs there. An example that is planned on that release already
    is not run twice.
    """
    if latest is None:
        return []
    restored = restored_in is not None and latest >= restored_in
    planned_there = {check.example for check in planned if check.version == latest}
    return [
        Check(latest, example, should_run, latest=True)
        for example, should_run in (("before", restored), ("after", True))
        if example not in planned_there
    ]


def latest_release(
    releases: Mapping[Version, Release], breaks_in: Version, python: Version
) -> tuple[Version | None, Result | None]:
    """Return the latest release after ``breaks_in`` to check, and a note when one is passed over.

    That is the latest stable release, or the latest of any kind when ``breaks_in``
    is a pre-release and no stable release follows it. It must accept ``python``,
    the Python that runs the examples, so a newer release that doesn't is passed over.
    """
    newer = sorted((v for v in releases if v > breaks_in), reverse=True)
    stable = [v for v in newer if not v.is_prerelease]
    candidates = stable if stable or not breaks_in.is_prerelease else newer
    chosen = next((v for v in candidates if _accepts(releases[v], python)), None)
    if not candidates or candidates[0] == chosen:
        return chosen, None
    passed_over = candidates[0]
    requires = f"{passed_over} requires Python {_requirement(releases[passed_over])}"
    if chosen is None:
        detail = f"not checked: no release after {breaks_in} accepts Python {python}; {requires}"
    else:
        detail = f"{requires}, so the latest release checked is {chosen}"
    return chosen, Result(LatestNote(passed_over), True, detail)


def _accepts(release: Release, python: Version) -> bool:
    """Whether some file of a release accepts ``python``, as uv decides which file to use."""
    return any(spec is None or _spec_accepts(spec, python) for spec in release.requires_python)


def _spec_accepts(spec: str, python: Version) -> bool:
    try:
        return SpecifierSet(spec).contains(python, prereleases=True)
    except InvalidSpecifier:
        # A Requires-Python that can't be read doesn't stop the release from being checked.
        return True


def _requirement(release: Release) -> str:
    return " or ".join(sorted(spec for spec in release.requires_python if spec))


def exclude_newer(uploaded: dt.datetime) -> str:
    """Return the cut-off for dependencies of a release, in RFC 3339 (UTC)."""
    cutoff = (uploaded + DEPENDENCY_WINDOW).astimezone(dt.UTC)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


@functools.cache
def examples_python(python: str) -> Version:
    """Return the version of the Python that runs the examples, asked once for each run.

    ``--python`` may be a path or a request such as ``3.12``, so uv makes an
    environment with it, as it does for the examples, and its interpreter tells.
    """
    with tempfile.TemporaryDirectory(prefix="breakwise-python-") as tmp_name:
        target = Path(tmp_name) / "venv"
        _run_tool([_uv(), "venv", "--no-config", "--quiet", "--python", python, str(target)])
        code = "import platform; print(platform.python_version())"
        output = _run_tool([str(target / "bin" / "python"), "-I", "-c", code]).strip()
    try:
        return Version(output)
    except InvalidVersion as error:
        raise VerifyError(f"Python {python} gave {output!r} as its version.") from error


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise VerifyError("uv is not installed. Install requirements-dev.txt first.")
    return uv


def install(package: str, version: Version, cutoff: str, python: str, target: Path) -> Path:
    """Create a virtual environment with one version of a package. Return its interpreter.

    The package comes from PyPI, where the list of its releases comes from.
    """
    uv = _uv()
    # --no-config: uv settings on this machine must not change what gets installed.
    _run_tool([uv, "venv", "--no-config", "--quiet", "--python", python, str(target)])
    interpreter = target / "bin" / "python"
    install_command = [uv, "pip", "install", "--no-config", "--quiet", "--python", str(interpreter)]
    source = ["--default-index", PYPI_INDEX, "--exclude-newer", cutoff]
    _run_tool([*install_command, *source, f"{package}=={version}"])
    return interpreter


# uv also reads settings from UV_* variables, which --no-config doesn't stop. Some of
# them change what gets installed or from where, such as UV_DEFAULT_INDEX, UV_INDEX_URL
# and UV_EXTRA_INDEX_URL, so the tools run without any UV_* variable but these. They only
# say where uv keeps its files and how it connects, which some machines need.
UV_VARIABLES_KEPT = frozenset(
    {"UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_NATIVE_TLS", "UV_HTTP_TIMEOUT"}
)


def tool_environment() -> dict[str, str]:
    """This process's environment without the uv variables that could change an install.

    Everything else stays, such as proxies and certificates.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("UV_") or name in UV_VARIABLES_KEPT
    }


def _run_tool(command: list[str]) -> str:
    """Run a tool and return what it printed."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed tool, arguments from the notice schema
            command,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
            check=False,
            env=tool_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise VerifyError(f"Timed out: {' '.join(command)}") from error
    if completed.returncode != 0:
        raise VerifyError(f"Failed: {' '.join(command)}\n{completed.stderr.strip()}")
    return completed.stdout


def run_example(
    interpreter: Path, code: str, workdir: Path, timeout: float = EXAMPLE_TIMEOUT
) -> Run:
    """Run example code offline, with fake API keys and nothing else from the environment."""
    workdir.mkdir(exist_ok=True)
    example = workdir / "example.py"
    guard = workdir / "run_example.py"
    network_log = workdir / "network.log"
    error_file = workdir / "error.txt"
    type_file = workdir / "error-type.txt"
    example.write_text(code, encoding="utf-8")
    guard.write_text(GUARD_SCRIPT, encoding="utf-8")
    env = {"PATH": os.defpath, "HOME": str(workdir), "LANG": "C.UTF-8"}
    env.update(dict.fromkeys(FAKE_KEY_VARIABLES, FAKE_KEY))
    # -I ignores PYTHON* variables and user site-packages; -X utf8 fixes the output encoding.
    command = [str(interpreter), "-I", "-X", "utf8", str(guard), str(example)]
    with subprocess.Popen(  # noqa: S603 - runs the notice's example, which is the point
        [*command, str(network_log), str(error_file), str(type_file)],
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
    error_type = type_file.read_text(encoding="utf-8") if type_file.exists() else ""
    return Run(process.returncode, stderr, error, network_log.exists(), error_type)


def _stop(process: subprocess.Popen[str]) -> None:
    """Kill a process and every process it started, then collect what is left of its output."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.communicate(timeout=STOPPED_OUTPUT_TIMEOUT)


def evaluate(  # noqa: PLR0911 - one return for each way an example can end
    check: Check, run: Run, expected: Failure | None = None
) -> Result:
    """Compare how an example ended with what the notice says should happen.

    A failure only counts as expected if it shows what ``expected`` asks for.
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
    mismatch = _mismatch(expected, run) if expected is not None else None
    if mismatch is not None:
        return Result(check, False, f"{mismatch}: {run.summary}")
    return Result(check, True, f"fails as expected: {run.summary}")


def _mismatch(expected: Failure, run: Run) -> str | None:
    """How a failure differs from what is expected of it, or None."""
    if expected.named and expected.named not in run.error:
        return f"fails, but not because of {expected.named}"
    name = exception_name(run)
    if expected.error and name != expected.error:
        return f"fails, but with {name or 'no exception'} instead of {expected.error}"
    if expected.message and expected.message not in run.error:
        return f"fails with {name}, but its message doesn't contain {expected.message!r}"
    return None


def exception_name(run: Run) -> str:
    """The name of the exception's type, as Python prints it, or "" without an exception.

    That is the type's qualified name, after its module unless the module is
    ``builtins`` or ``__main__``, such as ``TypeError`` or ``pydantic_ai.exceptions.UserError``.
    """
    return run.error_type


def plan_path_checks(rename: Mapping[str, Any], checks: Sequence[Check]) -> list[PathCheck]:
    """Decide on which versions the paths of a rename must resolve, or must not.

    ``rename.from`` resolves where ``example.before`` runs and not where it must
    fail, and ``rename.to`` resolves where ``example.after`` runs. Each callable
    of a renamed keyword argument resolves where an example runs.
    """
    plan: dict[Version, dict[str, bool]] = {}
    for check in checks:
        plan.setdefault(check.version, {})[check.example] = check.should_run
    path_checks = []
    for version, examples in plan.items():
        if "keyword_of" in rename:
            path_checks += _callables_where_examples_run(version, examples, rename["keyword_of"])
            continue
        before, after = examples.get("before"), examples.get("after")
        if before is not None:
            where = "example.before runs" if before else "example.before must fail"
            path_checks.append(PathCheck(version, "rename.from", rename["from"], before, where))
        if after:
            where = "example.after runs"
            path_checks.append(PathCheck(version, "rename.to", rename["to"], True, where))
    return path_checks


REMOVAL_HINT = "If it was removed, the type may be removal."


def plan_affects_checks(
    kind: str, affects: Sequence[str | Mapping[str, str]], checks: Sequence[Check]
) -> list[PathCheck]:
    """Decide on which versions the paths in ``affects`` must resolve, or must not.

    Every path resolves where ``example.before`` runs. Where it must fail, the path
    alone of a removal is gone, like ``rename.from``. Any other path still resolves
    there, because the change is in how the code uses it.
    """
    path_checks = []
    for check in checks:
        if check.example != "before":
            continue
        for index, item in enumerate(affects):
            path = item if isinstance(item, str) else item["path"]
            field = f"affects[{index}]"
            if check.should_run:
                where = "example.before runs"
                path_checks.append(PathCheck(check.version, field, path, True, where))
            elif kind == "removal" and isinstance(item, str):
                where = "example.before must fail"
                path_checks.append(PathCheck(check.version, field, path, False, where))
            else:
                where = "example.before must fail"
                path_checks.append(
                    PathCheck(check.version, field, path, True, where, hint=REMOVAL_HINT)
                )
    return path_checks


def plan_affects_signatures(
    affects: Sequence[str | Mapping[str, str]], checks: Sequence[Check]
) -> list[SignatureCheck]:
    """Each keyword in ``affects`` is an explicit parameter where ``example.before`` runs."""
    return [
        SignatureCheck(check.version, item["path"], item["keyword"], f"affects[{index}]")
        for check in checks
        if check.example == "before" and check.should_run
        for index, item in enumerate(affects)
        if not isinstance(item, str)
    ]


def _callables_where_examples_run(
    version: Version, examples: Mapping[str, bool], callables: Sequence[str]
) -> list[PathCheck]:
    running = [example for example, runs in examples.items() if runs]
    if not running:
        return []
    where = "both examples run" if len(running) > 1 else f"example.{running[0]} runs"
    return [PathCheck(version, "rename.keyword_of", path, True, where) for path in callables]


def run_probe(
    interpreter: Path,
    paths: Sequence[str],
    signatures: Sequence[tuple[str, str]],
    workdir: Path,
    timeout: float = EXAMPLE_TIMEOUT,
) -> Probe:
    """Resolve paths and read signatures with one release installed, in one run like an example."""
    workdir.mkdir()
    request = {"paths": list(paths), "signatures": [list(pair) for pair in signatures]}
    (workdir / PROBE_REQUEST).write_text(json.dumps(request), encoding="utf-8")
    run = run_example(interpreter, PROBE_SCRIPT, workdir, timeout)
    if run.returncode is None:
        return Probe(failure="did not finish in time, so it was stopped")
    if run.returncode != 0:
        return Probe(failure=f"could not be checked: {run.summary}")
    try:
        found = json.loads((workdir / PROBE_RESULT).read_text(encoding="utf-8"))
        return Probe(
            dict(found["paths"]),
            {
                (entry["target"], entry["keyword"]): entry["error"] or entry["explicit"]
                for entry in found["signatures"]
            },
            used_network=run.used_network,
        )
    except FileNotFoundError:
        return Probe(failure="could not be checked: the probe wrote no results")
    except (ValueError, KeyError, TypeError, AttributeError):
        return Probe(failure="could not be checked: the probe wrote results that are not valid")


def evaluate_path(  # noqa: PLR0911 - one return for each way a path check can end
    check: PathCheck, probe: Probe
) -> Result:
    """Compare what the probe found for a path with what the rename needs."""
    if probe.failure:
        return Result(check, False, probe.failure)
    if check.path not in probe.paths:
        return Result(check, False, "could not be checked: the probe has no result for it")
    error = probe.paths[check.path]
    # The report shows the field, such as rename.from, before the message.
    on = f"on {check.version}, where {check.where}"
    if check.should_exist:
        if error is None:
            return Result(check, True, f"{check.path} exists")
        hint = f". {check.hint}" if check.hint else ""
        return Result(check, False, f"{check.path} does not exist {on} ({error}){hint}")
    if error is None:
        return Result(check, False, f"{check.path} still exists {on}")
    if probe.used_network:
        # A path that fails to resolve after a blocked network call isn't proven to be gone.
        message = f"could not be checked: it failed after a network call was blocked ({error})"
        return Result(check, False, message)
    return Result(check, True, f"{check.path} is gone")


def evaluate_signature(check: SignatureCheck, probe: Probe) -> Result:
    """Compare what the probe found for a signature with what the notice says."""
    if probe.failure:
        return Result(check, False, probe.failure)
    found = probe.signatures.get((check.target, check.keyword))
    if found is True:
        return Result(check, True, f"takes {check.keyword} as an explicit parameter")
    if found is False:
        fix = (
            f"remove {check.target} from rename.keyword_of"
            if check.field == "rename.keyword_of"
            else f"change {check.field}"
        )
        message = (
            f"has no explicit parameter {check.keyword}, and taking it through **kwargs "
            f"does not count. Check the versions, or {fix}."
        )
        return Result(check, False, message)
    if found is None:
        return Result(check, False, "could not be checked: the probe has no result for it")
    return Result(check, False, f"could not be checked: {found}")


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
    python_version: Version | None = None,
) -> list[Result]:
    """Install the versions a notice needs and run its examples on them.

    ``python_version`` is the version of ``python``. Without it, examples_python() finds it.
    """
    notice = _read_valid_notice(path)
    if "withdrawn" in notice:
        withdrawn = Withdrawn(notice["withdrawn"]["date"], notice["withdrawn"]["reason"])
        detail = f"withdrawn on {withdrawn.date}: {withdrawn.reason.rstrip('.')}. Not verified."
        return [Result(withdrawn, True, detail)]
    name = notice["package"]["name"]
    breaks_in = Version(notice["breaks_in"])
    works_since = Version(notice["works_since"]) if "works_since" in notice else None
    restored_in = Version(notice["restored_in"]) if "restored_in" in notice else None
    expected = expected_failures(notice)

    releases = fetch(name)
    python_version = python_version or examples_python(python)
    latest, note = latest_release(releases, breaks_in, python_version)
    checks = plan_checks(releases, breaks_in, works_since, latest, restored_in)
    signature_versions = [v for v in (breaks_in, works_since, latest) if v is not None]
    path_checks, signature_checks = plan_probe(notice, checks, signature_versions)
    results = []
    with tempfile.TemporaryDirectory(prefix="breakwise-") as tmp_name:
        tmp = Path(tmp_name).resolve()
        interpreters: dict[Version, Path] = {}

        def interpreter(version: Version) -> Path:
            if version not in interpreters:
                cutoff = exclude_newer(releases[version].uploaded)
                target = tmp / f"venv-{version}"
                interpreters[version] = installer(name, version, cutoff, python, target)
            return interpreters[version]

        for number, check in enumerate(checks):
            code = notice["example"][check.example]
            run = _shorten_paths(
                run_example(interpreter(check.version), code, tmp / f"run-{number}"), tmp
            )
            results.append(evaluate(check, run, expected[check.example]))
        # One probe for each version reads every path and signature that the version needs.
        probes: dict[Version, Probe] = {}
        versions = [c.version for c in path_checks] + [s.version for s in signature_checks]
        for number, version in enumerate(dict.fromkeys(versions)):
            paths = [check.path for check in path_checks if check.version == version]
            signatures = [(s.target, s.keyword) for s in signature_checks if s.version == version]
            probe = run_probe(
                interpreter(version),
                list(dict.fromkeys(paths)),
                signatures,
                tmp / f"probe-{number}",
            )
            probes[version] = _shorten_probe(probe, tmp)
        results += [evaluate_path(check, probes[check.version]) for check in path_checks]
        results += [evaluate_signature(check, probes[check.version]) for check in signature_checks]
    on_latest = {check.version for check in checks if check.latest}
    results = [_on_latest(r) if _failed_on(r, on_latest) else r for r in results]
    return [*results, note] if note else results


def _failed_on(result: Result, versions: Collection[Version]) -> bool:
    """Whether a check failed on one of ``versions``."""
    return (
        not result.passed
        and not isinstance(result.check, Withdrawn)
        and result.check.version in versions
    )


def expected_failures(notice: Mapping[str, Any]) -> dict[str, Failure]:
    """What the failure of each example must show.

    For a rename, the exception names the name that the example uses, so that a
    failure for another reason doesn't count. ``example.fails_with`` adds the
    exception's name and a part of its message, for every type.
    """
    named = {"before": "", "after": ""}
    if notice["type"] == "rename":
        rename = notice["rename"]
        if "keyword_of" in rename:
            named = {"before": rename["from"], "after": rename["to"]}
        else:
            old, new = distinguishing_names(rename["from"], rename["to"])
            named = {"before": old, "after": new}
    fails_with = notice["example"].get("fails_with", {})
    return {
        example: Failure(
            named[example],
            fails_with.get(example, {}).get("error", ""),
            fails_with.get(example, {}).get("message", ""),
        )
        for example in ("before", "after")
    }


def plan_probe(
    notice: Mapping[str, Any], checks: Sequence[Check], signature_versions: Sequence[Version]
) -> tuple[list[PathCheck], list[SignatureCheck]]:
    """The paths and the signatures that the probes check, for a rename or for ``affects``.

    The callables of a renamed keyword argument take the new keyword on
    ``signature_versions``.
    """
    if notice["type"] != "rename":
        affects = notice["affects"]
        return (
            plan_affects_checks(notice["type"], affects, checks),
            plan_affects_signatures(affects, checks),
        )
    rename = notice["rename"]
    signatures = [
        SignatureCheck(version, target, rename["to"])
        for version in dict.fromkeys(signature_versions)
        for target in rename.get("keyword_of", [])
    ]
    return plan_path_checks(rename, checks), signatures


def _on_latest(result: Result) -> Result:
    """Say what a failure on the latest release after breaks_in may mean."""
    return dataclasses.replace(result, detail=f"{result.detail.rstrip('.')}. {LATEST_NOTE}")


def _shorten_paths(run: Run, tmp: Path) -> Run:
    """Temporary paths in error messages are noise, so they are shortened."""
    return dataclasses.replace(
        run,
        stderr=run.stderr.replace(str(tmp), "<tmp>"),
        error=run.error.replace(str(tmp), "<tmp>"),
    )


def _shorten_probe(probe: Probe, tmp: Path) -> Probe:
    def shorten(text: str) -> str:
        return text.replace(str(tmp), "<tmp>")

    return dataclasses.replace(
        probe,
        paths={path: error and shorten(error) for path, error in probe.paths.items()},
        signatures={
            key: found if isinstance(found, bool) else shorten(found)
            for key, found in probe.signatures.items()
        },
        failure=shorten(probe.failure),
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
            if isinstance(result.check, Withdrawn):
                # Nothing was run, so there is no version or field to show.
                print(f"  skip  {result.detail}")
                continue
            status = "ok  " if result.passed else "FAIL"
            if isinstance(result.check, LatestNote):
                status = "note"
            what = f"{result.check.version}  {result.check.label}"
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
