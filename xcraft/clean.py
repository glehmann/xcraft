"""Cleaning: removing what xcraft can regenerate."""

from __future__ import annotations

import math
import shutil
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, TypeAdapter

from xcraft import process
from xcraft.container import BUILD_IMAGE, SIGN_IMAGE
from xcraft.localrepo import OUTPUT_MARKER
from xcraft.resolution import resolutions_read
from xcraft.sources import read_manifest
from xcraft.specs import specs_read

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


def _other_isos(project: Project, isos: set[str]) -> list[Path]:
    """Return the ISOs of the platform but those of the keys `isos`: their cache, and those in .xcraft/iso/, but the
    hard links to the ISOs kept.
    """
    trees = list((project.state / "cache/isos" / project.arch).glob("*"))
    kept = {(t / "image.iso").stat().st_ino for t in trees if t.name in isos and (t / "image.iso").exists()}
    named = [iso for iso in (project.state / "iso").glob("*.iso") if iso.stat().st_ino not in kept]
    return [*(t for t in trees if t.name not in isos), *named]


def _shared(project: Project, isos: set[str] | None, *, sources: bool, used_since: float) -> list[Path]:
    """Return what the packages share that clean() removes, see there."""
    state = project.state
    paths = [state / "build", state / "tmp", state / "cache/extra"]
    paths += [state / "cache" / cache for cache in ("checks", "upstream", "rpms", "srpms")]
    # but those of the specs and resolutions this process read: the current ones, once the project is prepared
    current = specs_read() | resolutions_read()
    paths += [
        p
        for d in ("specs", "resolved")
        for p in (state / "cache" / d / project.arch).glob("*.json")
        if p not in current and p.stat().st_mtime < used_since
    ]
    if isos is not None:
        used = {t.name for t in (state / "cache/isos" / project.arch).glob("*") if t.stat().st_mtime >= used_since}
        paths += _other_isos(project, isos | used)
    # but those of the steps, like _resolve.log
    paths += [log for log in (state / "logs").glob("[!_]*.log") if log.stem not in project.package_dirs]
    if sources:
        manifests = [d / "sources" for d in project.package_dirs.values() if (d / "sources").exists()]
        had = {digest for manifest in manifests for digest in read_manifest(manifest).values()}
        paths += [d for d in project.source_cache.glob("*") if d.name not in had and d.stat().st_mtime < used_since]
    return paths


class _Image(BaseModel):
    names: list[str] = Field([], alias="Names")
    created: float = Field(alias="Created")


def remove_images(kept: set[str], used_since: float = math.inf) -> list[str]:
    """Remove the build environment and signing images of the machine, but the tags `kept` and those built since the
    time `used_since`, and return the tags removed. Those of running containers stay.
    """
    listed = process.run(
        ["podman", "images", "--format", "json", "--filter", f"reference={BUILD_IMAGE}", "--filter",
         f"reference={SIGN_IMAGE}"],
        capture=True,
    ).stdout  # fmt: skip
    tags = [
        tag
        for image in TypeAdapter(list[_Image]).validate_json(listed)
        if image.created < used_since
        for tag in image.names
        if tag.startswith((f"{BUILD_IMAGE}:", f"{SIGN_IMAGE}:")) and tag not in kept
    ]
    return [tag for tag in tags if not process.run(["podman", "rmi", tag], check=False, capture=True).returncode]


def clean(
    project: Project,
    keys: list[dict[str, str]],
    names: list[str] | None = None,
    *,
    all_builds: bool = False,
    ccache: bool = False,
    dune_cache: bool = False,
    sources: bool = False,
    isos: set[str] | None = None,
    used_since: float = math.inf,
) -> int:
    """Remove what xcraft can regenerate, and return how many bytes that freed.

    That is the build trees, the outdated builds of the platform, those of none of the `keys`, signed or not, all of
    them with `all_builds`, the compiler caches with `ccache`, and those of dune with `dune_cache`, of the packages
    `names`, or of all of them with what they share: the metadata of the specs and the resolutions of the platform but
    those this process read, the results of the checks, the requirements of the upstream packages, and the upstream
    packages downloaded, and the ISOs of the platform but those of the keys `isos`, if given: those in .xcraft/iso/ too,
    the hard links to their cache, and the copies, see xcraft.iso, the logs of the packages removed from the project,
    and with `sources`, the source archives none of its packages has. A signed build removed is fetched again from the
    remote storage, where publishing put it, or else signed again.

    The outdated builds, ISOs, specs, resolutions and source archives used since the time `used_since` are kept: those
    xcraft read last then, see the os.utime() of their uses.

    The local repository and snapshot, made again when needed, are removed in both cases: they have hard links to the
    files of the builds, which would keep using the space.
    """
    state = project.state
    before = _disk_usage(state)
    # the builds, and the signed ones, by signer
    trees = [state / "cache/builds" / project.arch, *(state / "cache/signed").glob(f"*/{project.arch}")]
    paths = [state / "repo", state / "publish"]
    if names is None:
        paths += _shared(project, isos, sources=sources, used_since=used_since)
        packages = [pkg for tree in trees for pkg in tree.glob("*")]
    else:
        paths += [state / "build" / name for name in names]
        packages = [tree / name for tree in trees for name in names if (tree / name).exists()]
    if all_builds:
        paths += packages
    else:
        paths += [
            build
            for pkg in packages
            for build in pkg.iterdir()
            if build.name not in {k.get(pkg.name) for k in keys} and build.stat().st_mtime < used_since
        ]
    for cache, wanted in (("ccache", ccache), ("dune-cache", dune_cache)):
        if wanted:
            paths += [state / cache] if names is None else [state / cache / name for name in names]
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for pkg in (pkg for tree in trees for pkg in tree.glob("*")):
        if not any(pkg.iterdir()):
            pkg.rmdir()
    return before - _disk_usage(state)
