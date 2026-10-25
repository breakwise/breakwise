"""Tests for the repository's own settings, such as .gitignore and the released schemas."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Released notice formats never change: a change goes into the next format version.
RELEASED_SCHEMAS = {
    "schema/notice-0.1.schema.json": (
        "38d4785467ab0515960e9acb32fe53c46d5b3ae439c4b571b6c1d52046b7478c"
    ),
    "schema/notice-0.2.schema.json": (
        "25271d2dd195944b7cf299034f628d48e32c8853b882d253831f5055ce231773"
    ),
}


@pytest.mark.parametrize(("schema", "sha256"), RELEASED_SCHEMAS.items())
def test_released_schemas_never_change(schema: str, sha256: str) -> None:
    digest = hashlib.sha256((REPO_ROOT / schema).read_bytes()).hexdigest()
    assert digest == sha256, f"{schema} changed. Put the change in a new format version instead."


# Real PyPI packages named like folders that .gitignore ignores, such as build/ and lib/.
@pytest.mark.parametrize("package", ["build", "dist", "lib", "env", "wheels"])
def test_notices_are_never_ignored(package: str) -> None:
    notice = f"notices/pypi/{package}/BW-2026-0001.yaml"
    # --no-index checks the path as git sees a new file, whether or not it is tracked.
    completed = subprocess.run(
        ["git", "check-ignore", "--no-index", notice],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 128, f"git could not check {notice}: {completed.stderr}"
    # git check-ignore exits with 1 when the path is not ignored.
    assert completed.returncode == 1, (
        f".gitignore ignores {notice}. Keep !notices/** as the last line of .gitignore."
    )
