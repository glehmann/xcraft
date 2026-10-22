"""The xcraft project: its configuration from ``xcraft.toml``, and where it keeps things."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

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


_ALMALINUX_KEY = "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10"
_EPEL_KEY = "https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux-10-EPEL-AltArch"
_FEDORA_EPEL = "https://dl.fedoraproject.org/pub/epel"


def _almalinux(section: str) -> Repo:
    return Repo(
        baseurl=f"https://repo.almalinux.org/almalinux/10/{section}/{{arch}}/os/",
        gpgkey=_ALMALINUX_KEY,
        source_baseurl=f"https://vault.almalinux.org/10/{section}/Source/",
    )


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

    release: str = "9.0"
    """Name of the release, in the paths of its repositories."""
    extra: list[str] = []
    """Upstream packages to publish too, for convenience, like mc, with what they need."""


class Iso(BaseModel):
    """What xcraft iso makes, from the snapshot."""

    model_config = ConfigDict(extra="forbid")

    installer: list[str] = [
        # the mandatory packages of AlmaLinux's @core, but kexec-tools, xfsprogs and yum
        *["audit", "basesystem", "bash", "coreutils", "cronie", "cronie-noanacron", "curl", "dnf", "e2fsprogs"],
        *["filesystem", "firewalld", "glibc", "grubby", "hostname", "iproute", "iproute-tc", "iputils"],
        *["irqbalance", "kbd", "less", "logrotate", "man-db", "ncurses", "openssh-clients", "openssh-server"],
        *["p11-kit", "parted", "policycoreutils", "procps-ng", "rootfiles", "rpm", "rpm-plugin-audit"],
        *["selinux-policy-targeted", "setup", "shadow-utils", "sssd-common", "sssd-kcm", "sudo", "systemd"],
        *["util-linux", "vim-minimal"],
        # the installer, xl for its preinit, and the hardware support
        *["host-installer-startup", "chrony", "dhcp-client", "xen-dom0-tools", "kernel", "linux-firmware"],
        # for debugging
        *["netcat", "pciutils", "usbutils"],
    ]
    """Packages of the installer's root filesystem, install.img: they are published too."""
    packages: list[str] = ["xcp-ng-deps"]
    """Packages of the repository of the ISO, with what they need, unless --netinstall."""


class Config(BaseModel):
    """Settings of an xcraft project, from its ``xcraft.toml``."""

    model_config = ConfigDict(extra="forbid")

    base_image: str = (
        "ghcr.io/almalinux/10-base:10.2@sha256:ed618ca107e9fd47ee2a64d1d33119ef356cfaf65a338daaa3fad19b80ae5da4"
    )
    """Image the build environment is made from, with a digest to pin it."""
    platforms: list[Platform] = ["linux/amd64/v2", "linux/arm64"]
    """Platforms of the builds: xcraft works on one at a time, see Project.find()."""
    repos: dict[str, Repo] = {
        # the current AlmaLinux 10 and its EPEL: the lock decides of the versions used
        **{s.lower(): _almalinux(s) for s in ("BaseOS", "AppStream", "CRB", "extras", "devel")},
        # AlmaLinux rebuilds EPEL for x86_64_v2 only
        "epel": Repo(
            baseurl="https://epel.repo.almalinux.org/10/{arch}/",
            gpgkey=_EPEL_KEY,
            source_baseurl="https://epel.vault.almalinux.org/10/Source/",
            arches=["x86_64_v2"],
        ),
        "epel-fedora": Repo(
            baseurl=f"{_FEDORA_EPEL}/10/Everything/{{arch}}/",
            gpgkey=f"{_FEDORA_EPEL}/RPM-GPG-KEY-EPEL-10",
            source_baseurl=f"{_FEDORA_EPEL}/10/Everything/source/tree/",
            arches=["x86_64", "aarch64"],
        ),
    }
    """The only repositories of the build environment, with the local builds."""
    macros: dict[str, str] = {"dist": "%{?autorev}.xcpng8.99", "xcpng": "9", "xenserver": "9"}
    """RPM macros of the build environment."""
    packages: list[str] = [
        # like mock's build roots, and what XCP-ng specs expect without BuildRequires
        # (coreutils-single, in the base image, provides coreutils)
        *["bash", "bzip2", "cpio", "diffutils", "findutils", "gawk", "grep", "gzip", "patch", "sed"],
        *["tar", "unzip", "which", "xz"],
        *["rpm-build", "redhat-rpm-config", "epel-rpm-macros", "gcc", "gcc-c++", "make", "git"],
        # used by xcraft
        *["dnf-plugins-core", "python3-dnf-plugin-versionlock", "createrepo_c", "python3-rpm", "ccache", "sudo"],
    ]
    """Packages installed in the build environment."""
    extra_upstream: dict[str, str] = {}
    """RPMs not available from the repositories, as name -> URL."""
    remote: Remote | None = None
    """Where the source archives and the builds are shared, if anywhere."""
    publish: Publish = Publish()
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
        """Return the project: $XCRAFT_PROJECT, or the checkout of xcraft's code, which comes with its specs.

        It works on the platform of the RPM architecture `arch`, $XCRAFT_ARCH, or the host's, like x86_64 for
        x86_64_v2, or else on the first one of the configuration.
        """
        root = Path(os.environ.get("XCRAFT_PROJECT") or Path(__file__).resolve().parent.parent)
        if not (root / "xcraft.toml").exists():
            msg = f"no xcraft.toml in {root}, set XCRAFT_PROJECT to the xcraft project"
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

    def all_platforms(self) -> list[Project]:
        """Return the project on each platform of the configuration."""
        return [replace(self, platform=platform) for platform in self.config.platforms]

    @property
    def repos(self) -> dict[str, Repo]:
        """The repositories of the build environment for the platform."""
        return {name: repo for name, repo in self.config.repos.items() if not repo.arches or self.arch in repo.arches}

    @property
    def specs(self) -> Path:
        """Directory with one subdirectory per repository, like ``base``, with one subdirectory per package."""
        return self.root / "specs"

    @cached_property
    def package_dirs(self) -> dict[str, Path]:
        """The directory of each package, by name, but the hidden ones, like those of the imports in progress."""
        dirs: dict[str, Path] = {}
        for path in sorted(self.specs.glob("[!.]*/[!.]*/")):
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
    def packages(self) -> dict[str, Package]:
        """The packages, by name, from their specs."""
        from xcraft.specs import load_packages  # noqa: PLC0415, which needs the project

        return load_packages(self)

    @cached_property
    def deps(self) -> dict[str, set[str]]:
        """The build dependencies of each package: the packages to build before it."""
        from xcraft.graph import build_deps  # noqa: PLC0415, which needs the project
        from xcraft.lock import load_lock  # noqa: PLC0415

        return build_deps(self.packages, load_lock(self).image)

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
