"""Write as JSON what xcraft needs of the specs of packages, parsed for a target architecture.

Run in the build environment, with the rpm python API: ``parse.py <output> <target> <spec>...``, the target like
``aarch64-linux``, like rpmbuild --target, the packages named after the directories of their specs.
"""

import json
import pathlib
import subprocess
import sys

import rpm  # ty: ignore[unresolved-import]
from rpmdeps import deps  # next to it, in /xcraft

MARKER = "XCRAFTAUTOREV"
"""What %autorev, the release suffix of the builds, is when parsing the specs to find the names it is in."""


def _load(spec_file: str, target: str, autorev: str | None = None) -> rpm.spec:
    """Return the spec parsed for `target`, from the configuration of rpm, with %autorev as `autorev`, if given."""
    pkg_dir = pathlib.Path(spec_file).parent
    rpm.reloadConfig(target)
    rpm.addMacro("_sourcedir", str(pkg_dir))
    rpm.addMacro("_specdir", str(pkg_dir))
    if autorev is not None:
        rpm.addMacro("autorev", autorev)
    return rpm.spec(spec_file)


def _autorev_names(spec_file: str, target: str) -> list[str]:
    """Return the names of the binary packages of a spec, with %{autorev} where the release suffix of the builds goes,
    for those named after their release, like the livepatch packages of the kernel, and nothing for the others.
    """
    spec = _load(spec_file, target, MARKER)
    names = [p.header[rpm.RPMTAG_NAME] for p in spec.packages if p.fileList is not None]
    return [name.replace(MARKER, "%{autorev}") if MARKER in name else "" for name in names]


def parse(spec_file: str, target: str) -> dict:
    """Return the binary packages, BuildRequires, %dist and sources of a spec, or why it cannot be parsed."""
    pkg_dir = pathlib.Path(spec_file).parent
    try:
        spec = _load(spec_file, target)
    except ValueError:
        # parse again with rpmspec, to get the error message
        cmd = ["rpmspec", "-D", f"_sourcedir {pkg_dir}", "-q", spec_file]
        return {
            "error": subprocess.run(cmd, capture_output=True, text=True, check=False).stderr.strip()
            or "cannot parse spec"
        }
    parsed = {
        "version": spec.sourceHeader[rpm.RPMTAG_VERSION],
        "binaries": [
            {
                "name": h[rpm.RPMTAG_NAME],
                "evr": h[rpm.RPMTAG_EVR],
                "arch": h[rpm.RPMTAG_ARCH],
                "provides": deps(h, rpm.RPMTAG_PROVIDENAME),
                "requires": deps(h, rpm.RPMTAG_REQUIRENAME),
                "recommends": deps(h, rpm.RPMTAG_RECOMMENDNAME),
            }
            # those without %files are not built
            for h in (p.header for p in spec.packages if p.fileList is not None)
        ],
        "buildrequires": deps(spec.sourceHeader, rpm.RPMTAG_REQUIRENAME),
        "dist": rpm.expandMacro("%{?dist}"),
        # sources are of type 1, patches 2
        "source0": next((source for source, n, kind in spec.sources if kind == 1 and n == 0), ""),
        "sources": [source for source, n, kind in sorted(spec.sources, key=lambda s: s[1]) if kind == 1],
    }
    # parsed again, from the start
    for binary, autorev_name in zip(parsed["binaries"], _autorev_names(spec_file, target), strict=True):
        binary["autorev_name"] = autorev_name
    return parsed


def main() -> None:
    """Parse the specs of the command line."""
    output, target, *specs = sys.argv[1:]
    pathlib.Path(output).write_text(json.dumps({pathlib.Path(s).parent.name: parse(s, target) for s in specs}))


if __name__ == "__main__":
    main()
