"""Persistent local QEMU sessions and their control transports."""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Self

import pyte
from pydantic import BaseModel, Field, JsonValue

if TYPE_CHECKING:
    from types import TracebackType


class Session(BaseModel):
    """The launch details retained after the launching CLI exits."""

    pid: int
    arch: str
    disk: Path
    iso: Path | None
    ssh_port: int
    command: list[str]


class Reply(BaseModel):
    """A QMP response; asynchronous events have neither result nor error."""

    result: JsonValue = Field(default=None, alias="return")
    error: dict[str, JsonValue] | None = None


def connect(path: Path) -> socket.socket:
    """Connect to a VM socket, including paths longer than Unix socket limits on Linux."""
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
    """A short-lived QMP connection, independent of the launching process."""

    def __init__(self, directory: Path) -> None:
        """Connect and negotiate QMP capabilities."""
        self.client = connect(directory / "qmp.sock")
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
    """Check the monitor rather than trusting a potentially recycled PID."""
    try:
        with Monitor(directory) as monitor:
            monitor.execute("query-status")
    except (OSError, RuntimeError):
        return False
    return True


def firmware(code: Path | None, template: Path | None) -> tuple[Path, Path]:
    """Locate a matching OVMF code/variables pair, or use explicit paths."""
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


def check_stopped(directory: Path) -> None:
    """Refuse to replace a live session's endpoints or launch details."""
    if running(directory):
        msg = f"a VM is already running in {directory}: use xcraft vm to control it"
        raise RuntimeError(msg)


def command(  # noqa: PLR0913
    directory: Path,
    *,
    name: str,
    disk: Path,
    iso: Path | None,
    memory: int,
    cpus: int,
    ssh_port: int,
    accel: str,
    headless: bool,
    ovmf: tuple[Path, Path] | None,
) -> list[str]:
    """Build the QEMU command and preserve the VM's writable firmware variables."""
    network = "user,model=e1000" + (f",hostfwd=tcp:127.0.0.1:{ssh_port}-:22" if ssh_port else "")
    cmd = [
        "qemu-system-x86_64", "-name", f"xcraft-{name}", "-accel", accel, "-machine", "q35",
        "-cpu", "host" if accel == "kvm" else "max", "-smp", str(cpus), "-m", str(memory),
        "-drive", f"file={str(disk).replace(',', ',,')},format=qcow2,if=ide", "-nic", network,
        "-qmp", "unix:qmp.sock,server=on,wait=off", "-monitor", "none",
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
    return cmd


def launch(directory: Path, session: Session) -> subprocess.Popen[bytes]:
    """Launch QEMU, preserving previous transcripts and waiting for its monitor."""
    (directory / "qmp.sock").unlink(missing_ok=True)
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
    (directory / "session.json").write_text(session.model_dump_json(indent=2) + "\n")
    try:
        for _ in range(200):
            if proc.poll() is not None:
                if proc.returncode == 0:
                    return proc
                msg = f"QEMU exited with {proc.returncode}: see {directory / 'qemu.log'}"
                raise RuntimeError(msg)  # noqa: TRY301
            if running(directory):
                return proc
            time.sleep(0.05)
        msg = f"QEMU did not open its monitor: see {directory / 'qemu.log'}"
        raise RuntimeError(msg)  # noqa: TRY301
    except BaseException:
        proc.terminate()
        proc.wait(timeout=10)
        raise


def wait(proc: subprocess.Popen[bytes], directory: Path) -> None:
    """Wait for a foreground VM, stopping it on Ctrl-C."""
    try:
        if code := proc.wait():
            raise subprocess.CalledProcessError(code, proc.args)
    except KeyboardInterrupt:
        if running(directory):
            with Monitor(directory) as monitor:
                monitor.execute("quit")
        proc.wait(timeout=10)
        raise


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


def collect(directory: Path, destination: Path) -> None:
    """Copy host-side diagnostics, including a screenshot when QEMU is reachable."""
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
