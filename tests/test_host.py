from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from xcraft import host

if TYPE_CHECKING:
    from pathlib import Path

CONF = ":qemu-aarch64:M::\\x7fELF\\x02:\\xff\\xff:/usr/bin/qemu-aarch64-static:F\n"


@pytest.fixture
def binfmt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proc, conf = tmp_path / "binfmt_misc", tmp_path / "binfmt.d"
    proc.mkdir()
    conf.mkdir()
    (proc / "status").write_text("enabled\n")
    (proc / "qemu-arm").write_text("enabled\ninterpreter /usr/bin/qemu-arm-static\nflags: F\n")
    (conf / "qemu-aarch64-static.conf").write_text(CONF)
    monkeypatch.setattr(host, "_BINFMT", proc)
    monkeypatch.setattr(host, "_BINFMT_D", conf)
    return proc


def test_emulation(binfmt: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert host.emulation_problem("aarch64") == "no emulation of aarch64, to build for it: install qemu-user-static"
    (binfmt / "qemu-aarch64").write_text("disabled\ninterpreter /usr/bin/qemu-aarch64-static\nflags: F\n")
    assert host.binfmt("aarch64") is None
    (binfmt / "qemu-aarch64").write_text("enabled\ninterpreter /usr/bin/qemu-aarch64-static\nflags: F\noffset 0\n")
    assert host.binfmt("aarch64") == ("qemu-aarch64", "F")
    assert "qemu-aarch64 having no C flag: run xcraft doctor --fix" in host.emulation_problem("aarch64")

    conf, flags, text = host.emulation_fix("aarch64")
    assert (conf.name, flags, text) == ("qemu-aarch64-static.conf", "F", CONF.replace(":F\n", ":FOC\n"))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: calls.append((cmd, kwargs.get("input"))))
    host.fix_emulation([(conf, text)])
    assert calls == [
        (["sudo", "tee", "/etc/binfmt.d/qemu-aarch64-static.conf"], CONF.replace(":F\n", ":FOC\n")),
        (["sudo", "systemctl", "restart", "systemd-binfmt"], None),
    ]
    (binfmt / "qemu-aarch64").write_text("enabled\ninterpreter /usr/bin/qemu-aarch64-static\nflags: OCF\n")
    assert host.emulation_problem("aarch64") == ""
