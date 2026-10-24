"""Test and pre builds: the release suffixes of their packages, and those in the cache."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xcraft.cache import build_keys, builds_dir
from xcraft.project import write_atomically

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project


def next_test_suffixes(project: Project, names: list[str], kind: str, ident: str) -> dict[str, str]:
    """Return the release suffixes of the next test or pre builds of `names`, numbered by `ident` and release.

    A test build is above its release and below the next one, and a pre build
    below its release. The numbers are kept with the project's generated files,
    across `clean`.
    """
    path = project.state / "test-builds.json"
    counters: dict[str, int] = json.loads(path.read_text()) if path.exists() else {}
    suffixes = {}
    for name in names:
        package = project.packages[name]
        release = package.binaries[0].evr.removesuffix(package.dist) if package.binaries else ""
        counter = f"{name} {release} {kind} {ident}"
        counters[counter] = counters.get(counter, 0) + 1
        suffixes[name] = f".0.{ident}.{counters[counter]}" if kind == "test" else f"~{ident}.{counters[counter]}"
    path.parent.mkdir(exist_ok=True)
    write_atomically(path, json.dumps(counters, indent=1, sort_keys=True) + "\n")
    return suffixes


_TEST_SUFFIX = re.compile(r"^(?:\.0\.(?P<test>[a-zA-Z0-9]+)|~(?P<pre>[a-zA-Z0-9]+))\.(?P<number>\d+)$")


@dataclass
class TestBuild:
    """A cached test or pre build of a package."""

    kind: str
    ident: str
    number: int
    built: float
    """When it was built, as a timestamp."""
    current: bool
    """Whether it is built from the current spec and dependencies."""


def test_builds(project: Project, name: str, hashes: dict[str, str]) -> list[TestBuild]:
    """Return the latest cached test or pre build of `name` for each ID, the newest first, current or not with the
    `hashes` of the package directories (see xcraft.cache.inputs()).
    """  # noqa: D205
    latest: dict[tuple[str, str], tuple[TestBuild, Path]] = {}
    for marker in builds_dir(project, name).glob("[!.]*/autorev"):
        if not (m := _TEST_SUFFIX.match(json.loads(marker.read_text())[name])):
            continue
        kind = "test" if m["test"] else "pre"
        built = (marker.parent / "build.log").stat().st_mtime
        # the numbers start again with each release
        if (kind, m[kind]) not in latest or built > latest[kind, m[kind]][0].built:
            latest[kind, m[kind]] = TestBuild(kind, m[kind], int(m["number"]), built, current=False), marker
    for build, marker in latest.values():
        build.current = build_keys(project, json.loads(marker.read_text()), hashes)[name] == marker.parent.name
    return sorted((build for build, _ in latest.values()), key=lambda build: -build.built)
