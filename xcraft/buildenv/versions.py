"""Write as JSON the available versions of the given upstream packages, oldest first, for each of them.

Run in the build environment, with the dnf python API, which does not apply the versionlock: ``versions.py <output>
<package>...``. Our packages, built by xcraft, are left out.
"""

import functools
import json
import sys
from pathlib import Path

import dnf  # ty: ignore[unresolved-import]

base = dnf.Base()
base.conf.read()
base.read_all_repos()
base.fill_sack(load_system_repo=False)
query = base.sack.query().available().filter(name=sys.argv[2:])
result, seen = {}, set()
for p in sorted(query, key=functools.cmp_to_key(lambda a, b: a.evr_cmp(b))):
    if not p.reponame.startswith("xcraft-") and (p.name, p.evr, p.arch) not in seen:
        seen.add((p.name, p.evr, p.arch))
        result.setdefault(p.name, []).append(
            {"evr": p.evr, "arch": p.arch, "source": p.source_name, "repo": p.reponame, "sha256": p.chksum[1].hex()}
        )
Path(sys.argv[1]).write_text(json.dumps(result))
