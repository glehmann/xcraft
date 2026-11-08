"""The contents of a snapshot: its sets of packages, of its repositories and of its ISO, from the resolution and the
lock, without building, why each package is there, and what changes between two of them: see docs/contents-design.md.
"""

from __future__ import annotations

import contextlib
import shutil
from typing import TYPE_CHECKING

from pydantic import BaseModel

from xcraft import buildkey, process, remote, resolution
from xcraft.cache import release_suffix
from xcraft.devbuilds import dev_keys
from xcraft.lock import cached_lock
from xcraft.project import Project
from xcraft.publish import build_dependencies
from xcraft.signing import dev_signer
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

FILE = "contents.json"
"""The contents of a published snapshot, of each of its architectures, next to its manifest."""
DEBUG = ("-debuginfo", "-debugsource")
"""The suffixes of the debug packages, which follow theirs: not shown, nor counted, nor compared."""
SETS = ("base", "base/Source", "build", "build/Source", "iso", "install.img")
"""The sets, in their order."""
_ROOTS = {
    "ours": "ours",
    "extra": "in [publish] extra",
    "installer": "in [iso] installer",
    "iso": "in [iso] packages",
    "image": "in the build environment image",
    "buildrequires": "a build requirement of {by}",
}


class Entry(BaseModel):
    """A package of a set: binary, or source, of arch src, with its source package, and whether it is ours."""

    evr: str
    arch: str
    source: str
    ours: bool = False


class Contents(BaseModel):
    """The sets of packages of a snapshot, of an architecture, by name, and why the binary ones are there, see
    resolution.Resolved.why.
    """

    sets: dict[str, dict[str, Entry]]
    why: dict[str, dict[str, list[str]]] = {}


class Published(BaseModel):
    """The contents of a published snapshot, of each of its architectures."""

    arches: dict[str, Contents]


class Change(BaseModel):
    """A package of a set, added, removed, or updated, as it was, `old`, and as it is, `new`, and `entry`, the one it
    has.
    """

    name: str
    old: Entry | None
    new: Entry | None
    entry: Entry


def contents(project: Project, keys: dict[str, str] | None = None) -> Contents:
    """Return the contents of the snapshot of the platform of `project`, prepared, of our builds of `keys`, or else of
    the final ones: without the release suffixes of the development builds, as once on the main branch.
    """
    resolved, lock = resolution.resolve(project), cached_lock(project)
    keys = keys or dev_keys(project, None, buildkey.directories(project))[0]
    binaries, sources = {}, {}
    for name, package in project.packages.items():
        if not package.binaries or name not in keys:
            continue
        suffix = release_suffix(keys[name])
        for binary, built in zip(package.binaries, package.built_names(suffix), strict=True):
            binaries[built] = Entry(
                evr=package.suffixed(binary.evr, binary.evr, suffix), arch=binary.arch, source=name, ours=True
            )
        evr = package.binaries[0].evr
        sources[name] = Entry(evr=package.suffixed(evr, evr, suffix), arch="src", source=name, ours=True)
    for name, pin in lock.binaries.get(project.arch, {}).items():
        binaries.setdefault(name, Entry(evr=pin.evr, arch=pin.arch, source=pin.source))

    def binary_set(names: list[str]) -> dict[str, Entry]:
        return {n: binaries[n] for n in sorted(names) if n in binaries and not n.endswith(DEBUG)}

    def source_set(packages: dict[str, Entry]) -> dict[str, Entry]:
        names = sorted({e.source for e in packages.values()})
        return {n: sources.get(n) or Entry(evr=lock.sources.get(n, ""), arch="src", source=n) for n in names}

    base = binary_set([*(n for n, e in binaries.items() if e.ours), *lock.runtime.get(project.arch, [])])
    sets = {"base": base, "base/Source": source_set(base)}
    if project.config.publish.build:
        build = binary_set(build_dependencies(project))
        sets |= {"build": build, "build/Source": source_set(build)}
    sets |= {name: binary_set(names) for name, names in resolved.sets.items()}
    return Contents(sets=sets, why=resolved.why)


def why(contents: Contents, name: str) -> dict[str, list[str]]:
    """Return why the package `name` is in each binary set of `contents` it is in, as the steps of its path from a
    root, by set.
    """
    paths = {}
    for kind, packages in contents.sets.items():
        if name in packages and kind in contents.why:
            paths[kind] = _path(contents.why[kind], name)
    return paths


def _path(why: dict[str, list[str]], name: str) -> list[str]:
    """Return the steps bringing `name` in, from its root, by `why`."""
    steps, seen = [], {name}
    while how := why.get(name):
        kind, by, dep = how
        if kind not in ("requires", "recommends"):
            steps.append(_ROOTS.get(kind, kind).format(by=by) + (f" ({dep})" if dep != name else ""))
            return steps
        steps.append(f"{'required' if kind == 'requires' else 'recommended'} by {by} ({dep})")
        if by in seen:
            break
        seen.add(by)
        name = by
    return steps


def reason(contents: Contents, kind: str, name: str) -> str:
    """Return the first step bringing the package `name` in the set `kind`, if known."""
    return steps[0] if (steps := _path(contents.why.get(kind, {}), name)) else ""


def changes(old: Contents, new: Contents) -> dict[str, list[Change]]:
    """Return the packages added, removed or updated from `old` to `new`, by set: those of ours first, then by source
    package and name.
    """
    found = {}
    for kind in SETS:
        before, after, changed = old.sets.get(kind, {}), new.sets.get(kind, {}), []
        for name in {*before, *after}:
            a, b = before.get(name), after.get(name)
            if (entry := b or a) and (not a or not b or a.evr != b.evr):
                changed.append(Change(name=name, old=a, new=b, entry=entry))
        if changed:
            found[kind] = sorted(changed, key=lambda c: (not c.entry.ours, c.entry.source, c.name))
    return found


@contextlib.contextmanager
def checkout(project: Project, ref: str) -> Generator[Project]:
    """Return the project at the git revision `ref`, in a temporary worktree sharing the caches of `project` keyed by
    their inputs, the parsed specs, the resolutions and the packages of the base image: what is resolved once is
    resolved for both.
    """
    with project.tmp_dir("contents") as work:
        tree = work / "tree"
        process.run(["git", "-C", str(project.root), "worktree", "add", "-q", "--detach", str(tree), ref])
        try:
            cache = tree / ".xcraft/cache"
            cache.mkdir(parents=True, exist_ok=True)  # or committed, by a project not ignoring it
            for name in ("specs", "resolved", "base"):
                (project.state / "cache" / name).mkdir(parents=True, exist_ok=True)
                shutil.rmtree(cache / name, ignore_errors=True)
                (cache / name).symlink_to(project.state / "cache" / name)
            yield Project.at(tree, project.arch)
        finally:
            process.run(["git", "-C", str(project.root), "worktree", "remove", "--force", str(tree)])


def published(project: Project, storage: remote.Remote, stage: str, snapshot: str, work: Path) -> Contents:
    """Return the contents of the published `snapshot` of the `stage`, of the architecture of `project`, from its
    contents.json, trusted like its manifest, downloaded in `work`.
    """
    tree = f"repos/{project.config.publish.release}/{stage}/{snapshot}"
    manifest = remote.read_manifest(storage, tree, trusted=project.signer or dev_signer(project))
    if FILE not in manifest:
        msg = f"no {FILE} in {storage.url}/{tree}: not published, untrusted, or published before xcraft contents"
        raise RuntimeError(msg)
    if not remote.download(storage, f"{tree}/{FILE}", work / FILE) or sha256_of(work / FILE) != manifest[FILE]:
        msg = f"{storage.url}/{tree}/{FILE} is not the one of its manifest"
        raise RuntimeError(msg)
    arches = Published.model_validate_json((work / FILE).read_text()).arches
    if project.arch not in arches:
        msg = f"no {project.arch} in {storage.url}/{tree}"
        raise RuntimeError(msg)
    return arches[project.arch]
