"""The host: what xcraft needs on it, which xcraft doctor checks, and the commands fixing what it misses.

The containers of a platform the host cannot run are emulated by qemu-user, which the kernel runs through its
binfmt_misc registrations. They need the C flag, for sudo, which dnf runs with in the containers: without it, the
kernel ignores the setuid bit of the emulated binaries.
"""

from __future__ import annotations

import shlex
import shutil
from pathlib import Path
from typing import NamedTuple

from xcraft import process

_QEMU = {"x86_64_v2": "x86_64", "x86_64": "x86_64", "aarch64": "aarch64"}
"""The qemu name of each RPM architecture."""
_ELF = {
    # the magic and mask of the ELF binaries of each qemu architecture, as qemu-binfmt-conf.sh registers them
    "x86_64": (
        r"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x3e\x00",
        r"\xff\xff\xff\xff\xff\xfe\xfe\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff",
    ),
    "aarch64": (
        r"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00",
        r"\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff",
    ),
}
_BINFMT = Path("/proc/sys/fs/binfmt_misc")
_BINFMT_D = Path("/usr/lib/binfmt.d")
_ETC_BINFMT_D = Path("/etc/binfmt.d")
_SYSTEMD = Path("/run/systemd/system")
_OS_RELEASE = Path("/etc/os-release")
_SUBIDS = (Path("/etc/subuid"), Path("/etc/subgid"))
_DISTROS = {
    # by family of distributions, as ID and ID_LIKE of os-release name them: the commands installing packages, and the
    # packages of the tools, when not named after them, or empty when it has none; qemu is the user emulator of
    # {arch}, statically linked
    "debian": [
        (
            "apt-get install -y",
            {
                "gpg": "gnupg",
                "ssh": "openssh-client",
                "newuidmap": "uidmap",
                "qemu": "qemu-user-static",
                "qemu-system-x86_64": "qemu-system-x86",
                "qemu-img": "qemu-utils",
                "ovmf": "ovmf",
            },
        )
    ],
    "fedora": [
        (
            "dnf install -y",
            {
                "gpg": "gnupg2",
                "ssh": "openssh-clients",
                "newuidmap": "shadow-utils",
                "qemu": "qemu-user-static",
                "qemu-system-x86_64": "qemu-system-x86-core",
                "qemu-img": "qemu-img",
                "ovmf": "edk2-ovmf",
            },
        )
    ],
    # RHEL and its rebuilds, which have no qemu-user, not even in EPEL
    "rhel": [
        (
            "dnf install -y",
            {
                "gpg": "gnupg2",
                "ssh": "openssh-clients",
                "newuidmap": "shadow-utils",
                "qemu": "",
                # qemu-kvm installs /usr/libexec/qemu-kvm rather than the executable boot uses.
                "qemu-system-x86_64": "",
                "qemu-img": "qemu-img",
                "ovmf": "edk2-ovmf",
            },
        )
    ],
    "suse": [
        (
            "zypper install -y",
            {
                "gpg": "gpg2",
                "ssh": "openssh-clients",
                "newuidmap": "shadow",
                "qemu": "qemu-linux-user",
                "qemu-system-x86_64": "qemu-x86",
                "qemu-img": "qemu-tools",
                "ovmf": "qemu-ovmf-x86_64",
            },
        )
    ],
    "arch": [
        (
            "pacman -S --needed --noconfirm",
            {
                "gpg": "gnupg",
                "ssh": "openssh",
                "newuidmap": "shadow",
                "qemu": "qemu-user-static qemu-user-static-binfmt",
                "qemu-system-x86_64": "qemu-system-x86",
                "qemu-img": "qemu-img",
                "ovmf": "edk2-ovmf",
            },
        )
    ],
    "alpine": [
        (
            "apk add",
            {
                "gpg": "gnupg",
                "ssh": "openssh-client",
                "newuidmap": "shadow-uidmap",
                "qemu": "qemu-{arch}",
                "qemu-system-x86_64": "qemu-system-x86_64",
                "qemu-img": "qemu-img",
                "ovmf": "ovmf",
            },
        )
    ],
    "gentoo": [
        (
            "emerge --noreplace",
            {
                "podman": "app-containers/podman",
                "git": "dev-vcs/git",
                "git-lfs": "dev-vcs/git-lfs",
                "gpg": "app-crypt/gnupg",
                "rsync": "net-misc/rsync",
                "ssh": "net-misc/openssh",
                "newuidmap": "sys-apps/shadow",
                "qemu": "app-emulation/qemu",
                "qemu-system-x86_64": "app-emulation/qemu",
                "qemu-img": "app-emulation/qemu",
                "ovmf": "sys-firmware/edk2-ovmf",
            },
        )
    ],
    # podman, git-lfs and qemu are not in Slackware, but in SlackBuilds.org
    "slackware": [
        ("slackpkg install", {"gpg": "gnupg2", "ssh": "openssh", "newuidmap": "shadow"}),
        (
            "sboinstall -r",
            {
                "podman": "podman",
                "git-lfs": "git-lfs",
                "qemu": "qemu",
                "qemu-system-x86_64": "qemu",
                "qemu-img": "qemu",
                "ovmf": "",
            },
        ),
    ],
}


class Fix(NamedTuple):
    """A command fixing the host, with its input."""

    cmd: list[str]
    input: str = ""

    def __str__(self) -> str:
        """Return the command as run in a shell."""
        return f"{shlex.join(self.cmd)} <<'EOF'\n{self.input}EOF" if self.input else shlex.join(self.cmd)

    def run(self) -> None:
        """Run the command, failing when it fails."""
        process.run(self.cmd, input=self.input or None, capture=bool(self.input))


def distribution() -> str:
    """Return the family of the host's distribution, a key of _DISTROS, or nothing when it is not one of them."""
    try:
        lines = _OS_RELEASE.read_text().splitlines()
    except OSError:
        return ""
    fields = {k: v.strip("\"'") for k, _, v in (line.partition("=") for line in lines)}
    return next((i for i in [fields.get("ID", ""), *fields.get("ID_LIKE", "").split()] if i in _DISTROS), "")


def install(tools: list[str]) -> list[Fix]:
    """Return the commands installing `tools`, like `qemu-<RPM arch>` for an emulator, on the host's distribution."""
    installers = _DISTROS.get(distribution(), [])
    commands: dict[str, list[str]] = {}
    for tool in tools if installers else []:
        arch = tool.removeprefix("qemu-") if tool.startswith("qemu-") else ""
        name, arch = ("qemu", arch) if arch in _QEMU else (tool, "")
        cmd, packages = next(((cmd, p[name]) for cmd, p in installers if name in p), (installers[0][0], name))
        if packages:
            commands.setdefault(cmd, []).extend(packages.format(arch=_QEMU.get(arch, "")).split())
    return [Fix(["sudo", *cmd.split(), *dict.fromkeys(packages)]) for cmd, packages in commands.items()]


def subids(user: str) -> list[Fix]:
    """Return the commands giving `user` the subordinate ids rootless podman needs, when it has none."""
    ranges = {path: [line.split(":") for line in path.read_text().split()] if path.exists() else [] for path in _SUBIDS}
    missing = [path for path, lines in ranges.items() if not any(name == user for name, _, _ in lines)]
    if not missing:
        return []
    # after all the ranges already given, to share none
    start = max([100000, *(int(first) + int(count) for lines in ranges.values() for _, first, count in lines)])
    options = [f"--add-sub{path.name[3:]}s" for path in missing]  # --add-subuids, --add-subgids
    ids = f"{start}-{start + 65535}"
    return [
        Fix(["sudo", "usermod", *(x for option in options for x in (option, ids)), user]),
        Fix(["podman", "system", "migrate"]),
    ]


def binfmt(arch: str) -> tuple[str, str] | None:
    """Return the name and the flags of the enabled binfmt_misc registration of the emulator of `arch`, if any."""
    qemu = f"qemu-{_QEMU[arch]}"
    for entry in _BINFMT.glob("*"):
        try:
            lines = [line.split(maxsplit=1) for line in entry.read_text().splitlines()]
        except OSError:  # register and status
            continue
        fields = {line[0]: line[1] if len(line) > 1 else "" for line in lines if line}
        if "enabled" in fields and fields.get("interpreter", "").endswith((qemu, f"{qemu}-static")):
            return entry.name, fields.get("flags:", "")
    return None


def emulator(arch: str) -> str:
    """Return the path of the static qemu-user emulator of `arch` on the host, or nothing when it has none."""
    qemu = f"qemu-{_QEMU[arch]}"
    return shutil.which(f"{qemu}-static") or shutil.which(qemu) or ""


def emulation_problem(arch: str) -> str:
    """Return why the host cannot emulate the RPM architecture `arch` for xcraft, or nothing when it can."""
    if not (entry := binfmt(arch)):
        if not emulator(arch):
            return f"no emulation of {arch}, to build for it: install its qemu-user emulator, statically linked"
        return f"the emulator of {arch} is not registered in binfmt_misc"
    if "C" not in entry[1]:
        return f"the emulation of {arch} cannot run sudo, {entry[0]} having no C flag"
    return ""


def _flags(registration: str) -> str:
    """Return the binfmt_misc `registration` with the flags FOC added to its own, the last field."""
    rest, flags = registration.rsplit(":", 1)
    return f"{rest}:{flags}{''.join(f for f in 'FOC' if f not in flags)}"


def emulation_fix(arch: str) -> list[Fix]:
    """Return the commands registering the emulation of `arch` with the flags FOC too, or nothing when it needs none.

    F loads the emulator once, and O and C open the binary with its credentials; the distribution's other flags, like
    P, are kept. The registration goes in
    /etc/binfmt.d/, over the file of the distribution registering it, if any. systemd-binfmt registers it, or else the
    commands do it directly, OpenRC's binfmt service reading /etc/binfmt.d/ at the next boot.
    """
    if not emulation_problem(arch) or not (path := emulator(arch)):
        return []
    entry = binfmt(arch)
    name = entry[0] if entry else f"qemu-{_QEMU[arch]}"
    prefix = f":{name}:"
    conf = next((c for c in sorted(_BINFMT_D.glob("*.conf")) if prefix in c.read_text()), _BINFMT_D / f"{name}.conf")
    if conf.exists():
        # the flags are the last field
        lines = conf.read_text().splitlines()
        text = "".join(f"{_flags(line)}\n" if line.startswith(prefix) else f"{line}\n" for line in lines)
    else:
        magic, mask = _ELF[_QEMU[arch]]
        text = f"{prefix}M::{magic}:{mask}:{path}:FOC\n"
    fixes = [] if _ETC_BINFMT_D.exists() else [Fix(["sudo", "mkdir", "-p", str(_ETC_BINFMT_D)])]
    fixes.append(Fix(["sudo", "tee", str(_ETC_BINFMT_D / conf.name)], text))
    if _SYSTEMD.exists():
        return [*fixes, Fix(["sudo", "systemctl", "restart", "systemd-binfmt"])]
    if entry:
        fixes.append(Fix(["sudo", "tee", str(_BINFMT / name)], "-1\n"))
    register = "".join(line + "\n" for line in text.splitlines() if line.startswith(prefix))
    return [*fixes, Fix(["sudo", "tee", str(_BINFMT / "register")], register)]
