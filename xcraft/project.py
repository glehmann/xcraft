"""The xcraft project: its configuration from ``xcraft.toml``, and where it keeps things."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import sys
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from collections.abc import Generator

    from xcraft.specs import Package


def write_atomically(path: Path, text: str) -> None:
    """Write `text` to `path`, in full or not at all."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text)
    tmp.replace(path)


class ProjectLockedError(RuntimeError):
    """Raised when the lock of a project is held by another command."""


class Repo(BaseModel):
    """A repository of the build environment."""

    model_config = ConfigDict(extra="forbid")

    baseurl: str
    """URL of the repository, where ``{arch}`` is the RPM architecture."""
    gpgkey: str | None = None
    """URL of the key signing its packages, which are not checked without one."""
    includepkgs: list[str] = []
    """The only packages taken from the repository, when given."""
    source_baseurl: str = ""
    """URL of the source packages of the repository, with all their versions, for publishing them."""
    arches: list[str] = []
    """The RPM architectures it is for, all when not given."""


Platform = Literal["linux/amd64/v2", "linux/amd64", "linux/arm64"]
ARCHES: dict[Platform, str] = {"linux/amd64/v2": "x86_64_v2", "linux/amd64": "x86_64", "linux/arm64": "aarch64"}
"""The RPM architecture of each platform."""
_HOST: dict[str, Platform] = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}
"""The platform of each machine, for a host running none of the project's."""


class Remote(BaseModel):
    """The shared storage of the source archives and the builds: public reads, authenticated S3 writes.

    The S3 credentials come from the AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY
    environment variables.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    """Base URL of the public reads, like the website endpoint of a Garage bucket."""
    s3_endpoint: str = ""
    """S3 API, for the writes."""
    bucket: str = ""
    region: str = "garage"
    build_keys: list[str] = []
    """Trusted fingerprints for verifying remote trees, including older release keys. Order does not matter."""

    @field_validator("build_keys")
    @classmethod
    def fingerprints(cls, keys: list[str]) -> list[str]:
        """Require full fingerprints, normalized for GPG's status output."""
        if any(len(key) not in (40, 64) or any(c not in "0123456789abcdefABCDEF" for c in key) for key in keys):
            msg = "build_keys must contain full GPG fingerprints"
            raise ValueError(msg)
        return [key.upper() for key in keys]


class Sign(BaseModel):
    """The key signing the published packages."""

    model_config = ConfigDict(extra="forbid")

    key: str
    """Fingerprint of the key, which gpg uses: it can be in an HSM, through gpg."""
    gnupghome: str = ""
    """The gpg keyring, gpg's default one when not given."""


class Publish(BaseModel):
    """What xcraft publish makes."""

    model_config = ConfigDict(extra="forbid")

    release: str = ""
    """Name of the release, in the paths of its repositories: needed to publish."""
    extra: list[str] = []
    """Upstream packages to publish too, for convenience, like mc, with what they need."""
    build: bool = True
    """Whether to publish the build repository too: the upstream packages the builds need, not in the base one."""


class Import(BaseModel):
    """What xcraft import makes of the packaging repositories and source RPMs."""

    model_config = ConfigDict(extra="forbid")

    branches: list[str] = ["master"]
    """Branches of the packaging repositories whose history is imported, when they have them."""
    srpm_branch: str = "srpm"
    """The branch of import/<pkg>/ getting the commits of the source RPMs, like XS-9.0."""
    removed: list[str] = []
    """Files removed from the source RPMs for legal reasons, each replaced by <file>.deleted-by-XCP-ng.txt saying so."""


class Iso(BaseModel):
    """What xcraft iso makes, from the snapshot."""

    model_config = ConfigDict(extra="forbid")

    installer: list[str] = []
    """Packages of the installer's root filesystem, install.img: they are published too."""
    packages: list[str] = []
    """Packages of the repository of the ISO, with what they need, unless --netinstall."""


class Config(BaseModel):
    """Settings of an xcraft project, from its ``xcraft.toml``."""

    model_config = ConfigDict(extra="forbid")

    base_image: str
    """Image the build environment is made from, with a digest to pin it."""
    platforms: list[Platform]
    """Platforms of the builds: xcraft works on one at a time, see Project.find()."""
    repos: dict[str, Repo]
    """The only repositories of the build environment, with the local builds."""
    macros: dict[str, str] = {}
    """RPM macros of the build environment."""
    packages: list[str] = []
    """Packages installed in the build environment, with those xcraft uses."""
    extra_upstream: dict[str, str] = {}
    """RPMs not available from the repositories, as name -> URL."""
    release_suffixes: list[str] = []
    """Suffixes of the releases of the upstream packages rebuilt for an architecture, like AlmaLinux's .alma_altarch:
    the locks of the architectures have the same versions but for them."""
    repositories: list[str] = ["base"]
    """The repositories the packages are published in: directories of the project, with one subdirectory per package."""
    import_: Import = Field(Import(), alias="import")
    remote: Remote | None = None
    """Where the source archives and the builds are shared, if anywhere."""
    publish: Publish = Publish()
    ccache_size: dict[str, str] = {}
    """The maximum size of the compiler cache of packages, by name, like 20G: ccache's default, 5G, for the others."""
    same_version: list[list[str]] = []
    """Groups of packages with the same version, like kernel and kernel-headers: bump --version updates them all."""
    iso: Iso = Iso()
    sign: Sign | None = None
    """The key signing the published packages: they are not signed without one."""


_held: set[Path] = set()
"""The roots of the projects whose lock this process holds."""


@dataclass
class Project:
    """An xcraft project rooted at the directory containing ``xcraft.toml``."""

    root: Path
    config: Config
    platform: Platform = "linux/amd64/v2"
    """The platform xcraft works on, one of the configuration's: see find()."""
    frozen: bool = False
    """Whether the lock cannot change, with --locked."""

    @classmethod
    def find(cls, arch: str | None = None) -> Project:
        """Find $XCRAFT_PROJECT, or search upward from the current directory, then the xcraft executable.

        It works on the platform of the RPM architecture `arch`, $XCRAFT_ARCH, or the host's, like x86_64 for
        x86_64_v2, or else on the first one of the configuration.
        """
        candidates = (
            [Path(os.environ["XCRAFT_PROJECT"])]
            if "XCRAFT_PROJECT" in os.environ
            else [Path.cwd(), *Path.cwd().parents, *Path(sys.argv[0]).resolve().parents]
        )
        if not (root := next((d for d in candidates if (d / "xcraft.toml").exists()), None)):
            msg = f"no xcraft.toml in {candidates[0]}" + ("" if "XCRAFT_PROJECT" in os.environ else " or its parents")
            if "XCRAFT_PROJECT" not in os.environ:
                msg += f", or the parents of {Path(sys.argv[0]).resolve()}"
            raise RuntimeError(msg)
        with (root / "xcraft.toml").open("rb") as f:
            config = Config.model_validate(tomllib.load(f))
        platforms = {ARCHES[p]: p for p in config.platforms}
        if arch := arch or os.environ.get("XCRAFT_ARCH"):
            if arch not in platforms:
                msg = f"{arch} is not an architecture of the project: {', '.join(platforms)}"
                raise RuntimeError(msg)
            return cls(root, config, platforms[arch])
        return next((p for p in cls(root, config).all_platforms() if p.native), cls(root, config, config.platforms[0]))

    @property
    def arch(self) -> str:
        """The RPM architecture of the platform."""
        return ARCHES[self.platform]

    @property
    def native(self) -> bool:
        """Whether the host runs the platform, like x86_64_v2 on x86_64, rather than emulating it."""
        return self.arch.startswith(os.uname().machine)

    @property
    def first(self) -> str:
        """The RPM architecture of the first platform of the configuration, whose findings are shared in the lock."""
        return ARCHES[self.config.platforms[0]]

    def all_platforms(self) -> list[Project]:
        """Return the project on each platform of the configuration."""
        return [replace(self, platform=platform) for platform in self.config.platforms]

    def on_host(self) -> Project:
        """Return the project on a platform the host runs natively: one of the configuration's, or else the host's."""
        return next((p for p in self.all_platforms() if p.native), replace(self, platform=_HOST[os.uname().machine]))

    @property
    def repos(self) -> dict[str, Repo]:
        """The repositories of the build environment for the platform."""
        return {name: repo for name, repo in self.config.repos.items() if not repo.arches or self.arch in repo.arches}

    @cached_property
    def package_dirs(self) -> dict[str, Path]:
        """The directory of each package, by name, but the hidden ones, like those of the imports in progress."""
        dirs: dict[str, Path] = {}
        for path in sorted(p for repo in self.config.repositories for p in (self.root / repo).glob("[!.]*/")):
            if path.name in dirs:
                msg = f"{path.name} is in two repositories: {dirs[path.name].parent.name} and {path.parent.name}"
                raise RuntimeError(msg)
            dirs[path.name] = path
        return dirs

    @property
    def id(self) -> str:
        """Short id of the project, from its path, in the names it shares with other projects, like containers'."""
        return hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:8]

    @property
    def state(self) -> Path:
        """Directory of everything xcraft generates: caches, logs, build trees."""
        return self.root / ".xcraft"

    @property
    def source_cache(self) -> Path:
        """Source archives, as ``<sha256>/<filename>``."""
        return self.state / "cache/sources"

    @cached_property
    def parsed(self) -> dict[str, Package]:
        """The packages, by name, from their specs, as parsed for the platform."""
        from xcraft.specs import load_packages  # noqa: PLC0415, which needs the project

        return load_packages(self)

    @cached_property
    def packages(self) -> dict[str, Package]:
        """The packages, by name, from their specs, with what their builds found in the lock: the requirements and
        capabilities rpm generated for their binary packages, and their dynamic BuildRequires.
        """  # noqa: D205
        from xcraft.lock import load_lock  # noqa: PLC0415

        lock = load_lock(self)
        requires, provides = lock.requires.of(self.arch), lock.provides.of(self.arch)
        buildrequires = lock.buildrequires.of(self.arch)
        packages = {name: package.model_copy(deep=True) for name, package in self.parsed.items()}
        for name, package in packages.items():
            package.buildrequires.extend(buildrequires.get(name, []))
            for binary in package.binaries:
                binary.requires.extend(requires.get(binary.name, []))
                binary.provides.extend(provides.get(binary.name, []))
        return packages

    @cached_property
    def deps(self) -> dict[str, set[str]]:
        """The build dependencies of each package: the packages to build before it."""
        from xcraft.graph import build_deps  # noqa: PLC0415, which needs the project
        from xcraft.resolution import resolve_image  # noqa: PLC0415

        return build_deps(self.packages, resolve_image(self).image)

    def refresh(self) -> None:
        """Forget the packages and their dependencies, after a change of the lock."""
        for name in ("packages", "deps"):
            self.__dict__.pop(name, None)

    def log(self, package: str) -> Path:
        """Return the log of the last build of `package`."""
        return self.state / "logs" / f"{package}.log"

    @contextmanager
    def work(self, name: str) -> Generator[Path]:
        """Return a new directory in .xcraft/tmp/, for `name` in this process, removed after, but on errors."""
        work = self.state / "tmp" / f"{name}-{os.getpid()}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        yield work
        shutil.rmtree(work)

    @contextmanager
    def lock(self) -> Generator[None]:
        """Hold the lock of the project, for the commands changing its packages or what xcraft generates.

        It can be taken again while held, also through the project on another platform.
        """
        if self.root in _held:
            yield
            return
        self.state.mkdir(exist_ok=True)
        with (self.state / "lock").open("w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                msg = "another xcraft command is building or changing this project"
                raise ProjectLockedError(msg) from None
            _held.add(self.root)
            try:
                yield
            finally:
                _held.remove(self.root)
