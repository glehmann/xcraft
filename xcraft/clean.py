"""Cleaning: removing what xcraft can regenerate."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from xcraft.localrepo import OUTPUT_MARKER

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project


def repos_disk_usage(project: Project) -> int:
    """Return the disk usage of the files only the repositories of xcraft repo, repo*/, have."""
    links: dict[int, tuple[int, int, int]] = {}  # inode: links in repo*/, links, blocks
    for repo in (r for r in project.root.glob("repo*/") if (r / OUTPUT_MARKER).exists()):
        for f in repo.rglob("*"):
            if (stat := f.lstat()) and f.is_file():
                count = links.get(stat.st_ino, (0, 0, 0))[0]
                links[stat.st_ino] = (count + 1, stat.st_nlink, stat.st_blocks)
    return sum(blocks * 512 for count, nlink, blocks in links.values() if count == nlink)


def _disk_usage(path: Path) -> int:
    """Return the disk usage of `path`, counting hard links once."""
    inodes = {(stat.st_ino, stat.st_blocks) for f in path.rglob("*") if (stat := f.lstat()) and f.is_file()}
    return sum(blocks * 512 for _, blocks in inodes)


def clean(
    project: Project,
    keys: list[dict[str, str]],
    names: list[str] | None = None,
    *,
    all_builds: bool = False,
    ccache: bool = False,
) -> int:
    """Remove what xcraft can regenerate, and return how many bytes that freed.

    That is the build trees, the outdated builds of the platform, those of none
    of the `keys`, all of them with `all_builds`, and the compiler caches with `ccache`, of the packages
    `names`, or of all of them with what they share. The source cache is kept.

    The local repository and snapshot, made again when needed, are removed in
    both cases: they have hard links to the files of the builds, which would
    keep using the space.
    """
    state = project.state
    before = _disk_usage(state)
    builds = state / "cache/builds" / project.arch
    paths = [state / "repo", state / "publish"]
    if names is None:
        paths += [state / "build", state / "tmp", state / "cache/extra"]
        specs = sorted((state / "cache/specs" / project.arch).glob("*.json"), key=lambda path: path.stat().st_mtime)
        paths += specs[:-1]  # but the metadata of the current specs, the latest
        packages = list(builds.glob("*"))
    else:
        paths += [state / "build" / name for name in names]
        packages = [builds / name for name in names if (builds / name).exists()]
    if all_builds:
        paths += packages
    else:
        paths += [
            build for pkg in packages for build in pkg.iterdir() if build.name not in {k.get(pkg.name) for k in keys}
        ]
    if ccache:
        paths += [state / "ccache"] if names is None else [state / "ccache" / name for name in names]
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for pkg in builds.glob("*"):
        if not any(pkg.iterdir()):
            pkg.rmdir()
    return before - _disk_usage(state)
