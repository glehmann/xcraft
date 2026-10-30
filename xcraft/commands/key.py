"""xcraft key."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from xcraft.display import console
from xcraft.process import git
from xcraft.project import Project
from xcraft.signing import create_dev_key, dev_keyring, dev_signer

app = typer.Typer(help="The developer's key, which signs the development builds.", no_args_is_help=True)


@app.command()
def create(
    name: Annotated[
        str | None,
        typer.Option(help="User id of the key. The git user's, marked as for development builds, by default."),
    ] = None,
) -> None:
    """Make the developer's key, without passphrase, in its own keyring: it signs the development builds of the
    checkout.
    """
    project = Project.find()
    if not name:
        user, email = (git("-C", str(project.root), "config", key) for key in ("user.name", "user.email"))
        name = f"{user} (xcraft development builds) <{email}>"
    signer = create_dev_key(project, name)
    console.print(f"[bold green]✓[/] key {signer.key} in {dev_keyring(project)}, for: {escape(name)}")
    console.print(
        "development builds are now signed with it, and test hosts get it with the repositories of development builds"
    )


@app.command()
def show(
    export: Annotated[bool, typer.Option("--export", help="Print the public key.")] = False,
) -> None:
    """Show the developer's key."""
    project = Project.find()
    if not (signer := dev_signer(project)):
        console.print("no key: see xcraft key create")
        raise typer.Exit(1)
    if export:
        console.print(signer.public_key(), end="", markup=False)
        return
    console.print(signer.key)
    console.print(signer.gpg("--list-keys", signer.key), end="", markup=False)
