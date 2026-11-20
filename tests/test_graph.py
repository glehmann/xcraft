from __future__ import annotations

import pytest

from xcraft.graph import GraphError, build_deps, build_order, components, dependents, reachable, upstream_deps
from xcraft.packages import Binary, Package
from xcraft.upstream import UpstreamRequires


def package(name: str, buildrequires: list[str] = [], **binaries: list[str]) -> Package:  # noqa: B006
    """Return a package whose binaries, given as name=requires, provide their name."""
    return Package(
        name=name,
        buildrequires=buildrequires,
        binaries=[
            Binary(name=binary.replace("_", "-"), evr="1-1", arch="x86_64", provides=[], requires=requires)
            for binary, requires in binaries.items()
        ],
    )


def packages(*pkgs: Package) -> dict[str, Package]:
    return {p.name: p for p in pkgs}


def should_follow_runtime_requires_of_required_binaries_only() -> None:
    # like qemu and xen: xen-libs-devel is needed, not xen-dom0-tools which requires qemu
    pkgs = packages(
        package("xen", xen_libs=[], xen_libs_devel=["xen-libs", "libc.so.6"], xen_dom0_tools=["qemu"]),
        package("qemu", ["xen-libs-devel", "gcc"], qemu=["xen-libs"]),
    )
    assert build_deps(pkgs) == {"xen": set(), "qemu": {"xen"}}


def should_include_runtime_requires_from_other_packages() -> None:
    pkgs = packages(
        package("libfoo", libfoo=[]),
        package("bar", bar_devel=["libfoo"]),
        package("app", ["bar-devel"], app=[]),
    )
    assert build_deps(pkgs)["app"] == {"bar", "libfoo"}


def should_include_ours_replacing_the_image_ones() -> None:
    pkgs = packages(
        package("kernel", ["bc"], kernel_headers=[]),
        package("bc", bc=[]),
        package("app", app=[]),
    )
    # bc is built with the image's kernel-headers, since kernel needs it
    assert build_deps(pkgs, ["kernel-headers", "gcc"]) == {"kernel": {"bc"}, "bc": set(), "app": {"kernel"}}


def should_order_the_builds_after_their_dependencies() -> None:
    deps = {"a": set(), "b": {"a"}, "c": {"b"}, "d": set()}
    assert build_order(deps, ["c"]) == ["a", "b", "c"]
    assert build_order(deps, ["c"], with_deps=False) == ["c"]
    assert dependents(deps, ["a"]) == {"b", "c"}


def should_report_dependency_cycles() -> None:
    with pytest.raises(GraphError, match="dependency cycle: "):
        build_order({"a": {"b"}, "b": {"a"}}, ["a"])


def should_find_the_upstream_dependencies_of_build_roots() -> None:
    pkgs = packages(
        package("lib", ["gcc"], lib=["glibc"], lib_tools=["python3"]),
        package("app", ["lib", "python3-devel"], app=[]),
    )
    upstream = UpstreamRequires(
        requires={"gcc": ["glibc"], "glibc": [], "python3": ["glibc"], "python3-devel": ["python3"]},
        providers={"gcc": ["gcc"], "glibc": ["glibc"], "python3": ["python3"], "python3-devel": ["python3-devel"]},
    )
    deps = upstream_deps(pkgs, upstream)
    # app installs lib, which requires glibc, not lib-tools
    assert (deps["lib"], deps["app"]) == ({"gcc"}, {"glibc", "python3-devel"})
    assert reachable(deps, ["app"]) == {"app", "glibc", "python3-devel", "python3"}


def should_group_the_packages_by_cycles_in_build_order() -> None:
    deps = {"app": {"a"}, "a": {"b"}, "b": {"a", "lib"}, "lib": set(), "alone": set()}
    # each after its dependencies
    assert components(deps) == [{"lib"}, {"a", "b"}, {"alone"}, {"app"}]
