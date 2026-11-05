"""The helpers making the tree of the ISO, which run in the build environment otherwise."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from xcraft.buildenv.iso import prune_firmware

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
