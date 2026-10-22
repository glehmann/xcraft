"""The lock of upstream packages: the exact versions the builds use, per architecture.

``locks/<arch>.toml`` has one version of each upstream package installed in
the build environment image, in a build root or with the tools making the ISO,
for the whole tree, and the upstream packages each of our builds installed
besides the image's. The build environment only sees the locked version of a
locked package; packages not locked yet are taken at their latest version, and
added to the lock.

When an update changes upstream packages in the build root of one of our
packages, it gets pending entries, with the versions its release was built
with, until it gets a new release, or the change is acknowledged.
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


def lock_path(project: Project) -> Path:
    """Return the lock file of the project's architecture."""
    return project.root / "locks" / f"{project.arch}.toml"


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
