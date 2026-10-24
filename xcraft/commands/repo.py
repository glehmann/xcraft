"""xcraft repo."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import typer

from xcraft import build as builder
from xcraft import process
from xcraft.commands import Arch, Packages, Pre, Test, load, test_kind
from xcraft.display import activity, console
from xcraft.signing import check_signatures, dev_signer
from xcraft.specs import is_debug

REMOTE = "/var/local/xcraft"
"""Where the repositories of test builds go on the hosts, one per ID."""
KEY_FILE = "RPM-GPG-KEY-xcraft-test"
"""The developer's key, in the repositories of test builds."""


def _hosts(hosts: list[str] | None) -> list[str] | None:
    """Refuse the hosts ssh and rsync would take for options."""
    if bad := [host for host in hosts or [] if host.startswith("-")]:
        msg = f"not a host: {', '.join(bad)}"
        raise typer.BadParameter(msg)
    return hosts


def _copy_to_host(host: str, output: Path, ident: str, binaries: list[str], *, upgrade: bool) -> None:
    """Copy the repository of test builds in `output` to `host`, with its .repo file, and maybe upgrade `binaries`."""
    remote = f"{REMOTE}/{ident}"
    with activity(f"copying the repository to {host}"):
        process.run(["ssh", host, f"mkdir -p {remote}"], capture=True)
        process.run(["rsync", "-a", "--delete", f"{output}/", f"{host}:{remote}/"], capture=True)
        process.run(["ssh", host, f"install -m 644 {remote}/xcraft-{ident}.repo /etc/yum.repos.d/"], capture=True)
    console.print(f"[bold green]✓[/] repository on {host}, in {remote}")
    if upgrade:
        # only those installed: dnf upgrade fails on the others
        script = (
            f"names=$(rpm -q --qf '%{{NAME}}\\n' {shlex.join(binaries)} | grep -v ' ' || true)\n"
            'if [ -n "$names" ]; then dnf -y upgrade --refresh $names; else echo "none of them is installed"; fi\n'
        )
        process.run(["ssh", host, script])


def repo(  # noqa: PLR0913, PLR0917
    packages: Packages = None,
    output: Annotated[
        Path | None,
        typer.Option(help="Directory of the repository. repo/ in the project by default, repo-<ID>/ for test builds."),
    ] = None,
    test: Test = None,
    pre: Pre = None,
    baseurl: Annotated[
        str | None,
        typer.Option(
            help="Where hosts find the repository of test builds, for its .repo file. Its directory by default."
        ),
    ] = None,
    hosts: Annotated[
        list[str] | None,
        typer.Option(
            "--host",
            help=f"Test host, as for ssh, to copy the repository of test builds to, in {REMOTE}/<ID>, with its "
            ".repo file. Can be repeated.",
            show_default=False,
            callback=_hosts,
        ),
    ] = None,
    upgrade: Annotated[
        bool, typer.Option("--upgrade", help="Also upgrade, on the hosts, the test packages they have installed.")
    ] = False,
    arch: Arch = None,
) -> None:
    """Build the given packages, all by default, and what they need, put them in a repository, and check them.

    With --test or --pre, the repository only has new test builds of the given packages, to install on test hosts
    over their packages, with a .repo file.
    """
    kind = test_kind(test, pre, packages)
    if (hosts or upgrade) and not kind:
        msg = "--host and --upgrade are for test builds, with --test or --pre"
        raise typer.BadParameter(msg)
    if upgrade and not hosts:
        msg = "--upgrade needs --host"
        raise typer.BadParameter(msg)
    if hosts and baseurl:
        msg = f"with --host, the hosts find the repository in {REMOTE}/<ID>: no --baseurl"
        raise typer.BadParameter(msg)
    project = load(packages, arch=arch)
    names = list(packages or sorted(project.packages))
    with project.lock():
        autorev = builder.next_test_suffixes(project, names, *kind) if kind else {}
        results, keys = builder.build(project, names, autorev=autorev)
        if failed := [r.name for r in results if r.failed]:
            msg = f"not built: {', '.join(failed)}"
            raise RuntimeError(msg)
        output = (output or project.root / (f"repo-{kind[1]}" if kind else "repo")).absolute()
        published = names if kind else [r.name for r in results]
        builder.make_repo(project, published, output, keys, autorev=autorev)
    console.print(f"[bold green]✓[/] repository in {output}, with all dependencies satisfied")
    if kind:
        url = baseurl or (f"file://{REMOTE}/{kind[1]}" if hosts else f"file://{output}")
        repo_file = output / f"xcraft-{kind[1]}.repo"
        if signer := dev_signer(project):
            key = signer.public_key()
            rpms = sorted(f.relative_to(output).as_posix() for f in output.glob("*.rpm"))
            check_signatures(project, output, rpms, what="test packages", public_key=key)
            signer.sign_file(output / "repodata/repomd.xml")
            (output / KEY_FILE).write_text(key)
            checks = f"gpgcheck=1\nrepo_gpgcheck=1\ngpgkey={url.rstrip('/')}/{KEY_FILE}\n"
        else:
            console.print("[yellow]the test packages are not signed[/]: see xcraft key create")
            checks = "gpgcheck=0\n"
        repo_file.write_text(
            f"[xcraft-{kind[1]}]\nname=xcraft {kind[0]} builds {kind[1]}\nbaseurl={url}\n"
            f"enabled=1\n{checks}metadata_expire=0\n"
        )
        binaries = [b.name for name in names for b in project.packages[name].binaries if not is_debug(b.name)]
        for host in hosts or []:
            _copy_to_host(host, output, kind[1], binaries, upgrade=upgrade)
        if not hosts:
            console.print(f"on the hosts, with this directory at {url}:", markup=False)
            console.print(
                f"  copy {repo_file.name} to /etc/yum.repos.d/, then: dnf upgrade --refresh {' '.join(binaries)}",
                markup=False,
            )
