"""The host: what xcraft needs on it, which xcraft doctor checks, like the emulation of the other architectures.

The containers of a platform the host cannot run are emulated by qemu-user, which the kernel runs through its
binfmt_misc registrations. They need the C flag, for sudo, which dnf runs with in the containers: without it, the
kernel ignores the setuid bit of the emulated binaries.
"""

from __future__ import annotations

from pathlib import Path

from xcraft import process

_QEMU = {"x86_64_v2": "x86_64", "x86_64": "x86_64", "aarch64": "aarch64"}
"""The qemu name of each RPM architecture."""
_BINFMT = Path("/proc/sys/fs/binfmt_misc")
_BINFMT_D = Path("/usr/lib/binfmt.d")


def binfmt(arch: str) -> tuple[str, str] | None:
    """Return the name and the flags of the enabled binfmt_misc registration of the emulator of `arch`, if any."""
    qemu = f"qemu-{_QEMU[arch]}"
    for entry in _BINFMT.glob("*"):
        try:
            lines = [line.split(maxsplit=1) for line in entry.read_text().splitlines()]
        except OSError:  # register and status
            continue
        fields = {line[0]: line[1] if len(line) > 1 else "" for line in lines if line}
        if "enabled" in fields and fields.get("interpreter", "").endswith((qemu, f"{qemu}-static")):
            return entry.name, fields.get("flags:", "")
    return None


def emulation_problem(arch: str) -> str:
    """Return why the host cannot emulate the RPM architecture `arch` for xcraft, or nothing when it can."""
    if not (entry := binfmt(arch)):
        return f"no emulation of {arch}, to build for it: install qemu-user-static"
    if "C" not in entry[1]:
        return f"the emulation of {arch} cannot run sudo, {entry[0]} having no C flag: run xcraft doctor --fix"
    return ""


def emulation_fix(arch: str) -> tuple[Path, str, str]:
    """Return how to register the emulation of `arch` with the C flag, over the distribution's registration.

    That is the file of the distribution, its flags, and its content with the flags FOC, for /etc/binfmt.d/: F, the
    emulator loaded once, and O and C, the binary opened with its credentials.
    """
    if not (entry := binfmt(arch)):
        msg = f"no emulation of {arch} to fix: install qemu-user-static"
        raise RuntimeError(msg)
    prefix = f":{entry[0]}:"
    for conf in sorted(_BINFMT_D.glob("*.conf")):
        lines = conf.read_text().splitlines()
        if any(line.startswith(prefix) for line in lines):
            # the flags are the last field
            text = "".join(
                f"{line.rsplit(':', 1)[0]}:FOC\n" if line.startswith(prefix) else f"{line}\n" for line in lines
            )
            return conf, entry[1], text
    msg = f"{entry[0]} is not registered by {_BINFMT_D}: add the C flag to its registration"
    raise RuntimeError(msg)


def fix_commands(fixes: list[tuple[Path, str]]) -> list[list[str]]:
    """Return the commands registering the `fixes`, files of the distribution and their new content, with sudo."""
    return [
        *(["sudo", "tee", f"/etc/binfmt.d/{conf.name}"] for conf, _ in fixes),
        ["sudo", "systemctl", "restart", "systemd-binfmt"],
    ]


def fix_emulation(fixes: list[tuple[Path, str]]) -> None:
    """Register the `fixes`, files of the distribution and their new content, in /etc/binfmt.d/, with sudo."""
    *writes, restart = fix_commands(fixes)
    for cmd, (_, text) in zip(writes, fixes, strict=True):
        process.run(cmd, input=text, capture=True)
    process.run(restart)
