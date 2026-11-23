"""Write as JSON the differences between two builds of a package, an empty list when they are the same.

Run in the build environment, with the rpm python API: ``same.py <output> <dir> <suffix> <dir> <suffix>``, each
directory with the RPMs and the source RPM of one build, of the release suffix, %autorev, given. The builds are the
same with:

- the same packages, by name and architecture;
- the same files: path, SHA-256, mode, owner, group, flags, link target, device and capabilities, not their times;
- the same dependencies, with the release suffix of the build replaced in those on the packages of the build only:
  a requirement on another build of a dependency is a difference. Not those rpm adds for the ~ or ^ of a release;
- the same scriptlets and triggers, with the release suffix of the build replaced.

The rest is ignored: the release, the build time and host, and the signatures and digests of the headers and payloads.
"""

import json
import re
import sys
from pathlib import Path

import rpm  # ty: ignore[unresolved-import]  # pyright: ignore[reportMissingImports]

FILES = ["FILENAMES", "FILEDIGESTS", "FILEMODES", "FILEUSERNAME", "FILEGROUPNAME", "FILEFLAGS", "FILELINKTOS"]
FILES += ["FILERDEVS", "FILECAPS"]
DEPS = ["REQUIRE", "PROVIDE", "CONFLICT", "OBSOLETE", "RECOMMEND", "SUGGEST", "SUPPLEMENT", "ENHANCE", "ORDER"]
SCRIPTS = ["PREIN", "POSTIN", "PREUN", "POSTUN", "PRETRANS", "POSTTRANS", "PREUNTRANS", "POSTUNTRANS", "VERIFYSCRIPT"]
SCRIPTS += [f"{s}PROG" for s in SCRIPTS]
TRIGGERS = ["TRIGGERSCRIPTS", "TRIGGERSCRIPTPROG", "TRIGGERNAME", "TRIGGERVERSION", "TRIGGERFLAGS", "TRIGGERINDEX"]
TRIGGERS += ["FILETRIGGERSCRIPTS", "FILETRIGGERNAME", "TRANSFILETRIGGERSCRIPTS", "TRANSFILETRIGGERNAME"]

RELEASED = {"rpmlib(TildeInVersions)", "rpmlib(CaretInVersions)"}
"""The requirements rpm adds for a ~ or a ^ in the release, like the suffixes of the development builds."""

ts = rpm.TransactionSet()
ts.setVSFlags(-1)  # no checks: the builds were checked already


def _tag(h: rpm.hdr, name: str) -> list:
    t = getattr(rpm, f"RPMTAG_{name}", None)
    value = None if t is None else h[t]
    if value is None or value in (b"", ""):
        return []
    return [
        v.decode(errors="replace") if isinstance(v, bytes) else v
        for v in (value if isinstance(value, list) else [value])
    ]


def _headers(directory: Path) -> dict[str, rpm.hdr]:
    """Return the headers of the packages of `directory`, by name and architecture, the source one as <name>.src."""
    found = {}
    for path in sorted(directory.rglob("*.rpm")):
        with path.open("rb") as f:
            h = ts.hdrFromFdno(f.fileno())
        found[f"{h[rpm.RPMTAG_NAME]}.{'src' if h[rpm.RPMTAG_SOURCEPACKAGE] else h[rpm.RPMTAG_ARCH]}"] = h
    return found


def _replaced(suffix: str, values: list) -> list:
    """Return `values` with the release `suffix` replaced, where it is not followed by more of a release."""
    pattern = re.compile(re.escape(suffix) + r"(?![0-9~^])")
    return [pattern.sub("%{autorev}", v) if isinstance(v, str) else v for v in values]


def _deps(h: rpm.hdr, suffix: str, own: set[str], kind: str) -> list[tuple]:
    """Return the dependencies `kind` of `h`, with the release `suffix` replaced in those on the packages `own`, but
    those rpm adds for the characters of a release, like the ~ of a development build, see RELEASED.
    """
    names, flags, versions = (_tag(h, f"{kind}{t}") for t in ("NAME", "FLAGS", "VERSION"))
    return [
        (n, f, _replaced(suffix, [v])[0] if re.sub(r"\(.*\)$", "", n) in own else v)
        for n, f, v in zip(names, flags, versions, strict=True)
        if n not in RELEASED
    ]


def _files(h: rpm.hdr) -> dict[str, tuple]:
    """Return the attributes of the files of `h`, see FILES, by path."""
    paths = _tag(h, "FILENAMES")
    return dict(zip(paths, zip(*(_tag(h, n) or [None] * len(paths) for n in FILES), strict=True), strict=True))


def _differences(a: rpm.hdr, b: rpm.hdr, suffixes: tuple[str, str], own: set[str]) -> list[str]:
    files = _files(a), _files(b)
    differences = [
        f"{path}: only in one"
        if fa is None or fb is None
        else f"{path}: {', '.join(n for n, x, y in zip(FILES, fa, fb, strict=True) if x != y)}"
        for path in sorted(files[0].keys() | files[1].keys())
        if (fa := files[0].get(path)) != (fb := files[1].get(path))
    ]
    for kind in DEPS:
        deps = set(_deps(a, suffixes[0], own, kind)), set(_deps(b, suffixes[1], own, kind))
        differences += [f"{kind.lower()}: {' '.join(map(str, d))} only in one" for d in sorted(deps[0] ^ deps[1])]
    differences += [
        n.lower()
        for n in SCRIPTS + TRIGGERS
        if _replaced(suffixes[0], _tag(a, n)) != _replaced(suffixes[1], _tag(b, n))
    ]
    return differences


def main() -> None:
    """Compare the builds of the command line."""
    output, a, suffix_a, b, suffix_b = sys.argv[1:]
    headers = _headers(Path(a)), _headers(Path(b))
    own = {key.rsplit(".", 1)[0] for key in headers[0]}
    differences = [f"{key}: not in both" for key in sorted(headers[0].keys() ^ headers[1].keys())]
    differences += [
        f"{key}: {d}"
        for key in sorted(headers[0].keys() & headers[1].keys())
        for d in _differences(headers[0][key], headers[1][key], (suffix_a, suffix_b), own)
    ]
    Path(output).write_text(json.dumps(differences))


if __name__ == "__main__":
    main()
