"""The xcraft project: its configuration from ``xcraft.toml``, and where it keeps things."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

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


_ALMALINUX_KEY = "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-10"
_EPEL_KEY = "https://repo.almalinux.org/almalinux/RPM-GPG-KEY-AlmaLinux-10-EPEL-AltArch"


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
    platform: str = "linux/amd64/v2"
    repos: dict[str, Repo] = {
        # the current AlmaLinux 10 and its EPEL: the lock decides of the versions used
        **{s.lower(): _almalinux(s) for s in ("BaseOS", "AppStream", "CRB", "extras", "devel")},
        "epel": Repo(
            baseurl="https://epel.repo.almalinux.org/10/{arch}/",
            gpgkey=_EPEL_KEY,
            source_baseurl="https://epel.vault.almalinux.org/10/Source/",
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

    @property
    def arch(self) -> str:
        """RPM architecture of the platform."""
        return {"linux/amd64/v2": "x86_64_v2", "linux/amd64": "x86_64", "linux/arm64": "aarch64"}[self.platform]


@dataclass
class Project:
    """An xcraft project rooted at the directory containing ``xcraft.toml``."""

    root: Path
    config: Config
    frozen: bool = False
    """Whether the lock cannot change, with --locked."""
    _locked: bool = field(default=False, init=False, repr=False, compare=False)

    @classmethod
    def find(cls) -> Project:
        """Return the project: $XCRAFT_PROJECT, or the checkout of xcraft's code, which comes with its specs."""
        root = Path(os.environ.get("XCRAFT_PROJECT") or Path(__file__).resolve().parent.parent)
        if not (root / "xcraft.toml").exists():
            msg = f"no xcraft.toml in {root}, set XCRAFT_PROJECT to the xcraft project"
            raise RuntimeError(msg)
        with (root / "xcraft.toml").open("rb") as f:
            return cls(root, Config.model_validate(tomllib.load(f)))

    @property
    def specs(self) -> Path:
        """Directory with one subdirectory per package."""
        return self.root / "specs"

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

        It can be taken again while held.
        """
        if self._locked:
            yield
            return
        self.state.mkdir(exist_ok=True)
        with (self.state / "lock").open("w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                msg = "another xcraft command is building or changing this project"
                raise ProjectLockedError(msg) from None
            self._locked = True
            try:
                yield
            finally:
                self._locked = False
