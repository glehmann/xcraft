"""Write as JSON the binary packages built, with the provides and requirements of each.

Run in the build environment, with the rpm python API: ``made.py <output> <RPMS directory>``, for xcraft to check
them against those it parsed from the spec.
"""

import json
import sys
from pathlib import Path

import rpm  # ty: ignore[unresolved-import]
from rpmdeps import deps  # next to it, in /xcraft


def main() -> None:
    """Describe the packages of the RPMS directory of the command line."""
    output, rpms = sys.argv[1:]
    ts = rpm.TransactionSet()
    ts.setVSFlags(-1)  # no checks: they were just built
    made = {}
    for path in Path(rpms).rglob("*.rpm"):
        with path.open("rb") as f:
            h = ts.hdrFromFdno(f.fileno())
        made[h[rpm.RPMTAG_NAME]] = {
            "evr": h[rpm.RPMTAG_EVR],
            "arch": h[rpm.RPMTAG_ARCH],
            "provides": deps(h, rpm.RPMTAG_PROVIDENAME),
            "requires": deps(h, rpm.RPMTAG_REQUIRENAME),
        }
    Path(output).write_text(json.dumps(made))


if __name__ == "__main__":
    main()
