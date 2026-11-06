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
    """Boot with the real CLI and QEMU, inspect the attached media through QMP, and quit, which fails the boot: the
    installer did not ask to reboot.
    """
    return attached(socket_path, [
        "boot", "--accel", "tcg", "--headless", "--memory", "512", "--cpus", "1", "--ssh-port", "0",
        "--arch", "x86_64_v2", "--qemu-arg=-S", "--qemu-arg=-qmp", f"--qemu-arg=unix:{socket_path},server=on,wait=off",
        *args,
    ], failure="QEMU was told to quit, like by xcraft vm stop, rather than the installer asking to reboot")  # fmt: skip


def attached(socket_path: Path, args: list[str], failure: str | None = None) -> list[Image]:
    """Run the real CLI with `args`, which launch QEMU with its QMP at `socket_path`, inspect the attached media through
    it, and quit, which succeeds, or fails with the message `failure`.
    """
    socket_path.unlink(missing_ok=True)
    cmd = [sys.executable, "-m", "xcraft", *args]
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
            assert vm.returncode == (1 if failure else 0), output
            assert failure is None or failure in " ".join(output.split()), output
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
    # started again from its disk, with the options of the boot, its QMP socket and paused CPUs too
    images = attached(tmp_path / "qmp", ["vm", "start", "--headless"])
    assert [image for image in images if image.format == "qcow2"] == [
        Image(filename=str(disk), format="qcow2", **{"virtual-size": 16 * 2**20})
    ]
    assert str(iso) not in {image.filename for image in images}
    assert disk.stat().st_ino == inode
    assert variables.stat().st_ino == vars_inode
    # No containers or build preparation are needed when using existing media or starting the installed disk.
    assert not (root / "xcraft.lock").exists()
    assert not (root / ".xcraft/cache").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
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


def test_vm_start_needs_a_session(root: Path) -> None:
    code, output = xcraft("vm", "--arch", "x86_64_v2", "start")
    assert code == 1, output
    assert "no VM session" in output
    session = root / ".xcraft/vm/x86_64_v2"
    session.mkdir(parents=True)
    # launched by an xcraft without xcraft vm start: without its options
    (session / "session.json").write_text(
        json.dumps({"pid": 0, "arch": "x86_64_v2", "disk": "disk.qcow2", "iso": None, "ssh_port": 0, "command": []})
    )
    code, output = xcraft("vm", "--arch", "x86_64_v2", "start")
    assert code == 1, output
    assert "launched by an older xcraft" in " ".join(output.split())


@pytest.mark.parametrize("edit", [["--kernel-args", "quiet"], ["--unattended"]])
def test_boot_kernel_args_need_the_grub_of_the_iso(root: Path, edit: list[str]) -> None:
    code, output = xcraft("boot", *edit, "--bios")
    assert code == 2, output
    assert "--kernel-args and --unattended edit the GRUB menu of the ISO" in " ".join(output.split())


def test_boot_unattended_needs_its_kernel_args(root: Path, tmp_path: Path) -> None:
    iso = tmp_path / "installer.iso"
    iso.touch()
    code, output = xcraft("boot", "--unattended", "--iso", str(iso))
    assert code == 2, output
    assert "no unattended kernel arguments" in output
    assert not (root / ".xcraft").exists()


# GRUB's editor, as it shows the default entry of host-installer's grub.cfg
EDITOR = """
                                                       GRUB version 2.12
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                                                                              │
 │setparams 'install'                                                                           │
 │                                                                                              │
 │    multiboot2 /boot/xen.gz dom0_max_vcpus=1-16 dom0_mem=max:8192M com1=115200,8n1 console=com1│
 │    module2 /boot/vmlinuz console=hvc0 console=tty0                                           │
 │    module2 /install.img                                                                      │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘
""".splitlines()


def test_kernel_line() -> None:
    assert vm.kernel_line(EDITOR) == 3
    with pytest.raises(RuntimeError, match="no kernel"):
        vm.kernel_line([line.replace("vmlinuz", "linux") for line in EDITOR])


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


def stop_vm(directory: Path) -> None:
    assert xcraft("vm", "--vm-dir", str(directory), "stop")[0] == 0
    for _ in range(100):
        if not vm.running(directory):
            return
        time.sleep(0.05)


@pytest.mark.skipif(not (shutil.which("qemu-system-x86_64") and shutil.which("qemu-img")), reason="needs QEMU")
def test_detached_vm_controls_and_logs(root: Path, tmp_path: Path, echo_disk: Path) -> None:
    directory = root / ("long-session-name-" * 4)
    iso = tmp_path / "installer.iso"
    iso.write_bytes(bytes(2048))
    boot = [
        sys.executable, "-m", "xcraft", "boot", "--iso", str(iso), "--bios", "--background", "--accel", "tcg",
        "--memory", "512", "--cpus", "1", "--ssh-port", "0", "--disk", str(echo_disk), "--vm-dir", str(directory),
    ]  # fmt: skip
    started = subprocess.run(boot, capture_output=True, text=True, check=False, timeout=20)
    assert started.returncode == 0, started.stdout + started.stderr

    def control(*args: str) -> tuple[int, str]:
        return xcraft("vm", "--vm-dir", str(directory), *args)

    # started again from its disk, the echo one, without the ISO
    stop_vm(directory)
    start = [sys.executable, "-m", "xcraft", "vm", "--vm-dir", str(directory), "start", "--background"]
    started = subprocess.run(start, capture_output=True, text=True, check=False, timeout=20)
    assert started.returncode == 0, started.stdout + started.stderr
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
        for again in (boot, start):
            failed = subprocess.run(again, capture_output=True, text=True, check=False, timeout=20)
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
        stop_vm(directory)
    result, output = control("status")
    assert result == 0, output
    assert json.loads(output)["status"] == "stopped"
    assert control("text", "no guest")[0] != 0
    bundle = tmp_path / "stopped-diagnostics"
    assert control("logs", str(bundle))[0] == 0
    assert (bundle / "serial.log").exists()


def test_ssh_port_in_use(root: Path, tmp_path: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        # the first free one by default, after those in use, like by another VM
        assert not vm.port_free(port)
        assert vm.free_port(port) > port
        # and an error for one given in use, before making anything
        iso = tmp_path / "installer.iso"
        iso.write_bytes(bytes(2048))
        code, output = xcraft("boot", "--iso", str(iso), "--bios", "--accel", "tcg", "--ssh-port", str(port))
        assert code == 2, output
        assert f"port {port} is in use on localhost" in " ".join(output.split())
        assert not (root / ".xcraft").exists()


def test_qemu_failure_shown(tmp_path: Path) -> None:
    (tmp_path / "qemu.log").write_text("qemu-system-x86_64: -nic user: Could not set up host forwarding rule\n")
    assert vm._failure(tmp_path) == ": qemu-system-x86_64: -nic user: Could not set up host forwarding rule"
