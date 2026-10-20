"""xcraft key."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from xcraft.display import console
from xcraft.importer import git
from xcraft.project import Project
from xcraft.signing import create_dev_key, dev_keyring, dev_signer

app = typer.Typer(help="The developer's key, which signs the test builds.", no_args_is_help=True)


@app.command()
def create(
    name: Annotated[
        str | None,
        typer.Option(help="User id of the key. The git user's, marked as for test builds, by default."),
    ] = None,
) -> None:
    """Make the developer's key, without passphrase, in its own keyring: it signs the test builds of this checkout."""
    project = Project.find()
    if not name:
        user, email = (git("-C", str(project.root), "config", key) for key in ("user.name", "user.email"))
        name = f"{user} (xcraft test builds) <{email}>"
    signer = create_dev_key(project, name)
    console.print(f"[bold green]✓[/] key {signer.key} in {dev_keyring(project)}, for: {escape(name)}")
    console.print("test builds are now signed with it, and test hosts get it with the repositories of test builds")


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
