"""The helpers making the tree of the ISO, which run in the build environment otherwise."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from xcraft.buildenv.iso import copy_templates, prune_firmware

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.skipif(not (shutil.which("objcopy") and shutil.which("modinfo")), reason="needs objcopy and modinfo")
def test_prune_firmware(tmp_path: Path) -> None:
    # a driver naming v/driver.bin, a link to the file of a revision, whose name no driver gives
    modules, firmware = tmp_path / "usr/lib/modules/6.6.0/kernel", tmp_path / "usr/lib/firmware"
    modules.mkdir(parents=True)
    (firmware / "v").mkdir(parents=True)
    (tmp_path / "modinfo").write_bytes(b"firmware=v/driver.bin\0license=GPL\0")
    subprocess.run(
        ["objcopy", "-I", "binary", "-O", "elf64-little", "--rename-section", ".data=.modinfo",
         tmp_path / "modinfo", modules / "driver.ko"],
        check=True,
    )  # fmt: skip
    (firmware / "v/revision.bin").touch()
    (firmware / "v/driver.bin").symlink_to("revision.bin")
    (firmware / "other.bin").touch()
    (firmware / "other-link.bin").symlink_to("other.bin")
    prune_firmware(tmp_path)
    assert sorted(f.relative_to(firmware).as_posix() for f in firmware.rglob("*")) == [
        "v",
        "v/driver.bin",
        "v/revision.bin",
    ]


def test_copy_templates_with_the_modes_git_keeps(tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / "etc").mkdir(parents=True)
    (src / "etc").chmod(0o700)
    (src / "etc/config").write_text("")
    (src / "etc/config").chmod(0o600)
    (src / "etc/script").write_text("")
    (src / "etc/script").chmod(0o750)
    (src / "etc/link").symlink_to("/dev/null")
    copy_templates(src, dest, symlinks=True)
    modes = {p.relative_to(dest).as_posix(): p.lstat().st_mode & 0o7777 for p in dest.rglob("*") if not p.is_symlink()}
    assert modes == {"etc": 0o755, "etc/config": 0o644, "etc/script": 0o755}
    assert (dest / "etc/link").readlink().as_posix() == "/dev/null"
