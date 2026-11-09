"""xcraft login and logout."""

from __future__ import annotations

from typing import Annotated

import typer

from xcraft import remote
from xcraft.commands import Destination, remote_storage
from xcraft.display import console
from xcraft.project import Project

Storage = Annotated[Destination, typer.Argument(help="Remote storage of the S3 server.")]


def login(storage: Storage = "dev") -> None:
    """Store the S3 credentials of the server of a remote storage in the desktop keyring, for all its buckets, once
    checked on that one's: those of the environment, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, take precedence.
    """
    selected = remote_storage(Project.find(), storage)
    remote.login(
        selected, remote.Credentials(typer.prompt("Access key ID").strip(), typer.prompt("Secret key", hide_input=True))
    )
    console.print(f"[bold green]✓[/] credentials of {selected.s3_endpoint} in the keyring")


def logout(storage: Storage = "dev") -> None:
    """Remove the S3 credentials of the server of a remote storage from the desktop keyring."""
    endpoint = remote_storage(Project.find(), storage).s3_endpoint
    console.print(
        f"credentials of {endpoint} removed" if remote.logout(endpoint) else f"no credentials of {endpoint} to remove"
    )
