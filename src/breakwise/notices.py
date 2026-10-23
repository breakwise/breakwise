"""The notices that come with the package, read without network access."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

import yaml

from breakwise import __version__

# Building the package copies the registry's notices/ folder here.
DATA: Traversable = resources.files("breakwise") / "data" / "notices"
REPOSITORY = "https://github.com/breakwise/breakwise"
# The pattern of "id" in the notice schema, character for character. A test keeps them equal.
NOTICE_ID = re.compile(r"^BW-[0-9]{4}-(?!0000$)([0-9]{4}|[1-9][0-9]{4,})$")


class NoticeError(Exception):
    """A notice that can't be found."""


@dataclass(frozen=True)
class Notice:
    """A notice and its place in the registry, such as ``pypi/pydantic-ai/BW-2026-0001.yaml``."""

    data: dict[str, Any]
    path: str

    @property
    def url(self) -> str:
        """The notice's file on GitHub."""
        return f"{REPOSITORY}/blob/main/notices/{self.path}"


def find(notice_id: str, root: Traversable | None = None) -> Notice:
    """Return a notice by its ID, from ``root`` or else from the notices in the package."""
    if not NOTICE_ID.fullmatch(notice_id.upper()):
        raise NoticeError(f"{notice_id!r} is not a notice ID. IDs look like BW-2026-0001.")
    notice_id = notice_id.upper()
    root = DATA if root is None else root
    if not root.is_dir():
        raise NoticeError("The notices that come with breakwise are missing. Reinstall breakwise.")
    # A notice's file name is its ID, under notices/<ecosystem>/<package>/.
    for ecosystem in root.iterdir():
        if not ecosystem.is_dir():
            continue
        for package in ecosystem.iterdir():
            file = package / f"{notice_id}.yaml"
            if file.is_file():
                data = yaml.safe_load(file.read_text(encoding="utf-8"))
                return Notice(data, f"{ecosystem.name}/{package.name}/{file.name}")
    raise NoticeError(
        f"There is no notice {notice_id} in the notices that come with breakwise {__version__}. "
        "A newer release may have it."
    )
