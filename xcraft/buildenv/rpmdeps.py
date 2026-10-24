"""The dependencies of rpm headers, as xcraft compares them: the parsed specs, the built packages."""

import rpm  # ty: ignore[unresolved-import]


def deps(header: rpm.hdr, tag: int) -> list[str]:
    """Return the dependencies of `tag` of `header`, like ``foo >= 1:2-3``, or rich ones, like ``(a if b)``."""
    return [d.DNEVR()[2:] for d in rpm.ds(header, tag) if not d.N().startswith("rpmlib(")]
