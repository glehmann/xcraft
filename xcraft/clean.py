"""Cleaning: removing what xcraft can regenerate.

The removals are what to remove, by what it is, like "outdated builds", selected the same for a dry run: see freed()
and remove().
"""

from __future__ import annotations

import math
import shutil
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, TypeAdapter

from xcraft import process
from xcraft.container import BUILD_IMAGE, SIDE_IMAGES, side_name
from xcraft.localrepo import OUTPUT_MARKER
from xcraft.resolution import resolutions_read
from xcraft.sources import read_manifest
from xcraft.specs import specs_read

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project


def freed(paths: list[Path]) -> int:
    """Return the disk usage of the files only `paths` have, all their hard links there: what removing them frees."""
    files = {f for p in paths for f in (p.rglob("*") if p.is_dir() and not p.is_symlink() else [p])}
    links: dict[int, tuple[int, int, int]] = {}  # inode: links in `paths`, links, blocks
    for f in files:
        if (stat := f.lstat()) and f.is_file() and not f.is_symlink():
            links[stat.st_ino] = (links.get(stat.st_ino, (0, 0, 0))[0] + 1, stat.st_nlink, stat.st_blocks)
    return sum(blocks * 512 for count, nlink, blocks in links.values() if count == nlink)


def repos_disk_usage(project: Project) -> int:
    """Return the disk usage of the files only the repositories of xcraft repo, repo*/, have."""
    return freed([r for r in project.root.glob("repo*/") if (r / OUTPUT_MARKER).exists()])


def _existing(removal: dict[str, list[Path]]) -> dict[str, list[Path]]:
    return {what: found for what, paths in removal.items() if (found := [p for p in paths if p.exists()])}


def _build_trees(project: Project) -> list[Path]:
    """Return the trees of the cached builds of the platform, one by signer."""
    return sorted((project.state / "cache/builds").glob(f"*/{project.arch}"))


def _builds(
    project: Project, keys: list[dict[str, str]], names: list[str] | None, *, current: bool, used_since: float
) -> dict[str, list[Path]]:
    """Return the builds of the platform of the packages `names`, or of all of them, whatever their signers: the
    outdated ones, of none of the `keys`, unused since the time `used_since`, and the current ones too with `current`.
    """
    removal = {}
    for tree in _build_trees(project):
        packages = list(tree.glob("*")) if names is None else [tree / name for name in names if (tree / name).exists()]
        for pkg in packages:
            for build in pkg.iterdir():
                up_to_date = build.name in {k.get(pkg.name) for k in keys}
                if current if up_to_date else build.stat().st_mtime < used_since:
                    removal.setdefault(f"{'current' if up_to_date else 'outdated'} builds", []).append(build)
    return removal


def _other_isos(project: Project, isos: set[str]) -> tuple[list[Path], list[Path]]:
    """Return the ISOs of the platform but those of the keys `isos`: their cache, and those in .xcraft/iso/, but the
    hard links to the ISOs kept.
    """
    trees = list((project.state / "cache/isos" / project.arch).glob("*"))
    kept = {(t / "image.iso").stat().st_ino for t in trees if t.name in isos and (t / "image.iso").exists()}
    named = [iso for iso in (project.state / "iso").glob("*.iso") if iso.stat().st_ino not in kept]
    return [t for t in trees if t.name not in isos], named


def routine_removal(
    project: Project,
    keys: list[dict[str, str]],
    names: list[str] | None = None,
    *,
    isos: set[str],
    current: bool = False,
    used_since: float = math.inf,
) -> dict[str, list[Path]]:
    """Return the routine cleanup: the build trees and the outdated builds of the platform, whatever their signers, or
    all of them with `current`, see builds_removal(), of the packages `names`, or of all of them with what they share:
    the temporary files, the metadata of the specs and the resolutions of the platform but those this process read,
    the results of the checks, the requirements of the upstream packages, the upstream packages downloaded, the ISOs of
    the platform but those of the keys `isos`, those in .xcraft/iso/ too, but the hard links to their cache, see
    xcraft.iso, and the logs of the packages removed from the project.

    The outdated builds, ISOs, specs and resolutions used since the time `used_since` are kept: those xcraft read last
    then, see the os.utime() of their uses.
    """
    state = project.state
    removal = builds_removal(project, keys, names, current=current, used_since=used_since)
    if names is not None:
        return _existing({"build trees": [state / "build" / name for name in names], **removal})
    built = list((state / "build").glob("*"))
    read = specs_read() | resolutions_read()  # the current ones, once the project is prepared
    used = {t.name for t in (state / "cache/isos" / project.arch).glob("*") if t.stat().st_mtime >= used_since}
    trees, named = _other_isos(project, isos | used)
    return _existing({
        "build trees": [p for p in built if p.is_dir()],
        "temporary files": [*(p for p in built if not p.is_dir()), *(state / "tmp").glob("*")],
        **removal,
        "outdated ISOs": trees,
        "ISOs in .xcraft/iso": named,
        "outdated metadata": [
            p
            for d in ("specs", "resolved")
            for p in (state / "cache" / d / project.arch).glob("*.json")
            if p not in read and p.stat().st_mtime < used_since
        ],
        "checks and downloads": [
            state / "cache" / cache for cache in ("checks", "upstream", "rpms", "srpms", "extra", "keys")
        ],
        # but those of the steps, like _resolve.log
        "logs": [log for log in (state / "logs").glob("[!_]*.log") if log.stem not in project.package_dirs],
    })  # fmt: skip


def builds_removal(
    project: Project,
    keys: list[dict[str, str]],
    names: list[str] | None = None,
    *,
    current: bool = False,
    used_since: float = math.inf,
) -> dict[str, list[Path]]:
    """Return the builds of the platform of the packages `names`, or of all of them, whatever their signers, see
    _builds(), with the local repository and snapshot, made again when needed, which have hard links to their files. A
    build removed is fetched again from the remote storage, where pushing or publishing put it, or else built again.
    """
    state = project.state
    removal = _builds(project, keys, names, current=current, used_since=used_since)
    return _existing({"local repositories": [state / "repo", state / "publish"], **removal})


def caches_removal(project: Project, cache: str, names: list[str] | None = None) -> dict[str, list[Path]]:
    """Return the caches `cache`, like ccache, of the packages `names`, or of all of them."""
    caches = project.state / cache
    return _existing({cache: list(caches.glob("*")) if names is None else [caches / name for name in names]})


def sources_removal(project: Project, used_since: float = math.inf) -> dict[str, list[Path]]:
    """Return the source archives no package of the checkout has, but those used since the time `used_since`."""
    manifests = [d / "sources" for d in project.package_dirs.values() if (d / "sources").exists()]
    had = {digest for manifest in manifests for digest in read_manifest(manifest).values()}
    return _existing({
        "source archives": [
            d for d in project.source_cache.glob("*") if d.name not in had and d.stat().st_mtime < used_since
        ]
    })  # fmt: skip


def remove(project: Project, removal: dict[str, list[Path]]) -> None:
    """Remove what `removal` has, then the directories of the packages left without builds."""
    for path in (p for paths in removal.values() for p in paths):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for pkg in (pkg for tree in _build_trees(project) for pkg in tree.glob("*")):
        if not any(pkg.iterdir()):
            pkg.rmdir()


class _Image(BaseModel):
    names: list[str] = Field([], alias="Names")
    created: float = Field(alias="Created")


def other_images(kept: set[str], built_since: float = math.inf) -> list[str]:
    """Return the tags of the build environment and side images of the machine, but the tags `kept` and those built
    since the time `built_since`.
    """
    names = [BUILD_IMAGE, *map(side_name, SIDE_IMAGES)]
    listed = process.run(
        ["podman", "images", "--format", "json", *(a for n in names for a in ("--filter", f"reference={n}"))],
        capture=True,
    ).stdout
    return sorted({
        tag
        for image in TypeAdapter(list[_Image]).validate_json(listed)  # an image once by name
        if image.created < built_since
        for tag in image.names
        if tag.startswith(tuple(f"{n}:" for n in names)) and tag not in kept
    })  # fmt: skip


def remove_images(tags: list[str]) -> list[str]:
    """Remove the images `tags`, and return those removed: not those of running containers."""
    return [tag for tag in tags if not process.run(["podman", "rmi", tag], check=False, capture=True).returncode]
