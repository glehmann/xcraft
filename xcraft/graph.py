"""Build dependencies between the packages of a project, and with the upstream packages."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from xcraft.specs import Binary, Package
    from xcraft.upstream import UpstreamRequires


class GraphError(RuntimeError):
    """Raised when packages cannot be ordered for building."""


def build_root_binaries(packages: dict[str, Package]) -> dict[str, list[Binary]]:
    """Return, for each package, the binaries of the project its build installs.

    Those provide its BuildRequires, or are required at runtime by those
    binaries, since dnf installs them too in the build root.
    """
    providers: dict[str, list[Binary]] = {}
    for package in packages.values():
        for binary in package.binaries:
            for capability in [binary.name, *binary.provides]:
                providers.setdefault(capability, []).append(binary)

    def binaries_providing(capabilities: Iterable[str]) -> list[Binary]:
        return [provider for capability in capabilities for provider in providers.get(capability, ())]

    result = {}
    for package in packages.values():
        todo = binaries_providing(package.buildrequires)
        seen: dict[str, Binary] = {}
        while todo:
            binary = todo.pop()
            if binary.name not in seen:
                seen[binary.name] = binary
                todo.extend(binaries_providing(binary.requires))
        result[package.name] = list(seen.values())
    return result


def build_deps(packages: dict[str, Package], image: Iterable[str] = ()) -> dict[str, set[str]]:
    """Return, for each package, the packages that must be built before it: those of the binaries its build installs.

    Those include our binaries replacing the upstream ones of the build
    environment `image`, like kernel-headers, in all builds but those of the
    packages they need. Dependencies on anything else are left to the build
    environment's repositories.
    """
    owners = {binary.name: package.name for package in packages.values() for binary in package.binaries}
    deps = {
        name: {owners[binary.name] for binary in binaries} - {name}
        for name, binaries in build_root_binaries(packages).items()
    }
    for owner in sorted({owners[name] for name in image if name in owners}):
        for name in deps.keys() - reachable(deps, [owner]):
            deps[name].add(owner)
    return deps


def upstream_deps(packages: dict[str, Package], upstream: UpstreamRequires) -> dict[str, set[str]]:
    """Return the upstream binary packages each of our packages and each upstream package needs.

    Ours need those providing their BuildRequires, or required by our binaries
    their builds install. Upstream ones need those providing their Requires.
    """
    deps = {name: set(requires) for name, requires in upstream.requires.items()}
    for name, binaries in build_root_binaries(packages).items():
        capabilities = [*packages[name].buildrequires, *(r for binary in binaries for r in binary.requires)]
        deps[name] = {provider for c in capabilities for provider in upstream.providers.get(c, ())}
    return deps


def reachable(deps: dict[str, set[str]], roots: Iterable[str]) -> set[str]:
    """Return `roots` and what they depend on, directly or not."""
    found = set(roots)
    todo = list(found)
    while todo:
        for dep in deps.get(todo.pop(), set()) - found:
            found.add(dep)
            todo.append(dep)
    return found


def dependents(deps: dict[str, set[str]], names: Iterable[str]) -> set[str]:
    """Return the packages built against any of `names`, directly or not."""
    found: set[str] = set()
    todo = set(names)
    while todo:
        new = {name for name, name_deps in deps.items() if name_deps & todo} - found
        found |= new
        todo = new
    return found


def build_order(deps: dict[str, set[str]], targets: Iterable[str], *, with_deps: bool = True) -> list[str]:
    """Return `targets`, with all their dependencies unless `with_deps` is false, in build order."""
    plan = reachable(deps, targets) if with_deps else set(targets)
    try:
        # sorted, for a stable order between packages that do not depend on each other
        return list(TopologicalSorter({name: sorted(deps[name] & plan) for name in sorted(plan)}).static_order())
    except CycleError as e:
        msg = f"dependency cycle: {' -> '.join(e.args[1])}"
        raise GraphError(msg) from e


def components(deps: dict[str, set[str]]) -> list[set[str]]:
    """Return the sets of packages depending on each other, a package alone otherwise, their dependencies first."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    result: list[set[str]] = []

    def visit(name: str) -> None:  # Tarjan's algorithm
        index[name] = low[name] = len(index)
        stack.append(name)
        for dep in sorted(deps[name]):
            if dep not in index:
                visit(dep)
                low[name] = min(low[name], low[dep])
            elif dep in stack:
                low[name] = min(low[name], index[dep])
        if low[name] == index[name]:
            component, member = set(), ""
            while member != name:
                member = stack.pop()
                component.add(member)
            result.append(component)

    for name in sorted(deps):
        if name not in index:
            visit(name)
    return result
