"""The hosts reached over SSH: the test hosts of xcraft repo --host, and the builders, see xcraft.builders."""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import typer

from xcraft import process

if TYPE_CHECKING:
    from collections.abc import Generator


@dataclass
class Host:
    """A host: its `name`, as given, and how ssh reaches it, with the `options` of ssh, like its port."""

    name: str
    destination: str
    options: list[str] = field(default_factory=list)

    def ssh(self, command: str) -> list[str]:
        """Return the command running `command` on the host, through ssh."""
        return ["ssh", *self.options, self.destination, command]


def ssh_host(name: str) -> Host:
    """Return the host `name`, as ssh takes it, or as ssh://[user@]host[:port], whose port rsync cannot take."""
    if name.startswith("-"):  # which ssh and rsync would take for options
        msg = f"not a host: {name}"
        raise typer.BadParameter(msg)
    if not name.startswith("ssh://"):
        return Host(name, name)
    url = urlsplit(name)
    try:
        port = url.port
    except ValueError:
        port = -1
    user = f"{url.username}@" if url.username else ""
    # nor an option once parsed, like ssh://-oProxyCommand=...
    if not url.hostname or url.path or port == -1 or f"{user}{url.hostname}".startswith("-"):
        msg = f"not a host: {name}, as ssh://[user@]host[:port]"
        raise typer.BadParameter(msg)
    return Host(name, f"{user}{url.hostname}", ["-p", str(port)] if port else [])


@contextmanager
def connected(host: Host) -> Generator[Host]:
    """Connect to `host` once, asking for its password, if any, before the activity of the copy, and yield it with
    the options of ssh sharing that connection, which its commands and rsync reuse, closed then.
    """
    # a short path: the socket of the connection is longer still, and Unix sockets are limited to about 100 bytes
    with tempfile.TemporaryDirectory(prefix="xcraft-ssh-") as tmp:
        shared = replace(host, options=[*host.options, "-o", f"ControlPath={tmp}/%C"])
        master = ["-o", "ControlMaster=yes", "-o", "ControlPersist=yes", "-f", "-N", host.destination]
        process.run(["ssh", *shared.options, *master])
        try:
            yield shared
        finally:
            process.run(["ssh", *shared.options, "-O", "exit", host.destination], check=False, capture=True)
