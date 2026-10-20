"""Write as JSON the installed packages, or the latest versions of the given ones.

The installed packages built by xcraft have the repository ``xcraft``, whose
id in the containers is ``xcraft-<project id>``. Run as
root in the build environment, with the dnf python API, which does not apply
the versionlock: ``record.py <output> [<package>...]``.
"""

import json
import sys
from pathlib import Path

import dnf  # ty: ignore[unresolved-import]

base = dnf.Base()
base.conf.read()
base.read_all_repos()
base.fill_sack(load_system_repo=True)
available = base.sack.query().available()
result, missing = {}, []
if sys.argv[2:]:
    for p in available.filter(name=sys.argv[2:], latest_per_arch=True):
        result[p.name] = p
else:
    for p in base.sack.query().installed().filter(name__neq="gpg-pubkey"):
        # built here, rather than from the same version upstream
        found = sorted(
            available.filter(name=p.name, evr=p.evr, arch=p.arch), key=lambda a: not a.reponame.startswith("xcraft-")
        )
        if found:
            result[p.name] = found[0]
        else:
            missing.append(p.name)
if missing:
    sys.exit(f"installed but not in any repository: {' '.join(sorted(missing))}")
Path(sys.argv[1]).write_text(
    json.dumps(
        {
            name: {
                "evr": p.evr,
                "arch": p.arch,
                "source": p.source_name,
                "repo": "xcraft" if p.reponame.startswith("xcraft-") else p.reponame,
                "sha256": p.chksum[1].hex(),
            }
            for name, p in result.items()
        }
    )
)
