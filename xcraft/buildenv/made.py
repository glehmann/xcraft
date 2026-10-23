"""Write as JSON the binary packages built, with the provides and requirements of each.

Run in the build environment, with the rpm python API: ``made.py <output> <RPMS directory>``, for xcraft to check
them against those it parsed from the spec.
"""

import json
import sys
from pathlib import Path

import rpm  # ty: ignore[unresolved-import]

ts = rpm.TransactionSet()
ts.setVSFlags(-1)  # no checks: they were just built
made = {}
for path in Path(sys.argv[2]).rglob("*.rpm"):
    with path.open("rb") as f:
        h = ts.hdrFromFdno(f.fileno())
    made[h[rpm.RPMTAG_NAME]] = {
        "evr": h[rpm.RPMTAG_EVR],
        "arch": h[rpm.RPMTAG_ARCH],
        "provides": [d.DNEVR()[2:] for d in rpm.ds(h, rpm.RPMTAG_PROVIDENAME)],
        "requires": [d.DNEVR()[2:] for d in rpm.ds(h, rpm.RPMTAG_REQUIRENAME) if not d.N().startswith("rpmlib(")],
    }
Path(sys.argv[1]).write_text(json.dumps(made))
