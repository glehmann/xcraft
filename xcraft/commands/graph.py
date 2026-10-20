"""xcraft graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import typer
from rich.tree import Tree

from xcraft.commands import complete_package, load
from xcraft.display import console
from xcraft.graph import build_order, reachable, upstream_deps
from xcraft.lock import load_lock
from xcraft.upstream import upstream_requires

if TYPE_CHECKING:
    from collections.abc import Iterable


def _tree(root: str, edges: dict[str, set[str]], ours: Iterable[str]) -> Tree:
    """Return the tree of `edges` from `root`, each package expanded once: those of `ours` first, the others in cyan."""

    def label(name: str) -> str:
        return name if name in ours else f"[cyan]{name}[/]"

    tree = Tree(f"[bold]{label(root)}[/]", guide_style="dim")
    shown = {root}

    def add(node: Tree, name: str) -> None:
        for child in sorted(edges.get(name, ()), key=lambda child: (child not in ours, child)):
            if child in shown:
                node.add(f"{label(child)} [dim](see above)[/]")
            else:
                shown.add(child)
                add(node.add(label(child)), child)

    add(tree, root)
    return tree


def graph(
    package: Annotated[
        str,
        typer.Argument(help="Package, ours or upstream.", show_default=False, autocompletion=complete_package),
    ],
    with_dependents: Annotated[
        bool, typer.Option("--dependents", help="Show the packages built against it instead.")
    ] = False,
    upstream: Annotated[
        bool,
        typer.Option(
            "--upstream",
            "-u",
            help="Also show the upstream packages our builds need, in cyan, after ours. Implied by an upstream one.",
        ),
    ] = False,
    flat: Annotated[
        bool, typer.Option("--flat", help="Only list the packages: ours in build order, then the upstream ones.")
    ] = False,
) -> None:
    """Show the packages a package is built against, as a tree."""
    project = load()
    packages, deps = project.packages, project.deps
    upstream = upstream or package not in packages
    edges = {name: set(names) for name, names in deps.items()}
    if upstream:
        if package not in packages and package not in load_lock(project).packages:
            msg = f"unknown package: {package}"
            raise typer.BadParameter(msg)
        for name, names in upstream_deps(packages, upstream_requires(project)).items():
            edges.setdefault(name, set()).update(names)
    if with_dependents:
        # only through the upstream packages our builds need
        needed = reachable(edges, packages)
        edges = {name: {other for other in needed if name in edges.get(other, ())} for name in needed}
    names = reachable(edges, [package])
    if flat:
        if with_dependents:
            names.remove(package)
        ours = names & packages.keys()
        for name in [*build_order(deps, ours, with_deps=False), *sorted(names - ours)]:
            console.print(name)
        return

    console.print(_tree(package, edges, ours=packages.keys()))
