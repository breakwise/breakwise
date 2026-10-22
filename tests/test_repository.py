"""Tests for the repository's own settings, such as .gitignore."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


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
