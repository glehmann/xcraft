"""The lock of upstream packages: the exact versions the builds use, per architecture.

``xcraft.<arch>.lock`` has one version of each upstream package installed in
the build environment image, in a build root or with the tools making the ISO,
for the whole tree, and the upstream packages each of our builds installed
besides the image's. The build environment only sees the locked version of a
locked package; packages not locked yet are taken at their latest version, and
added to the lock.

When an update changes upstream packages in the build root of one of our
packages, it gets pending entries, with the versions its release was built
with, until it gets a new release, or the change is acknowledged.

The architectures lock the same versions, but for the suffixes of the releases
of the rebuilds for an architecture, like AlmaLinux's .alma_altarch: an update
takes the latest version all of them have.
"""

from __future__ import annotations

import json
import tomllib
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from xcraft.project import write_atomically

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project


class Locked(BaseModel):
    """The locked version of an upstream binary package."""

    model_config = ConfigDict(extra="forbid")

    evr: str
    arch: str
    source: str
    """Name of its source package."""
    repo: str
    sha256: str


class Available(BaseModel):
    """The available versions of the locked packages of an architecture, oldest first, for the update of the locks of
    all the architectures, with those of the others: CI looks for them on a machine of each architecture.
    """  # noqa: D205

    model_config = ConfigDict(extra="forbid")

    arch: str
    versions: dict[str, list[Locked]]


class Lock(BaseModel):
    """The content of a lock file."""

    model_config = ConfigDict(extra="forbid")

    image: list[str] = []
    """Upstream packages of the build environment image."""
    runtime: list[str] = []
    """Upstream packages of the published repositories: what our packages need at runtime, and the extra ones."""
    iso: list[str] = []
    """Upstream packages of the tools making the ISO, besides the image's."""
    packages: dict[str, Locked] = {}
    buildroots: dict[str, list[str]] = {}
    """Upstream packages installed by the build of each of our packages, besides the image's."""
    pending: dict[str, dict[str, str]] = {}
    """For each of our packages, the updated upstream packages of its build root, and the versions it was built with."""

    def note_updates(self, previous: dict[str, str]) -> None:
        """Add the pending entries of updated packages, given their `previous` versions."""
        for package, names in self.buildroots.items():
            entries = self.pending.setdefault(package, {})
            for name, evr in previous.items():
                if name in names or name in self.image:
                    entries.setdefault(name, evr)
                    if entries[name] == self.packages[name].evr:  # back to the version it was built with
                        del entries[name]
        self.pending = {package: entries for package, entries in self.pending.items() if entries}

    def source(self, name: str) -> str:
        """Return the source package of the upstream package `name`, itself when it is not locked anymore."""
        return self.packages[name].source if name in self.packages else name

    def binaries(self, names: list[str]) -> list[str]:
        """Return the upstream packages `names`, binary or source names, as binary names."""
        return sorted({*names, *(n for n in self.packages if self.source(n) in names)})

    def clear_pending(self, packages: list[str], upstream: list[str] | None = None) -> None:
        """Remove the pending entries of `packages`, all of them, or only those of the `upstream` packages."""
        for package in packages:
            entries = self.pending.get(package, {})
            for name in list(entries) if upstream is None else upstream:
                entries.pop(name, None)
            if not entries:
                self.pending.pop(package, None)

    def versionlock(self, names: list[str] | None = None) -> str:
        """Return a dnf versionlock list of the packages `names`, all the locked ones by default."""
        locked = sorted(self.packages.items() if names is None else ((n, self.packages[n]) for n in names))
        return "".join(f"{name}-{p.evr if ':' in p.evr else f'0:{p.evr}'}.{p.arch}\n" for name, p in locked)

    def merge(self, installed: dict[str, Locked]) -> list[str]:
        """Add the packages of `installed` not locked yet, and return their names.

        Raise when a locked package is installed in another version, which
        means the versionlock was not applied.
        """
        if changed := sorted(
            n
            for n, p in installed.items()
            if n in self.packages and (self.packages[n].evr, self.packages[n].arch) != (p.evr, p.arch)
        ):
            msg = f"packages installed in another version than locked: {', '.join(changed)}"
            raise RuntimeError(msg)
        added = sorted(installed.keys() - self.packages.keys())
        self.packages.update((name, installed[name]) for name in added)
        return added


def same_version(evr: str, suffixes: list[str]) -> str:
    """Return `evr` without the release `suffixes` of the rebuilds for an architecture, to compare architectures."""
    for suffix in suffixes:
        evr = evr.replace(suffix, "")
    return evr


def choose_versions(
    locks: dict[str, Lock], available: dict[str, dict[str, list[Locked]]], suffixes: list[str]
) -> dict[str, dict[str, Locked]]:
    """Return the versions to lock, by architecture, of the packages of `available`, their versions oldest first, for
    the architectures updated: the version the other `locks` have, or else the latest one all the updated ones have,
    or else, when there is none, their latest one.
    """  # noqa: D205
    chosen: dict[str, dict[str, Locked]] = {arch: {} for arch in available}
    for name in sorted({name for versions in available.values() for name in versions}):
        candidates = {
            arch: ps
            for arch, versions in available.items()
            if (ps := [p for p in versions.get(name, []) if p.arch == locks[arch].packages[name].arch])
        }
        if not candidates:
            continue
        fixed = {
            same_version(lock.packages[name].evr, suffixes)
            for arch, lock in locks.items()
            if arch not in available and name in lock.packages
        }
        common = set.intersection(*({same_version(p.evr, suffixes) for p in ps} for ps in candidates.values()))
        order = [v for p in next(iter(candidates.values())) if (v := same_version(p.evr, suffixes)) in common]
        targets = fixed & common if fixed else set(order[-1:])
        for arch, ps in candidates.items():
            chosen[arch][name] = next((p for p in reversed(ps) if same_version(p.evr, suffixes) in targets), ps[-1])
    return chosen


def differences(locks: dict[str, Lock], suffixes: list[str]) -> dict[str, dict[str, str]]:
    """Return the packages locked in other versions on some architectures, with their version on each."""
    result = {}
    for name in sorted({name for lock in locks.values() for name in lock.packages}):
        evrs = {arch: lock.packages[name].evr for arch, lock in locks.items() if name in lock.packages}
        if len({same_version(evr, suffixes) for evr in evrs.values()}) > 1:
            result[name] = evrs
    return result


def lock_path(project: Project) -> Path:
    """Return the lock file of the project's architecture."""
    return project.root / f"xcraft.{project.arch}.lock"


def load_lock(project: Project) -> Lock:
    """Return the lock of the project's architecture, empty when there is none yet."""
    path = lock_path(project)
    if not path.exists():
        return Lock()
    with path.open("rb") as f:
        return Lock.model_validate(tomllib.load(f))


def _array(items: list[str]) -> str:
    return "[\n" + "".join(f"    {json.dumps(item)},\n" for item in items) + "]" if items else "[]"


def _entries(lock: Lock) -> set[str]:
    """Return the entries of `lock`, like ``buildroots.xen gcc``, to show what changes."""
    return {
        *(f"{field} {name}" for field, names in (("image", lock.image), ("runtime", lock.runtime)) for name in names),
        *(f"iso {name}" for name in lock.iso),
        *(f"packages {name}-{p.evr}.{p.arch}" for name, p in lock.packages.items()),
        *(f"buildroots.{package} {name}" for package, names in lock.buildroots.items() for name in names),
        *(
            f"pending.{package} {name}-{evr}"
            for package, pending in lock.pending.items()
            for name, evr in pending.items()
        ),
    }


def save_lock(project: Project, lock: Lock) -> None:
    """Write the lock of the project's architecture, with one line per package for readable diffs.

    When the project is `frozen`, fail instead if that changes the lock.
    """
    if project.frozen:
        before, after = _entries(load_lock(project)), _entries(lock)
        if changes := [*(f"+{e}" for e in sorted(after - before)), *(f"-{e}" for e in sorted(before - after))]:
            msg = f"the lock would change, which --locked forbids: {', '.join(changes)}"
            raise RuntimeError(msg)
        return
    lines = [
        "# written by xcraft build, xcraft lock, xcraft publish and xcraft iso",
        "",
        f"image = {_array(sorted(lock.image))}",
    ]
    lines += [f"runtime = {_array(sorted(lock.runtime))}", f"iso = {_array(sorted(lock.iso))}", "", "[packages]"]
    lines += [
        f"{json.dumps(name)} = {{ {', '.join(f'{field} = {json.dumps(value)}' for field, value in p)} }}"
        for name, p in sorted(lock.packages.items())
    ]
    lines += ["", "[buildroots]"]
    lines += [f"{json.dumps(name)} = {_array(sorted(names))}" for name, names in sorted(lock.buildroots.items())]
    for package, entries in sorted(lock.pending.items()):
        lines += ["", f"[pending.{json.dumps(package)}]"]
        lines += [f"{json.dumps(name)} = {json.dumps(evr)}" for name, evr in sorted(entries.items())]
    path = lock_path(project)
    path.parent.mkdir(exist_ok=True)
    write_atomically(path, "\n".join(lines) + "\n")
