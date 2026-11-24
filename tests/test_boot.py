"""Real QEMU media attachment, persistent sessions, and guest console transports."""

from __future__ import annotations

import fcntl
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
import typer
from pydantic import BaseModel, Field, TypeAdapter

from tests import xcraft
from xcraft import vm
from xcraft.commands import boot
from xcraft.commands.boot import answered


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
def should_start_again_from_the_disk_it_created(unprepared_root: Path, tmp_path: Path) -> None:
    try:
        code, _ = vm.firmware(None, None)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    # Blank CD media is enough to inspect attachment: -S keeps the CPUs paused throughout the test.
    iso = tmp_path / "installer.iso"
    iso.write_bytes(bytes(2048))
    disk = unprepared_root / ".xcraft/vm/x86_64_v2/disk.qcow2"
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
    assert not (unprepared_root / "xcraft.lock").exists()
    assert not (unprepared_root / ".xcraft/cache").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--accel", "unknown"], "--accel must be kvm or tcg"),
        (["--arch", "aarch64", "--bios"], "--bios is for x86 only, not aarch64, which boots with UEFI"),
        (["--memory", "0"], "'--memory': 0 is not in the range x>=512"),
        (["--cpus", "0"], "'--cpus': 0 is not in the range x>=1"),
    ],
)
def should_reject_invalid_options_before_making_anything(unprepared_root: Path, args: list[str], message: str) -> None:
    code, output = xcraft("boot", *args)
    assert code == 2, output
    assert message in " ".join(output.split())
    assert not (unprepared_root / ".xcraft").exists()


def should_refuse_a_vm_directory_of_other_content(unprepared_root: Path, tmp_path: Path) -> None:
    (tmp_path / "mine").write_text("kept")
    code, output = xcraft("boot", "--vm-dir", str(tmp_path))
    assert code == 2, output
    assert "has content xcraft did not make" in " ".join(output.replace("│", "").split())
    assert (tmp_path / "mine").read_text() == "kept"


@pytest.mark.usefixtures("unprepared_root")
def should_refuse_vm_commands_without_a_session() -> None:
    for command in ("start", "ssh", "ssh-copy-id"):
        code, output = xcraft("vm", "--arch", "x86_64_v2", command)
        assert code == 1, output
        assert "no VM session" in output


def old_session(root: Path) -> None:
    """Write the session of a VM launched by an xcraft without xcraft vm start: without its options."""
    session = root / ".xcraft/vm/x86_64_v2"
    session.mkdir(parents=True)
    (session / "session.json").write_text(
        json.dumps({"pid": 0, "arch": "x86_64_v2", "disk": "disk.qcow2", "iso": None, "ssh_port": 0, "command": []})
    )


def should_refuse_to_start_vms_of_older_xcraft_versions(unprepared_root: Path) -> None:
    old_session(unprepared_root)
    code, output = xcraft("vm", "--arch", "x86_64_v2", "start")
    assert code == 1, output
    assert "launched by an older xcraft" in " ".join(output.split())


def should_refuse_ssh_to_a_vm_not_running(unprepared_root: Path) -> None:
    old_session(unprepared_root)
    for command in ("ssh", "ssh-copy-id"):
        code, output = xcraft("vm", "--arch", "x86_64_v2", command)
        assert code == 1, output
        assert "no VM running in" in " ".join(output.split())


@pytest.mark.usefixtures("unprepared_root")
def should_refuse_grub_menu_edits_when_booting_with_bios() -> None:
    code, output = xcraft("boot", "--kernel-args", "quiet", "--bios")
    assert code == 2, output
    assert "--kernel-args and --unattended edit the GRUB menu of the ISO" in " ".join(output.split())


def should_refuse_unattended_boot_without_answer_file(unprepared_root: Path, tmp_path: Path) -> None:
    iso = tmp_path / "installer.iso"
    iso.touch()
    code, output = xcraft("boot", "--unattended", "--iso", str(iso))
    assert code == 2, output
    assert "no unattended answer file" in output
    assert not (unprepared_root / ".xcraft").exists()


def should_refuse_answers_without_unattended_boot(unprepared_root: Path, tmp_path: Path) -> None:
    iso = tmp_path / "installer.iso"
    iso.touch()
    code, output = xcraft("boot", "--answer", "keymap=fr", "--iso", str(iso))
    assert code == 2, output
    assert "--answer changes the answer file of --unattended" in " ".join(output.split())
    assert not (unprepared_root / ".xcraft").exists()


def should_set_the_answers_in_the_answer_file() -> None:
    text = (
        '<?xml version="1.0"?>\n<!-- for test VMs -->\n<installation srtype="ext">\n'
        "  <primary-disk>sda</primary-disk>\n  <keymap>us</keymap>\n</installation>\n"
    )
    edited = answered(text, ["keymap=fr", "primary-disk.fs-type=ext4", "system-snapshot.name=installed"])
    # the text and the attributes of the elements, those missing added, the comments kept
    assert "<!-- for test VMs -->" in edited
    assert '<primary-disk fs-type="ext4">sda</primary-disk>' in edited
    assert "<keymap>fr</keymap>" in edited
    assert '<system-snapshot name="installed" />' in edited
    assert answered(text, []).count("<keymap>us</keymap>") == 1
    with pytest.raises(typer.BadParameter, match=r"not NAME=VALUE nor NAME\.ATTR=VALUE"):
        answered(text, ["keymap"])


def should_serve_the_answer_file_to_the_guest(tmp_path: Path) -> None:
    response = vm.answer_response(tmp_path, "<installation/>\n")
    assert response.read_bytes() == (
        b"HTTP/1.0 200 OK\r\nContent-Type: application/xml\r\nContent-Length: 16\r\n\r\n<installation/>\n"
    )
    launch = vm.Launch(name="test", memory=512, cpus=1, accel="tcg", ovmf=None, qemu_args=[])
    cmd = vm.qemu_command(
        tmp_path, launch, disk=tmp_path / "disk", iso=None, ssh_port=0, headless=True, answer=response
    )
    assert f"guestfwd=tcp:10.0.2.100:80-cmd:cat {response}" in cmd[cmd.index("-nic") + 1]


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


def should_find_the_kernel_line_in_the_grub_editor() -> None:
    assert vm.kernel_line(EDITOR) == 3
    with pytest.raises(RuntimeError, match="no kernel"):
        vm.kernel_line([line.replace("vmlinuz", "linux") for line in EDITOR])


def should_fail_when_qemu_is_not_installed(
    unprepared_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso = tmp_path / "installer.iso"
    iso.touch()
    monkeypatch.setenv("PATH", "")
    code, output = xcraft("boot", "--iso", str(iso), "--accel", "tcg", "--arch", "x86_64_v2")
    assert code == 1, output
    assert "qemu-system-x86_64 is not installed" in output
    assert not (unprepared_root / ".xcraft").exists()
    code, output = xcraft("boot", "--iso", str(iso), "--arch", "aarch64")
    assert code == 1, output
    assert "qemu-system-aarch64 is not installed" in output


def should_boot_aarch64_vms_on_the_virt_machine_with_uefi(tmp_path: Path) -> None:
    firmware = (tmp_path / "code.fd", tmp_path / "vars.fd")
    for path in firmware:
        path.touch()
    launch = vm.Launch(name="test", memory=512, cpus=1, accel="tcg", ovmf=firmware, qemu_args=[], arch="aarch64")
    cmd = vm.qemu_command(tmp_path, launch, disk=tmp_path / "disk", iso=tmp_path / "iso", ssh_port=2222, headless=True)
    # EL2 for Xen, the core of the Ampere Altra, virtio, the machine having no IDE, the ISO first
    assert cmd[:2] == ["qemu-system-aarch64", "-name"]
    assert cmd[cmd.index("-machine") + 1] == "virt,virtualization=on,gic-version=3"
    assert cmd[cmd.index("-cpu") + 1] == "neoverse-n1"
    assert "virtio-blk-pci,drive=disk,bootindex=1" in cmd
    assert "scsi-cd,drive=cdrom,bus=scsi.0,bootindex=0" in cmd
    assert cmd[cmd.index("-nic") + 1].startswith("user,model=virtio-net-pci,hostfwd=")
    assert "pvpanic-pci" in cmd
    assert "-vga" not in cmd
    assert (tmp_path / "OVMF_VARS.fd").exists()
    # an x86 VM as before, like those of the sessions of earlier versions, without their architecture
    old = vm.Launch.model_validate({k: v for k, v in launch.model_dump().items() if k != "arch"})
    cmd = vm.qemu_command(tmp_path, old, disk=tmp_path / "disk", iso=None, ssh_port=0, headless=True)
    assert cmd[0] == "qemu-system-x86_64"
    assert cmd[cmd.index("-machine") + 1] == "q35"
    assert cmd[cmd.index("-cpu") + 1] == "max"
    assert "ide-hd,drive=disk,bus=ide.0,bootindex=0" in cmd


def should_find_the_uefi_firmware_of_the_architecture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for arch, directory, names in (
        ("x86_64", "x64", ("OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd")),
        ("aarch64", "aarch64", ("QEMU_EFI-pflash.raw", "vars-template-pflash.raw")),
    ):
        (tmp_path / directory).mkdir()
        for name in names:
            (tmp_path / directory / name).touch()
        monkeypatch.setitem(vm._FIRMWARES, arch, [(str(tmp_path / directory), *names)])
    assert vm.firmware(None, None, "x86_64_v2")[0].name == "OVMF_CODE.4m.fd"
    assert vm.firmware(None, None, "aarch64")[0].name == "QEMU_EFI-pflash.raw"
    monkeypatch.setitem(vm._FIRMWARES, "aarch64", [])
    with pytest.raises(RuntimeError, match=r"AAVMF \(edk2-aarch64\) is not installed"):
        vm.firmware(None, None, "aarch64")


@pytest.mark.usefixtures("unprepared_root")
def should_accelerate_the_vms_of_the_architecture_of_the_host_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "uname", lambda: os.uname_result(("Linux", "h", "6", "1", "x86_64")))
    assert (vm.default_accel("x86_64_v2"), vm.default_accel("aarch64")) == ("kvm", "tcg")
    code, output = xcraft("boot", "--arch", "aarch64", "--accel", "kvm")
    assert code == 1, output
    assert "KVM only runs the VMs of the architecture of the host, x86_64: use --accel tcg" in output
    monkeypatch.setattr(os, "uname", lambda: os.uname_result(("Linux", "h", "6", "1", "aarch64")))
    assert (vm.default_accel("x86_64_v2"), vm.default_accel("aarch64")) == ("tcg", "kvm")


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


def should_show_vm_help_outside_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    code, output = xcraft("vm", "status", "--help")
    assert code == 0, output
    assert "QEMU status" in output


def should_require_a_project_for_vm_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    code, output = xcraft("vm", "status")
    assert code == 1, output
    assert "no xcraft.toml" in output


def stop_vm(directory: Path) -> None:
    assert xcraft("vm", "--vm-dir", str(directory), "stop")[0] == 0
    for _ in range(100):
        if not vm.running(directory):
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the VM of {directory} still runs 5s after xcraft vm stop")


@pytest.mark.skipif(not (shutil.which("qemu-system-x86_64") and shutil.which("qemu-img")), reason="needs QEMU")
def should_control_a_background_vm_and_collect_its_logs(unprepared_root: Path, tmp_path: Path, echo_disk: Path) -> None:  # noqa: PLR0915
    directory = unprepared_root / ("long-session-name-" * 4)
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
        with vm.Monitor(directory):  # which serves one command at a time: running all the same
            failed = subprocess.run(start, capture_output=True, text=True, check=False, timeout=20)
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
    with (directory / "launch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        failed = subprocess.run(start, capture_output=True, text=True, check=False, timeout=20)
        assert "another command is launching a VM" in failed.stdout + failed.stderr
    assert control("text", "no guest")[0] != 0
    bundle = tmp_path / "stopped-diagnostics"
    assert control("logs", str(bundle))[0] == 0
    assert (bundle / "serial.log").exists()


@pytest.mark.skipif(not (shutil.which("qemu-system-x86_64") and shutil.which("qemu-img")), reason="needs QEMU")
def should_destroy_the_vm_but_keep_a_given_disk(unprepared_root: Path, tmp_path: Path, echo_disk: Path) -> None:
    directory = unprepared_root / "vm"
    iso = tmp_path / "installer.iso"
    iso.write_bytes(bytes(2048))
    boot = [
        sys.executable, "-m", "xcraft", "boot", "--iso", str(iso), "--bios", "--background", "--accel", "tcg",
        "--memory", "512", "--cpus", "1", "--ssh-port", "0", "--disk", str(echo_disk), "--vm-dir", str(directory),
    ]  # fmt: skip
    started = subprocess.run(boot, capture_output=True, text=True, check=False, timeout=20)
    assert started.returncode == 0, started.stdout + started.stderr
    destroy = [sys.executable, "-m", "xcraft", "vm", "--vm-dir", str(directory), "destroy"]
    try:
        # asked first: nothing changes without a yes
        kept = subprocess.run(destroy, input="n\n", capture_output=True, text=True, check=False, timeout=20)
        assert kept.returncode == 1, kept.stdout + kept.stderr
        assert vm.running(directory)
        destroyed = subprocess.run([*destroy, "--yes"], capture_output=True, text=True, check=False, timeout=20)
        assert destroyed.returncode == 0, destroyed.stdout + destroyed.stderr
        assert destroyed.stdout == f"destroyed, keeping the disk {echo_disk}\n"
    finally:
        if directory.exists() and vm.running(directory):
            stop_vm(directory)
    assert not directory.exists()
    assert echo_disk.exists()  # given with --disk, out of the directory
    code, output = xcraft("vm", "--vm-dir", str(directory), "destroy", "--yes")
    assert code == 1, output
    assert "no VM session" in output


def should_skip_ssh_ports_in_use() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        # after those in use, like by another VM
        assert not vm.port_free(port)
        assert vm.free_port(port) > port


def should_refuse_an_ssh_port_in_use(unprepared_root: Path, tmp_path: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        # before making anything
        iso = tmp_path / "installer.iso"
        iso.write_bytes(bytes(2048))
        code, output = xcraft("boot", "--iso", str(iso), "--bios", "--accel", "tcg", "--ssh-port", str(port))
        assert code == 2, output
        assert f"port {port} is in use on localhost" in " ".join(output.split())
        assert not (unprepared_root / ".xcraft").exists()


def should_show_why_qemu_failed(tmp_path: Path) -> None:
    # like QEMU failing to forward the SSH port: what it says last
    error = "qemu-system-x86_64: -nic user: Could not set up host forwarding rule"
    command = ["sh", "-c", f"echo starting >&2; echo '{error}' >&2; exit 1"]
    session = vm.Session(pid=0, arch="x86_64_v2", disk=tmp_path / "disk", iso=None, ssh_port=0, command=command)
    with pytest.raises(RuntimeError, match=f"^QEMU exited with 1: {error}$"):
        vm.launch(tmp_path, session)


def should_tell_the_outcome_of_an_unattended_installation_from_its_log() -> None:
    log = (
        "INFO     [2026-11-24 19:09:20] ran ['chroot', '/tmp/root', 'dracut', '-f']; rc 0\n"
        "INFO     [2026-11-24 19:09:21] INSTALL FAILED.\n"
        "INFO     [2026-11-24 19:09:21] A fatal exception occurred:\n"
        "INFO     [2026-11-24 19:09:21] Traceback (most recent call last):\n"
        '  File "/opt/xensource/installer/backend.py", line 1011, in prepFallback\n'
        "    shutil.copyfile(src, dst)\n"
        "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/root/boot/xen-4.21.2-3.gz'\n"
        "\n"
        "During handling of the above exception, another exception occurred:\n"
        "INFO     [2026-11-24 19:09:42] An unrecoverable error has occurred.\n"
    )
    failed = boot._unattended_outcome(log)
    assert failed is not None
    assert failed.startswith("the installation failed:\nINFO     [2026-11-24 19:09:21] INSTALL FAILED.")
    assert failed.endswith("No such file or directory: '/tmp/root/boot/xen-4.21.2-3.gz'")
    assert boot._unattended_outcome("INFO     The installation completed successfully.\n") is None
    # to the first exception, without a module, like the installer's own
    log = (
        "INFO     INSTALL FAILED.\nTraceback (most recent call last):\nException: The disk appears to be in use\nmore\n"
    )
    assert (boot._unattended_outcome(log) or "").endswith("\nException: The disk appears to be in use")
    # an installer stopped before either, like by a crash
    assert boot._unattended_outcome("INFO     Starting actual installation\n") == (
        "the installer told neither its success nor its failure"
    )
