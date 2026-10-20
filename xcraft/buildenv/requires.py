"""Write as JSON the requirements between upstream packages, and the upstream providers of capabilities.

Run in the build environment, with the dnf python API: ``requires.py <input> <output>``.
The input has the locked packages, as ``{name: [evr, arch]}``, and the capabilities.
Only those packages are providers: in their locked version, or their latest one
when the repositories do not have it anymore.
"""

import json
import sys
from pathlib import Path

import dnf  # ty: ignore[unresolved-import]

data = json.loads(Path(sys.argv[1]).read_text())
locked = data["packages"]
base = dnf.Base()
base.conf.read()
base.read_all_repos()
base.fill_sack(load_system_repo=False)
chosen = {}
for p in base.sack.query().available().filter(name=list(locked), latest_per_arch=True):
    if p.arch == locked[p.name][1]:
        chosen[p.name] = p
for p in base.sack.query().available().filter(name=list(locked)):
    if [p.evr, p.arch] == locked[p.name]:
        chosen[p.name] = p
candidates = base.sack.query().filter(pkg=list(chosen.values()))
resolved = {}


def providers(requirement: object) -> list[str]:
    """Return the names of the candidates providing `requirement`, a capability, a file or a rich dependency."""
    key = str(requirement)
    if key not in resolved:
        try:
            found = candidates.filter(provides=requirement) or (
                candidates.filter(file=key) if key.startswith("/") else []
            )
        except (dnf.exceptions.Error, ValueError):
            found = []
        resolved[key] = sorted({p.name for p in found})
    return resolved[key]


Path(sys.argv[2]).write_text(
    json.dumps(
        {
            "requires": {
                p.name: sorted({name for r in p.requires for name in providers(r)} - {p.name}) for p in candidates
            },
            "providers": {capability: providers(capability) for capability in data["capabilities"]},
        }
    )
)
