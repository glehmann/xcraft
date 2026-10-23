"""Write as JSON the installed packages, and those providing the given requirements.

The installed packages built by xcraft have the repository ``xcraft``, whose
id in the containers is ``xcraft-<project id>``. Run as
root in the build environment, with the dnf python API, which does not apply
the versionlock: ``record.py <output> [<requirements>]``, the requirements one per line, when the file exists.
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
installed = base.sack.query().installed().filter(name__neq="gpg-pubkey")
result, missing = {}, []
for p in installed:
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
lines = [line for f in sys.argv[2:] if (path := Path(f)).exists() for line in path.read_text().splitlines()]
providers = {
    r: sorted({p.name for p in installed.filter(provides=r) or (installed.filter(file=r) if r.startswith("/") else [])})
    for r in lines
    if not r.startswith("rpmlib(")
}
Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "installed": {
                name: {
                    "evr": p.evr,
                    "arch": p.arch,
                    "source": p.source_name,
                    "repo": "xcraft" if p.reponame.startswith("xcraft-") else p.reponame,
                    "sha256": p.chksum[1].hex(),
                }
                for name, p in result.items()
            },
            "providers": providers,
        }
    )
)
