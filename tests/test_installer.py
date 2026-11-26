"""Real ISOs of the test project, with its iso/ and the hook there, made in the build environment and booted with
QEMU.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import ARCH, TOML, commit_base, git, xcraft
from xcraft.project import Project
from xcraft.signing import dev_signer, signed_by

pytestmark = [
    pytest.mark.skipif(ARCH != "x86_64_v2", reason="the ISOs are for x86_64"),
    pytest.mark.skipif(not shutil.which("bsdtar"), reason="needs bsdtar, to read the ISOs"),
]

# a kernel and systemd, which runs the installer of iso/installimg/, and grub, for UEFI, from the snapshot
INSTALLER = 'installer = ["kernel-core", "systemd"]\n'
GRUB = '"grub2-efi-x64-cdboot", "grub2-efi-x64-modules"'


@pytest.fixture
def root(root: Path) -> Path:
    (root / "xcraft.toml").write_text(f'{TOML}{INSTALLER}packages = ["with-deps"]\n\n[publish]\nextra = [{GRUB}]\n')
    return root


def made_iso(root: Path) -> Path:
    """Make the ISO of the checkout, signed by the developer's key, and return it."""
    code, output = xcraft("iso", "--output", str(root / "toy.iso"))
    assert code == 0, output
    # from a snapshot without the source packages, which it does not have
    assert not (root / ".xcraft/publish/base/Source").exists()
    # signed once made, with its detached signature next to it
    signer = dev_signer(Project.find())
    assert signer
    assert signed_by(signer.public_key(), signer.key, root / "toy.iso", root / "toy.iso.asc")
    return root / "toy.iso"


def packages(iso: Path) -> list[str]:
    """Return the packages of the repository of `iso`."""
    listing = subprocess.run(["bsdtar", "-tf", iso], capture_output=True, text=True, check=True).stdout.split()
    return sorted(f.removeprefix("Packages/") for f in listing if f.startswith("Packages/") and f.endswith(".rpm"))


def booted(root: Path, *args: str) -> tuple[int, str, str]:
    """Boot the ISO of the checkout with xcraft boot, and return its exit code, its output, and the serial console."""
    accel = "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"
    vm_dir = root / "vm"
    shutil.rmtree(vm_dir, ignore_errors=True)
    code, output = xcraft(
        "boot", "--headless", "--accel", accel, "--memory", "2048", "--cpus", "2", "--ssh-port", "0",
        "--disk-size", "1G", "--vm-dir", str(vm_dir), *args,
    )  # fmt: skip
    return code, output, (vm_dir / "serial.log").read_bytes().decode(errors="replace")


def installer_line(root: Path, *args: str) -> str:
    """Boot the ISO of the checkout with xcraft boot, and return what its installer showed, before asking to reboot."""
    code, output, log = booted(root, *args)
    assert code == 0, output
    assert "✓ the installer finished and asked to reboot: xcraft vm start boots the installed system" in " ".join(
        output.split()
    )
    # up to a message of the kernel printed right after it, on the same console
    found = re.search(r"^toy installer of .*?(?=\[ *\d+\.\d+\]|\r?$)", log, re.MULTILINE)
    assert found, log
    return found[0]


@pytest.mark.skipif(not shutil.which("qemu-system-x86_64"), reason="needs QEMU")
def should_boot_the_installer_with_uefi_and_bios(root: Path) -> None:
    iso = made_iso(root)
    listing = subprocess.run(["bsdtar", "-tf", iso], capture_output=True, text=True, check=True).stdout.split()
    assert {"install.img", "boot/vmlinuz", "boot/efiboot.img", "boot/isolinux/isolinux.bin"} <= set(listing)
    assert "EFI/BOOT/efiboot.cfg" not in listing
    assert [p.split("-1.0-")[0] for p in packages(iso)] == ["with-deps"]
    hooked = subprocess.run(["bsdtar", "-xOf", iso, "toy-hook.json"], capture_output=True, check=True).stdout
    assert json.loads(hooked) == {"netinstall": False, "signed": True}
    assert iso.read_bytes()[0x8028:0x8048].rstrip() == b"TOY_1_0"  # the volume id of the primary volume descriptor
    # the ISO of the checkout, cached: with UEFI, with kernel arguments added in GRUB's editor, and with BIOS
    assert installer_line(root, "--kernel-args", "xcraft.test=1") == (
        "toy installer of toy 1.0: BOOT_IMAGE=/boot/vmlinuz console=ttyS0 xcraft.test=1"
    )
    assert installer_line(root, "--bios") == (
        "toy installer of toy 1.0: BOOT_IMAGE=/boot/vmlinuz initrd=/install.img console=ttyS0"
    )
    # an installer which powers off rather than asking to reboot fails
    code, output, _ = booted(root, "--kernel-args", "xcraft.poweroff")
    assert code == 1
    assert "✗ the guest powered off, rather than the installer asking to reboot: see" in " ".join(output.split())


def should_have_the_development_builds_of_changed_packages(root: Path) -> None:
    # with trivial, which with-deps is built against: the final builds of the main branch, then the development ones
    # of a branch, as their changes rebuild them and those built against them
    (root / "xcraft.toml").write_text(
        f'{TOML}{INSTALLER}packages = ["with-deps", "trivial"]\n\n[publish]\nextra = [{GRUB}]\n'
    )
    commit_base(root)
    assert packages(made_iso(root)) == [
        "trivial-1.0-1+b1.xcpng8.99.noarch.rpm", "with-deps-1.0-1+b1.xcpng8.99.noarch.rpm",
    ]  # fmt: skip
    git(root, "checkout", "-qb", "feature")
    for name, built, expected in (
        ("with-deps", ["with-deps"], ["trivial-1.0-1+b1", "with-deps-1.0-1+b2~1.dev"]),
        ("trivial", ["trivial", "with-deps"], ["trivial-1.0-1+b2~1.dev", "with-deps-1.0-1+b2~2.dev"]),
    ):
        spec = root / f"base/{name}/{name}.spec"
        spec.write_text(spec.read_text().replace("%install\n", "%install\necho changed\n"))
        code, output = xcraft("iso", "--output", str(root / "toy.iso"))
        assert code == 0, output
        assert re.findall(r"^✓ (\S+) built", output, re.MULTILINE) == built, output
        assert packages(root / "toy.iso") == [f"{p}.xcpng8.99.noarch.rpm" for p in expected]


def should_make_partial_isos_of_the_given_packages_built_only(root: Path) -> None:
    # trivial changed, which with-deps is built against: with-deps kept as it was built, against the previous trivial
    (root / "xcraft.toml").write_text(
        f'{TOML}{INSTALLER}packages = ["with-deps", "trivial"]\n\n[publish]\nextra = [{GRUB}]\n'
    )
    commit_base(root)
    made_iso(root)
    git(root, "checkout", "-qb", "feature")
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("%install\n", "%install\necho changed\n"))
    code, output = xcraft("iso", "trivial")
    assert code == 0, output
    assert re.findall(r"^✓ (\S+) built", output, re.MULTILINE) == ["trivial"], output
    assert "partial: the latest builds of with-deps, rather than new ones" in output
    partial = next((root / ".xcraft/iso").glob("*-partial.iso"))
    assert packages(partial) == [
        "trivial-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm", "with-deps-1.0-1+b1.xcpng8.99.noarch.rpm",
    ]  # fmt: skip
    # but the ISO of the checkout, with all its packages built: with-deps the same as the main branch's build, which it
    # reuses, see xcraft.build._Run._reproduced()
    code, output = xcraft("iso", "--output", str(root / "toy.iso"))
    assert code == 0, output
    assert re.findall(r"^✓ (\S+) built", output, re.MULTILINE) == ["with-deps"], output
    assert not (root / "toy.iso").samefile(partial)
