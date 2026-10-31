"""Write as JSON the installed packages.

They are read from the database of rpm, without the metadata of the upstream repositories, which may not have them
anymore: those built by xcraft have the repository ``xcraft``, those of the repository of our builds, whose id in the
containers is ``xcraft-<project id>``, the others ``upstream``, without their SHA-256, which only their downloads
checked, see xcraft.pinned. Run as root in the build environment, with the dnf python API, which does not apply the
versionlock: ``record.py <output>``.
"""

import json
import sys
from pathlib import Path

import dnf  # ty: ignore[unresolved-import]


def main() -> None:
    """Record the installed packages."""
    base = dnf.Base()
    base.conf.read()
    base.read_all_repos()
    for repo in base.repos.iter_enabled():
        if not repo.id.startswith("xcraft-") or repo.id == "xcraft-pinned":
            repo.disable()
    base.fill_sack(load_system_repo=True)
    ours = base.sack.query().available()
    Path(sys.argv[1]).write_text(
        json.dumps(
            {
                "installed": {
                    p.name: {
                        "evr": p.evr,
                        "arch": p.arch,
                        "source": p.source_name,
                        # built here, rather than from the same version upstream
                        "repo": "xcraft" if ours.filter(name=p.name, evr=p.evr, arch=p.arch) else "upstream",
                        "sha256": "",
                    }
                    for p in base.sack.query().installed().filter(name__neq="gpg-pubkey")
                },
            }
        )
    )


if __name__ == "__main__":
    main()
