from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from xcraft import host
from xcraft.host import Fix

if TYPE_CHECKING:
    from pathlib import Path

CONF = ":qemu-aarch64:M::\\x7fELF\\x02:\\xff\\xff:/usr/bin/qemu-aarch64-static:F\n"


@pytest.fixture
def binfmt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proc, conf, etc, systemd, bin_ = (tmp_path / d for d in ("binfmt_misc", "binfmt.d", "etc", "systemd", "bin"))
    for d in (proc, conf, etc, systemd, bin_):
        d.mkdir()
    (proc / "status").write_text("enabled\n")
    (proc / "qemu-arm").write_text("enabled\ninterpreter /usr/bin/qemu-arm-static\nflags: F\n")
    (bin_ / "qemu-aarch64-static").write_text("")
    (bin_ / "qemu-aarch64-static").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_))
    monkeypatch.setattr(host, "_BINFMT", proc)
    monkeypatch.setattr(host, "_BINFMT_D", conf)
    monkeypatch.setattr(host, "_ETC_BINFMT_D", etc)
    monkeypatch.setattr(host, "_SYSTEMD", systemd)
    return tmp_path


def test_emulation(binfmt: Path) -> None:
    proc, etc = binfmt / "binfmt_misc", binfmt / "etc"
    (binfmt / "binfmt.d/qemu-aarch64-static.conf").write_text(CONF)
    assert host.emulation_problem("aarch64") == "the emulator of aarch64 is not registered in binfmt_misc"
    (proc / "qemu-aarch64").write_text("disabled\ninterpreter /usr/bin/qemu-aarch64-static\nflags: F\n")
    assert host.binfmt("aarch64") is None
    (proc / "qemu-aarch64").write_text("enabled\ninterpreter /usr/bin/qemu-aarch64-static\nflags: F\noffset 0\n")
    assert host.binfmt("aarch64") == ("qemu-aarch64", "F")
    assert (
        host.emulation_problem("aarch64") == "the emulation of aarch64 cannot run sudo, qemu-aarch64 having no C flag"
    )

    # over the file of the distribution, registered by systemd-binfmt
    assert host.emulation_fix("aarch64") == [
        Fix(["sudo", "tee", f"{etc}/qemu-aarch64-static.conf"], CONF.replace(":F\n", ":FOC\n")),
        Fix(["sudo", "systemctl", "restart", "systemd-binfmt"]),
    ]
    # or directly, without systemd
    (binfmt / "systemd").rmdir()
    assert host.emulation_fix("aarch64")[1:] == [
        Fix(["sudo", "tee", f"{proc}/qemu-aarch64"], "-1\n"),
        Fix(["sudo", "tee", f"{proc}/register"], CONF.replace(":F\n", ":FOC\n")),
    ]
    # keeping the other flags of the distribution, like Debian's
    (binfmt / "binfmt.d/qemu-aarch64-static.conf").write_text(CONF.replace(":F\n", ":OPF\n"))
    assert host.emulation_fix("aarch64")[0].input == CONF.replace(":F\n", ":OPFC\n")
    (proc / "qemu-aarch64").write_text("enabled\ninterpreter /usr/bin/qemu-aarch64-static\nflags: OCF\n")
    assert host.emulation_problem("aarch64") == ""
    assert host.emulation_fix("aarch64") == []


def test_emulation_unregistered(binfmt: Path) -> None:
    # without a file of the distribution, nor /etc/binfmt.d/, like on Slackware
    (binfmt / "systemd").rmdir()
    (binfmt / "etc").rmdir()
    proc, etc = binfmt / "binfmt_misc", binfmt / "etc"
    magic = r"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00"
    mask = r"\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff"
    conf = f":qemu-aarch64:M::{magic}:{mask}:{binfmt}/bin/qemu-aarch64-static:FOC\n"
    assert host.emulation_fix("aarch64") == [
        Fix(["sudo", "mkdir", "-p", str(etc)]),
        Fix(["sudo", "tee", f"{etc}/qemu-aarch64.conf"], conf),
        Fix(["sudo", "tee", f"{proc}/register"], conf),
    ]
    # and nothing to register without the emulator
    (binfmt / "bin/qemu-aarch64-static").unlink()
    assert host.emulation_problem("aarch64").startswith("no emulation of aarch64, to build for it: install")
    assert host.emulation_fix("aarch64") == []


@pytest.mark.parametrize(
    ("os_release", "commands"),
    [
        ('ID=ubuntu\nID_LIKE="debian"\n', ["sudo apt-get install -y podman gnupg qemu-user-static"]),
        ("ID=fedora\n", ["sudo dnf install -y podman gnupg2 qemu-user-static"]),
        ('ID="almalinux"\nID_LIKE="rhel centos fedora"\n', ["sudo dnf install -y podman gnupg2"]),
        ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', ["sudo zypper install -y podman gpg2 qemu-linux-user"]),
        (
            "ID=arch\n",
            ["sudo pacman -S --needed --noconfirm podman gnupg qemu-user-static qemu-user-static-binfmt"],
        ),
        ("ID=alpine\n", ["sudo apk add podman gnupg qemu-x86_64 qemu-aarch64"]),
        ("ID=gentoo\n", ["sudo emerge --noreplace app-containers/podman app-crypt/gnupg app-emulation/qemu"]),
        ("ID=slackware\n", ["sudo sboinstall -r podman qemu", "sudo slackpkg install gnupg2"]),
        ("ID=nixos\n", []),
    ],
)
def test_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, os_release: str, commands: list[str]) -> None:
    (tmp_path / "os-release").write_text(os_release)
    monkeypatch.setattr(host, "_OS_RELEASE", tmp_path / "os-release")
    assert [str(fix) for fix in host.install(["podman", "gpg", "qemu-x86_64_v2", "qemu-aarch64"])] == commands


def test_subids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subuid, subgid = tmp_path / "subuid", tmp_path / "subgid"
    monkeypatch.setattr(host, "_SUBIDS", (subuid, subgid))
    assert host.subids("dev") == [
        Fix(["sudo", "usermod", "--add-subuids", "100000-165535", "--add-subgids", "100000-165535", "dev"]),
        Fix(["podman", "system", "migrate"]),
    ]
    # after the ranges of the others
    subuid.write_text("dev:100000:65536\n")
    subgid.write_text("other:165536:65536\n")
    assert host.subids("dev")[0] == Fix(["sudo", "usermod", "--add-subgids", "231072-296607", "dev"])
    subgid.write_text("dev:100000:65536\n")
    assert host.subids("dev") == []
