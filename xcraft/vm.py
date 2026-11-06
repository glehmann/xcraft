"""Persistent local QEMU sessions and their control transports."""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Self

import pyte
from pydantic import BaseModel, Field, JsonValue

from xcraft.project import write_atomically

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import TracebackType


class Launch(BaseModel):
    """The options of the VM xcraft boot launched, for xcraft vm start to start it again from its disk."""

    name: str
    memory: int
    cpus: int
    accel: str
    ovmf: tuple[Path, Path] | None
    qemu_args: list[str]


class Reply(BaseModel):
    """A QMP response; asynchronous events have neither result nor error."""

    result: JsonValue = Field(default=None, alias="return")
    error: dict[str, JsonValue] | None = None


def connect(path: Path) -> socket.socket:
    """Return a connection to the socket at `path`, even longer than Unix sockets allow, through /proc on Linux."""
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(10)
    try:
        if len(os.fsencode(path)) < 104:  # noqa: PLR2004  # Unix socket address limit
            client.connect(str(path))
        else:
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                client.connect(f"/proc/self/fd/{fd}/{path.name}")
            finally:
                os.close(fd)
    except BaseException:
        client.close()
        raise
    return client


class Monitor:
    """A short-lived QMP connection, independent of the launching process: to the monitor of the commands, `qmp.sock`,
    or to the one of the events, `events.sock`, see launch() and stop_reason().
    """

    def __init__(self, directory: Path, name: str = "qmp.sock") -> None:
        """Connect and negotiate QMP capabilities."""
        self.client = connect(directory / name)
        self.stream = self.client.makefile("rw", encoding="utf-8")
        try:
            greeting = json.loads(self.stream.readline())
            if "QMP" not in greeting:
                msg = "not a QMP monitor"
                raise RuntimeError(msg)  # noqa: TRY301
            self.execute("qmp_capabilities")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __enter__(self) -> Self:
        """Keep the connection open for a group of commands."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Close the stream and socket."""
        self.stream.close()
        self.client.close()

    def execute(self, command: str, arguments: dict[str, JsonValue] | None = None) -> JsonValue:
        """Execute a QMP command, skipping asynchronous notifications."""
        self.stream.write(json.dumps({"execute": command, "arguments": arguments or {}}) + "\n")
        self.stream.flush()
        while line := self.stream.readline():
            reply = Reply.model_validate_json(line)
            if reply.error:
                raise RuntimeError(str(reply.error.get("desc", reply.error)))
            if "result" in reply.model_fields_set:
                return reply.result
        msg = "QEMU closed its monitor"
        raise RuntimeError(msg)


def running(directory: Path) -> bool:
    """Return whether QEMU runs in `directory`: its monitor answers, rather than a PID maybe of another process now."""
    try:
        with Monitor(directory) as monitor:
            monitor.execute("query-status")
    except (OSError, RuntimeError):
        return False
    return True


def _failure(directory: Path) -> str:
    """Return why QEMU failed, from the end of its log."""
    lines = (directory / "qemu.log").read_text(errors="replace").strip().splitlines()
    return f": {lines[-1]}" if lines else f": see {directory / 'qemu.log'}"


def launch(directory: Path, session: Session) -> tuple[subprocess.Popen[bytes], Monitor | None]:
    """Launch QEMU, preserving previous transcripts and waiting for its monitor, and return it with the connection to
    its monitor of the events, when its command has one, see qemu_command(): QEMU waits for it before starting the
    guest, so that no event is missed.
    """
    for name in ("qmp.sock", "events.sock"):
        (directory / name).unlink(missing_ok=True)
    fifo = directory / "serial.in"
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo, 0o600)
    for name in ("serial.log", "qemu.log"):
        path = directory / name
        if path.exists():
            path.rename(path.with_name(f"{name}.{time.time_ns()}"))
    # QEMU retains both ends on stdin, so disconnecting a writer never sends EOF.
    fd = os.open(fifo, os.O_RDWR)
    try:
        with (directory / "qemu.log").open("wb") as errors, (directory / "serial.log").open("wb") as output:
            proc = subprocess.Popen(
                session.command,
                cwd=directory,
                stdin=fd,
                stdout=output,
                stderr=errors,
                start_new_session=True,
            )
    finally:
        os.close(fd)
    session.pid = proc.pid
    write_atomically(directory / "session.json", session.model_dump_json(indent=2) + "\n")
    try:
        return proc, _started(proc, directory, events=_EVENTS in session.command)
    except BaseException:
        proc.terminate()
        proc.wait(timeout=10)
        raise


def _started(proc: subprocess.Popen[bytes], directory: Path, *, events: bool) -> Monitor | None:
    """Wait for the monitor of QEMU running `proc` in `directory`, and return the connection to its monitor of the
    events with `events`, which it waits for first.
    """
    monitor = None
    try:
        for _ in range(200):
            if proc.poll() is not None:
                if proc.returncode == 0:
                    return monitor
                msg = f"QEMU exited with {proc.returncode}{_failure(directory)}"
                raise RuntimeError(msg)  # noqa: TRY301
            if events and not monitor and (directory / "events.sock").exists():
                monitor = Monitor(directory, "events.sock")
            if (monitor or not events) and running(directory):
                return monitor
            time.sleep(0.05)
        msg = f"QEMU did not open its monitor: see {directory / 'qemu.log'}"
        raise RuntimeError(msg)  # noqa: TRY301
    except BaseException:
        if monitor:
            monitor.__exit__(None, None, None)
        raise


class Session(BaseModel):
    """The launch details retained after the launching CLI exits."""

    pid: int
    arch: str
    disk: Path
    iso: Path | None
    ssh_port: int
    command: list[str]
    launch: Launch | None = None  # None: launched by an xcraft without xcraft vm start


SSH_DESTINATION = "root@127.0.0.1"
"""Where ssh reaches the VM, on its SSH port of localhost."""


def ssh_options(port: int) -> list[str]:
    """Return the options of ssh reaching the VM on `port` of localhost: without checking its host key, which each
    installation changes on the same port.
    """
    return [
        "-p",
        str(port),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
    ]


def ssh_port(directory: Path) -> int:
    """Return the SSH port of the VM running in `directory`, failing when it does not run, or has none."""
    if not (directory / "session.json").is_file() or not running(directory):
        msg = f"no VM running in {directory}: see xcraft boot and xcraft vm start"
        raise RuntimeError(msg)
    if not (port := Session.model_validate_json((directory / "session.json").read_text()).ssh_port):
        msg = f"the VM of {directory} has no SSH port"
        raise RuntimeError(msg)
    return port


def firmware(code: Path | None, template: Path | None) -> tuple[Path, Path]:
    """Return the OVMF code and template of its variables: those given, or else those installed."""
    if bool(code) != bool(template):
        msg = "--firmware-code and --firmware-vars must be supplied together"
        raise RuntimeError(msg)
    if code and template:
        if not (code.is_file() and template.is_file()):
            msg = "OVMF firmware code and variables template must both be files"
            raise RuntimeError(msg)
        return code.absolute(), template.absolute()
    for directory, code_name, vars_name in [
        ("/usr/share/edk2/ovmf", "OVMF_CODE.fd", "OVMF_VARS.fd"),
        ("/usr/share/OVMF", "OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"),
        ("/usr/share/OVMF", "OVMF_CODE.fd", "OVMF_VARS.fd"),
        ("/usr/share/edk2/x64", "OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"),
    ]:
        code, template = Path(directory) / code_name, Path(directory) / vars_name
        if code.is_file() and template.is_file():
            return code, template
    msg = "OVMF is not installed: install it or supply --firmware-code and --firmware-vars"
    raise RuntimeError(msg)


def check_tools(accel: str, *, new_disk: bool) -> None:
    """Raise when QEMU, or qemu-img for a `new_disk`, is not installed, or KVM not accessible for the `accel` kvm."""
    for tool in ["qemu-system-x86_64", *(["qemu-img"] if new_disk else [])]:
        if not shutil.which(tool):
            msg = f"{tool} is not installed: install QEMU to run the VM"
            raise RuntimeError(msg)
    if accel == "kvm" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
        msg = "KVM is not accessible: enable it and grant access to /dev/kvm, or use --accel tcg"
        raise RuntimeError(msg)


def check_stopped(directory: Path) -> None:
    """Refuse to replace a live session's endpoints or launch details."""
    if running(directory):
        msg = f"a VM is already running in {directory}: use xcraft vm to control it"
        raise RuntimeError(msg)


_EVENTS = "unix:events.sock,server=on,wait=on"
"""The monitor of the events of QEMU, which waits for its connection before starting the guest, see launch()."""


def qemu_command(
    directory: Path,
    launch: Launch,
    *,
    disk: Path,
    iso: Path | None,
    ssh_port: int,
    headless: bool,
    events: bool = False,
) -> list[str]:
    """Return the QEMU command of the VM, copying the template of the firmware variables it writes, once, with a
    monitor of the events with `events`, which launch() connects to.
    """
    accel, ovmf = launch.accel, launch.ovmf
    network = "user,model=e1000" + (f",hostfwd=tcp:127.0.0.1:{ssh_port}-:22" if ssh_port else "")
    # OVMF exposes modes that fit in VRAM; limit it so the installer cannot select a huge framebuffer.
    cmd = [
        "qemu-system-x86_64", "-name", f"xcraft-{launch.name}", "-accel", accel, "-machine", "q35",
        "-cpu", "host" if accel == "kvm" else "max", "-smp", str(launch.cpus), "-m", str(launch.memory),
        "-vga", "std", "-global", "VGA.xres=1024", "-global", "VGA.yres=768",
        "-global", "VGA.vgamem_mb=4",
        "-drive", f"file={str(disk).replace(',', ',,')},format=qcow2,if=ide", "-nic", network,
        # QEMU's monitors take one connection at a time: the events have their own, see launch()
        *(["-qmp", _EVENTS] if events else []), "-qmp", "unix:qmp.sock,server=on,wait=off", "-monitor", "none",
        # a panic of a guest kernel with the driver of pvpanic stops it, rather than leaving it hung
        "-device", "pvpanic", "-action", "panic=shutdown",
        "-chardev", "stdio,id=serial,signal=off",
        "-serial", "chardev:serial", *(["-display", "none"] if headless else []),
    ]  # fmt: skip
    if ovmf:
        variables = directory / "OVMF_VARS.fd"
        if not variables.exists():
            shutil.copy2(ovmf[1], variables)
        cmd += [
            "-drive",
            f"if=pflash,format=raw,readonly=on,file={str(ovmf[0]).replace(',', ',,')}",
            "-drive",
            "if=pflash,format=raw,file=OVMF_VARS.fd",
        ]
    if iso:
        cmd += ["-cdrom", str(iso.absolute()), "-boot", "order=d,menu=on", "-no-reboot"]
    else:
        cmd += ["-boot", "order=c,menu=on"]
    return [*cmd, *launch.qemu_args]


def port_free(port: int) -> bool:
    """Return whether `port` is free on localhost, for QEMU to forward."""
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port(start: int = 2222) -> int:
    """Return the first port from `start` free on localhost."""
    if port := next((p for p in range(start, start + 100) if port_free(p)), None):
        return port
    msg = f"no free port from {start} to {start + 99} on localhost: give one with --ssh-port"
    raise RuntimeError(msg)


def wait(proc: subprocess.Popen[bytes], directory: Path, events: Monitor | None = None) -> str | None:
    """Wait for a foreground VM, stopping it on Ctrl-C, and return why it stopped, from the `events` of QEMU, if given:
    see stop_reason().
    """
    reason = None
    try:
        if events:
            reason = stop_reason(events)
        if code := proc.wait():
            raise subprocess.CalledProcessError(code, proc.args)
    except KeyboardInterrupt:
        if running(directory):
            with Monitor(directory) as monitor:
                monitor.execute("quit")
        proc.wait(timeout=10)
        raise
    return reason


def stop_reason(events: Monitor) -> str | None:
    """Return why the guest stopped, from the events of QEMU until it exits: the reason of its SHUTDOWN event, like
    guest-reset, which -no-reboot turns into a stop, guest-shutdown, guest-panic or host-qmp-quit; None without one.
    """
    events.client.settimeout(None)  # as long as the guest runs
    reason = None
    while line := events.stream.readline():
        if (event := json.loads(line)).get("event") == "SHUTDOWN":
            reason = event["data"]["reason"]
    return reason


def press(monitor: Monitor, keys: list[str]) -> None:
    """Press a QEMU key-code chord, releasing it before the next command."""
    monitor.execute("send-key", {"keys": [{"type": "qcode", "data": key} for key in keys], "hold-time": 1})
    time.sleep(0.02)


def send(directory: Path, data: bytes) -> None:
    """Write serial input without blocking forever on a stopped or unresponsive guest."""
    fd = os.open(directory / "serial.in", os.O_WRONLY | os.O_NONBLOCK)
    deadline = time.monotonic() + 10
    try:
        while data:
            if not select.select([], [fd], [], max(0, deadline - time.monotonic()))[1]:
                msg = "timed out sending serial input"
                raise TimeoutError(msg)
            try:
                data = data[os.write(fd, data) :]
            except BlockingIOError:
                continue
    finally:
        os.close(fd)


def type_text(monitor: Monitor, text: str) -> None:
    """Type text using the US keyboard layout, for shells on graphical virtual terminals."""
    punctuation = dict(
        zip(
            " `-=[]\\;',./\n\t",
            [
                "spc",
                "grave",
                "minus",
                "equal",
                "bracket_left",
                "bracket_right",
                "backslash",
                "semicolon",
                "apostrophe",
                "comma",
                "dot",
                "slash",
                "ret",
                "tab",
            ],
            strict=True,
        )
    )
    shifted = dict(zip('~_+{}|:"<>?!@#$%^&*()', "`-=[]\\;',./1234567890", strict=True))
    chords: list[list[str]] = []
    for character in text:
        base = shifted.get(character, character.lower())
        if base not in punctuation and not (base.isascii() and base.isalnum()):
            msg = f"cannot type {character!r} with the US keyboard mapping"
            raise ValueError(msg)
        chords.append(
            [*(["shift"] if character in shifted or character.isupper() else []), punctuation.get(base, base)]
        )
    for chord in chords:
        press(monitor, chord)


def kernel_line(screen: Sequence[str]) -> int:
    """Return how many lines below the first one of the entry in GRUB's editor, on its `screen`, the kernel's is."""
    first = next((i for i, line in enumerate(screen) if "setparams" in line), None)
    kernel = next((i for i, line in enumerate(screen) if "/vmlinuz" in line), None)
    if first is None or kernel is None:
        msg = "no kernel in the default boot entry, in GRUB's editor"
        raise RuntimeError(msg)
    return kernel - first


def _render(data: bytes) -> list[str]:
    """Return the screen of serial output, wide enough for GRUB's on the firmware's console."""
    screen = pyte.Screen(200, 60)
    pyte.ByteStream(screen).feed(data)
    return screen.display


def _wait_serial(directory: Path, found: Callable[[bytes], bool], what: str, timeout: float, start: int = 0) -> bytes:
    """Return the serial output from `start` once `found` in it, before `timeout`."""
    deadline = time.monotonic() + timeout
    while not found(data := (directory / "serial.log").read_bytes()[start:]):
        if time.monotonic() > deadline:
            msg = f"timed out waiting for {what} on the serial console"
            raise TimeoutError(msg)
        if not running(directory):
            msg = "QEMU stopped"
            raise RuntimeError(msg)
        time.sleep(0.1)
    return data


def edit_default_entry(directory: Path, args: str, timeout: float = 120) -> None:
    """Add `args` to the kernel command line of the default entry of the GRUB menu, in GRUB's editor, and wait for the
    kernel booted with them.
    """
    _wait_serial(directory, lambda data: b"Press enter to boot" in data, "GRUB's menu", timeout)
    start = (directory / "serial.log").stat().st_size
    # QEMU's monitor takes one connection at a time, like the one of running(), which waiting checks
    with Monitor(directory) as monitor:
        press(monitor, ["e"])
    editor = _wait_serial(
        directory, lambda data: "/vmlinuz" in "".join(_render(data)), "the kernel in GRUB's editor", timeout, start
    )
    with Monitor(directory) as monitor:
        for _ in range(kernel_line(_render(editor))):
            press(monitor, ["down"])
        press(monitor, ["ctrl", "e"])
        type_text(monitor, f" {args}")
        press(monitor, ["ctrl", "x"])
    booted = re.compile(rb"Kernel command line: [^\r\n]*" + re.escape(args.encode()))
    _wait_serial(directory, lambda data: bool(booted.search(data)), "the kernel booted with them", timeout, start)


def collect(directory: Path, destination: Path) -> None:
    """Copy the session and logs of the VM to `destination`, with its serial console as text, and its status when
    QEMU answers.
    """
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("session.json", "serial.log", "qemu.log"):
        if (directory / name).is_file():
            shutil.copy2(directory / name, destination / name)
    if (destination / "serial.log").exists():
        screen = pyte.Screen(80, 24)
        pyte.ByteStream(screen).feed((destination / "serial.log").read_bytes())
        (destination / "console.txt").write_text("\n".join(screen.display) + "\n")
    if running(directory):
        with Monitor(directory) as monitor:
            (destination / "status.json").write_text(json.dumps(monitor.execute("query-status"), indent=2) + "\n")
            monitor.execute("screendump", {"filename": str((destination / "screen.png").absolute()), "format": "png"})
