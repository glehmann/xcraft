"""The lock of upstream packages: the versions the builds use, for all the architectures.

``xcraft.lock`` has one version of each upstream source package installed in
the build environment image, in a build root, with the tools making the ISO, or
published, for the whole tree: on each architecture, the build environment only
sees the binary packages of that version, but the suffixes of the releases of
the rebuilds for an architecture, like AlmaLinux's .alma_altarch. Packages not
locked yet are added to the lock, in the latest version all the architectures
have.

What only builds know, which the build roots computed from the specs need (see
xcraft.resolution), is in the lock of the builds, see xcraft.builds.

An update of upstream packages in the build root of one of our packages changes
its build key, and so its build ID: it is built again, unless it reuses its
build of the main branch, see xcraft.buildid.
"""

from __future__ import annotations

import json
import tomllib
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from xcraft.display import changes
from xcraft.project import write_atomically

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project


class Locked(BaseModel):
    """An upstream binary package of a locked source package, on an architecture."""

    model_config = ConfigDict(extra="forbid")

    evr: str
    arch: str
    source: str
    """Name of its source package."""
    repo: str
    sha256: str


FORMAT = 1
"""The version of the format of the lock."""


class LockFormatError(RuntimeError):
    """Raised when the lock has a format of another xcraft."""


class Pin(BaseModel):
    """An upstream binary package, on an architecture: its RPM, by its SHA-256, which the builds, the published
    repositories and the tools making the ISO get, and the builds keys cover. A table by architecture has one for each
    name: another one of the same name, like another architecture of a multilib package, needs another format.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evr: str = Field(pattern=r"^\S+$")
    arch: str = Field(pattern=r"^[a-z0-9_]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str = Field(pattern=r"^\S+$")
    """The name of its source package, whose version the lock has in its sources."""

    @classmethod
    def parse(cls, text: str) -> Pin:
        """Return the pin of its text in the lock: ``<evr> <arch> <sha256> <source>``."""
        match text.split(" "):
            case [evr, arch, sha256, source]:
                return cls(evr=evr, arch=arch, sha256=sha256, source=source)
        msg = f"not a pinned binary package: {text}"
        raise ValueError(msg)

    def text(self) -> str:
        """Return its text in the lock."""
        return f"{self.evr} {self.arch} {self.sha256} {self.source}"


class Recorded(BaseModel):
    """What builds found, by package, the same on all the architectures but for the entries of some."""

    model_config = ConfigDict(extra="forbid")

    shared: dict[str, list[str]] = {}
    arches: dict[str, dict[str, list[str]]] = {}
    """The entries of the architectures that differ from the shared ones, which they replace."""

    def of(self, arch: str) -> dict[str, list[str]]:
        """Return the entries of `arch`."""
        return self.shared | self.arches.get(arch, {})

    def record(self, arch: str, first: str, name: str, values: list[str]) -> None:
        """Record the `values` of `name` found on `arch`: in the shared entries for `first`, the first architecture of
        the project, and in those of `arch` otherwise, when they differ, so that the first architecture never gets
        what another one found. The others without their own entries have the shared ones.
        """
        values = sorted(values)
        if arch == first:
            if values:
                self.shared[name] = values
            else:
                self.shared.pop(name, None)
            # those found the same elsewhere
            for entries in self.arches.values():
                if entries.get(name) == values:
                    entries.pop(name)
        elif values == self.shared.get(name, []):
            self.arches.get(arch, {}).pop(name, None)
        else:
            self.arches.setdefault(arch, {})[name] = values
        self.arches = {a: entries for a, entries in self.arches.items() if entries}

    def keep(self, names: set[str]) -> None:
        """Keep the entries of `names` only."""
        self.shared = {name: values for name, values in self.shared.items() if name in names}
        self.arches = {
            arch: kept
            for arch, entries in self.arches.items()
            if (kept := {n: v for n, v in entries.items() if n in names})
        }


class Lock(BaseModel):
    """The content of the lock file."""

    model_config = ConfigDict(extra="forbid")

    version: int = FORMAT
    sources: dict[str, str] = {}
    """The version-release of each upstream source package, without the suffixes of an architecture."""
    binaries: dict[str, dict[str, Pin]] = {}
    """The upstream binary packages of the images, of the build roots, of the published repositories and of the tools
    making the ISO, by architecture, then by name."""
    image: dict[str, list[str]] = {}
    """The upstream binary packages of the build environment image, by architecture, pinned in binaries."""
    roots: Recorded = Recorded()
    """For each package of ours, the upstream binary packages of its build root, but those of the image, pinned in
    binaries: with those ours replace in its build, like the image, see graph.build_deps()."""
    runtime: dict[str, list[str]] = {}
    """Upstream packages of the published repositories, by architecture: what our packages need at runtime, the extra
    ones, and the installer's, pinned in binaries."""
    iso: dict[str, list[str]] = {}
    """Upstream packages of the tools making the ISO, besides the image's, by architecture, pinned in binaries."""

    @model_validator(mode="after")
    def _pinned(self) -> Lock:
        """Check that the images, the build roots, the published repositories and the tools making the ISO have
        pinned binary packages only, of locked sources.
        """
        arches = {*self.image, *self.roots.arches, *self.runtime, *self.iso}
        if unpinned := sorted(arches - self.binaries.keys()):
            msg = f"architectures without pinned binary packages: {', '.join(unpinned)}"
            raise ValueError(msg)
        for arch, pins in self.binaries.items():
            names = {
                *self.image.get(arch, []), *(n for names in self.roots.of(arch).values() for n in names),
                *self.runtime.get(arch, []), *self.iso.get(arch, []),
            }  # fmt: skip
            if missing := sorted(names - pins.keys()):
                msg = f"binary packages of {arch} not pinned in the lock: {', '.join(missing)}"
                raise ValueError(msg)
            if unlocked := sorted({p.source for p in pins.values()} - self.sources.keys()):
                msg = f"pinned binary packages of {arch} of source packages not locked: {', '.join(unlocked)}"
                raise ValueError(msg)
        return self


def choose_versions(available: dict[str, dict[str, list[str]]]) -> dict[str, str]:
    """Return the versions to lock of the source packages of `available`, by architecture, with their versions oldest
    first: the latest one all the architectures having them have, or else the latest one of the first of them.
    """
    chosen = {}
    for source in sorted({source for versions in available.values() for source in versions}):
        candidates = [versions[source] for versions in available.values() if versions.get(source)]
        if not candidates:
            continue
        common = set.intersection(*map(set, candidates))
        chosen[source] = next((v for v in reversed(candidates[0]) if v in common), candidates[0][-1])
    return chosen


def lock_path(project: Project) -> Path:
    """Return the lock file of the project."""
    return project.root / "xcraft.lock"


_parsed: dict[Path, tuple[str, Lock]] = {}
"""The last lock parsed of each file, with its content: the commands read it many times, and parsing it is slow."""


def _recorded(entries: dict) -> dict:
    """Return the data of a table of the lock by package, for all the architectures or for some."""
    return {
        "shared": {name: values for name, values in entries.items() if isinstance(values, list)},
        "arches": {arch: values for arch, values in entries.items() if isinstance(values, dict)},
    }


def _validate(data: dict) -> Lock:
    """Return the lock of the `data` of a lock file."""
    data["binaries"] = {
        arch: {n: Pin.parse(t) for n, t in pins.items()} for arch, pins in data.get("binaries", {}).items()
    }
    return Lock.model_validate(data | {"roots": _recorded(data.get("roots", {}))})


def parse_lock(text: str) -> Lock:
    """Return the lock of the content of a lock file, of the format of this xcraft."""
    data = tomllib.loads(text)
    if text and data.get("version") != FORMAT:
        msg = f"the lock has the format {data.get('version')}, not {FORMAT}: of another xcraft"
        raise LockFormatError(msg)
    return _validate(data)


def cached_lock(project: Project) -> Lock:
    """Return the lock of the project, empty when there is none yet, shared by the callers: not to change, see
    load_lock().
    """
    if not (path := lock_path(project)).exists():
        return Lock()
    text = path.read_text()
    if (cached := _parsed.get(path)) is None or cached[0] != text:
        _parsed[path] = cached = text, parse_lock(text)
    return cached[1]


def load_lock(project: Project) -> Lock:
    """Return the lock of the project, empty when there is none yet: a copy of their own, which they may change."""
    return cached_lock(project).model_copy(deep=True)


def _array(items: list[str]) -> str:
    return "[\n" + "".join(f"    {json.dumps(item)},\n" for item in items) + "]" if items else "[]"


def _entries(lock: Lock) -> set[str]:
    """Return the entries of `lock`, like ``roots.qemu pixman``, to show what changes."""
    return {
        *(f"sources {name}-{version}" for name, version in lock.sources.items()),
        *(f"binaries.{arch} {name}-{pin.text()}" for arch, pins in lock.binaries.items() for name, pin in pins.items()),
        *(
            f"roots{f'.{arch}' if arch else ''}.{package} {value}"
            for arch, entries in [("", lock.roots.shared), *lock.roots.arches.items()]
            for package, values in entries.items()
            for value in values or ["(none)"]
        ),
        *(f"{field}.{arch} {name}" for field in ("image", "runtime", "iso")
          for arch, names in getattr(lock, field).items() for name in names),
    }  # fmt: skip


def _tables(name: str, entries: dict[str, list[str]], comment: str = "") -> list[str]:
    return [
        "",
        f"[{name}]{f'  # {comment}' if comment else ''}",
        *(f"{json.dumps(key)} = {_array(sorted(values))}" for key, values in sorted(entries.items())),
    ]


def save_lock(project: Project, lock: Lock) -> None:
    """Write the lock of the project, with one line per package for readable diffs.

    When the project is `frozen`, fail instead if that changes the lock.
    """
    if project.frozen:
        if differences := changes(_entries(load_lock(project)), _entries(lock)):
            msg = f"the lock would change, which --locked forbids: {', '.join(differences)}"
            raise RuntimeError(msg)
        return
    lines = [
        "# written by xcraft: the versions of the upstream packages, for all the architectures",
        "",
        f"version = {FORMAT}",
    ]
    lines += ["", "[sources]"]
    lines += [f"{json.dumps(name)} = {json.dumps(version)}" for name, version in sorted(lock.sources.items())]
    for arch, pins in sorted(lock.binaries.items()):
        lines += ["", f"[binaries.{arch}]  # <evr> <arch> <sha256> <source>"]
        lines += [f"{json.dumps(name)} = {json.dumps(pin.text())}" for name, pin in sorted(pins.items())]
    lines += _tables("roots", lock.roots.shared, "the upstream binary packages of the build roots, but the image's")
    for arch, entries in sorted(lock.roots.arches.items()):
        lines += _tables(f"roots.{arch}", entries)
    lines += _tables("image", lock.image, "the upstream binary packages of the build environment image")
    lines += _tables("runtime", lock.runtime, "the upstream binary packages of the published repositories")
    lines += _tables("iso", lock.iso, "the upstream binary packages of the tools making the ISO, but the image's")
    write_atomically(lock_path(project), "\n".join(lines) + "\n")
