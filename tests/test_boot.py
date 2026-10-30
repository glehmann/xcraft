"""Real QEMU media attachment, persistent sessions, and guest console transports."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tests import PROJECT, TOML
from tests.test_build import xcraft
from xcraft import vm


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    shutil.copytree(PROJECT, root)
    (root / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return root


class Image(BaseModel):
    """The disk image details reported by QEMU."""

    filename: str
    format: str
    size: int = Field(alias="virtual-size")


class Inserted(BaseModel):
    """A disk or CD-ROM inserted into a QEMU block device."""

    image: Image


class Block(BaseModel):
    """A QEMU block device, which can have no media."""

    inserted: Inserted | None = None


def boot_vm(socket_path: Path, *args: str) -> list[Image]:
    """Start the real CLI and QEMU, inspect the attached media through QMP, and quit."""
    socket_path.unlink(missing_ok=True)
    cmd = [
        sys.executable, "-m", "xcraft", "boot", "--accel", "tcg", "--headless", "--memory", "512", "--cpus", "1",
        "--ssh-port", "0", "--arch", "x86_64_v2", "--qemu-arg=-S", "--qemu-arg=-qmp",
        f"--qemu-arg=unix:{socket_path},server=on,wait=off", *args,
    ]  # fmt: skip
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True
    ) as vm:
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(10)
                for _ in range(200):
                    try:
                        client.connect(str(socket_path))
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        if vm.poll() is not None:
                            pytest.fail(vm.communicate()[0])
                        time.sleep(0.05)
                else:
                    pytest.fail("QEMU did not open its monitor")
                with client.makefile("rw") as monitor:
                    assert "QMP" in json.loads(monitor.readline())

                    def execute(command: str) -> object:
                        monitor.write(json.dumps({"execute": command}) + "\n")
                        monitor.flush()
                        while line := monitor.readline():
                            response: dict[str, object] = json.loads(line)
                            assert "error" not in response, response
                            if "return" in response:
                                return response["return"]
                        pytest.fail("QEMU closed its monitor")

                    execute("qmp_capabilities")
                    blocks = TypeAdapter(list[Block]).validate_python(execute("query-block"))
                    execute("quit")
            output = vm.communicate(timeout=10)[0]
            assert vm.returncode == 0, output
            return [block.inserted.image for block in blocks if block.inserted]
        finally:
            if vm.poll() is None:
                os.killpg(vm.pid, signal.SIGTERM)
                vm.communicate(timeout=10)


@pytest.mark.skipif(not (shutil.which("qemu-system-x86_64") and shutil.which("qemu-img")), reason="needs QEMU")
def test_boot_creates_disk_and_reuses_it(root: Path, tmp_path: Path) -> None:
    try:
        code, _ = vm.firmware(None, None)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    # Blank CD media is enough to inspect attachment: -S keeps the CPUs paused throughout the test.
    iso = tmp_path / "installer.iso"
    iso.write_bytes(bytes(2048))
    disk = root / ".xcraft/vm/x86_64_v2/disk.qcow2"
    images = boot_vm(tmp_path / "qmp", "--iso", str(iso), "--disk-size", "16M")
    assert {image.filename for image in images} == {str(disk), str(iso), str(code), "OVMF_VARS.fd"}
    assert next(image for image in images if image.format == "qcow2").size == 16 * 2**20
    inode = disk.stat().st_ino
    variables = disk.parent / "OVMF_VARS.fd"
    vars_inode = variables.stat().st_ino
    images = boot_vm(tmp_path / "qmp", "--installed", "--disk-size", "32M")
    assert [image for image in images if image.format == "qcow2"] == [
        Image(filename=str(disk), format="qcow2", **{"virtual-size": 16 * 2**20})
    ]
    assert str(iso) not in {image.filename for image in images}
    assert disk.stat().st_ino == inode
    assert variables.stat().st_ino == vars_inode
    # No containers or build preparation are needed when using existing media or booting the installed disk.
    assert not (root / "xcraft.lock").exists()
    assert not (root / ".xcraft/cache").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--installed"], "no installed disk"),
        (["--accel", "unknown"], "--accel must be kvm or tcg"),
        (["--arch", "aarch64"], "boot currently supports x86_64 and x86_64_v2 only"),
        (["--memory", "0"], "512"),
        (["--cpus", "0"], "1"),
    ],
)
def test_boot_invalid_options(root: Path, args: list[str], message: str) -> None:
    code, output = xcraft("boot", *args)
    assert code == 2, output
    assert message in " ".join(output.split())
    assert not (root / ".xcraft").exists()


def test_boot_conflicting_media(root: Path, tmp_path: Path) -> None:
    iso = tmp_path / "installer.iso"
    iso.touch()
    code, output = xcraft("boot", "--installed", "--iso", str(iso))
    assert code == 2
    assert "--installed and --iso cannot be used together" in output


def test_boot_missing_qemu(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    iso = tmp_path / "installer.iso"
    iso.touch()
    monkeypatch.setenv("PATH", "")
    code, output = xcraft("boot", "--iso", str(iso), "--accel", "tcg", "--arch", "x86_64_v2")
    assert code == 1
    assert "qemu-system-x86_64 is not installed" in output
    assert not (root / ".xcraft").exists()


@pytest.fixture
def echo_disk(tmp_path: Path) -> Path:
    """Create a BIOS boot sector that emits READY and echoes UART and keyboard input to COM1."""
    code = b"\x31\xc0\x8e\xd8\xba\xf8\x03"
    for byte in b"\x1b[2J\x1b[HREADY\r\n":
        code += b"\xb0" + bytes([byte]) + b"\xee"
    code += bytes.fromhex(
        "ba fd 03 ec a8 01 74 07 ba f8 03 ec ee eb f1 b4 01 cd 16 74 eb b4 00 cd 16 ba f8 03 ee eb e1"
    )
    raw = tmp_path / "boot.raw"
    raw.write_bytes((code.ljust(510, b"\0") + b"\x55\xaa").ljust(16 * 2**20, b"\0"))
    disk = tmp_path / "guest.qcow2"
    subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(raw), str(disk)], check=True)
    return disk


def test_vm_help_outside_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    code, output = xcraft("vm", "status", "--help")
    assert code == 0, output
    assert "QEMU status" in output
    code, output = xcraft("vm", "status")
    assert code == 1
    assert "no xcraft.toml" in output


@pytest.mark.skipif(not (shutil.which("qemu-system-x86_64") and shutil.which("qemu-img")), reason="needs QEMU")
def test_detached_vm_controls_and_logs(root: Path, tmp_path: Path, echo_disk: Path) -> None:
    directory = root / ("long-session-name-" * 4)
    boot = [
        sys.executable, "-m", "xcraft", "boot", "--installed", "--bios", "--background", "--accel", "tcg",
        "--memory", "512", "--cpus", "1", "--ssh-port", "0", "--disk", str(echo_disk), "--vm-dir", str(directory),
    ]  # fmt: skip
    started = subprocess.run(boot, capture_output=True, text=True, check=False, timeout=20)
    assert started.returncode == 0, started.stdout + started.stderr

    def control(*args: str) -> tuple[int, str]:
        return xcraft("vm", "--vm-dir", str(directory), *args)

    try:
        status = subprocess.run(
            [sys.executable, "-m", "xcraft", "vm", "--vm-dir", str(directory), "status"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert json.loads(status.stdout)["status"] == "running"
        result, output = control("console", "--wait", "READY", "--timeout", "10")
        assert result == 0, output
        assert "READY" in output
        assert control("text", "serial")[0] == 0
        result, output = control("console", "--wait", "serial", "--timeout", "5")
        assert result == 0, output
        assert control("text", "--physical", "--enter", "Keys_A+1")[0] == 0
        result, output = control("console", "--wait", "Keys_A+1", "--timeout", "5")
        assert result == 0, output
        assert control("key", "ctrl", "alt", "f2")[0] == 0
        failed = subprocess.run(boot, capture_output=True, text=True, check=False, timeout=20)
        assert failed.returncode != 0
        assert "already running" in failed.stdout + failed.stderr
        bundle = tmp_path / "diagnostics"
        result, output = control("logs", str(bundle))
        assert result == 0, output
        assert (bundle / "screen.png").read_bytes().startswith(b"\x89PNG")
        assert "Keys_A+1" in (bundle / "serial.log").read_text()
        assert (bundle / "session.json").exists()
        assert (bundle / "qemu.log").exists()
        assert "Keys_A+1" in (bundle / "console.txt").read_text()
        result, output = control("console", "--wait", "absent", "--timeout", "0.1")
        assert result != 0
        assert "READY" in output
        assert list(directory.glob("timeout-*/console.txt"))
    finally:
        assert control("stop")[0] == 0
    for _ in range(100):
        if not vm.running(directory):
            break
        time.sleep(0.05)
    result, output = control("status")
    assert result == 0, output
    assert json.loads(output)["status"] == "stopped"
    assert control("text", "no guest")[0] != 0
    bundle = tmp_path / "stopped-diagnostics"
    assert control("logs", str(bundle))[0] == 0
    assert (bundle / "serial.log").exists()
