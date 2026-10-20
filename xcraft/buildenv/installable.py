"""Check that each package of a repository can be installed from it alone, on an empty system.

Run as root in the build environment: ``installable.py <repository> <output>``. It writes as JSON the packages that
cannot, with why. Unlike repoclosure, the resolution evaluates rich dependencies, like ``(a if b)``.
"""

import json
import sys
import tempfile
from pathlib import Path

import dnf  # ty: ignore[unresolved-import]

base = dnf.Base()
base.conf.read()
base.conf.installroot = tempfile.mkdtemp()
base.conf.install_weak_deps = False
base.repos.add_new_repo("checked", base.conf, baseurl=[f"file://{sys.argv[1]}"])
base.fill_sack(load_system_repo=False)
errors = {}
for package in base.sack.query().available():
    base.reset(goal=True)
    try:
        base.package_install(package)
        base.resolve()
    except dnf.exceptions.Error as e:
        errors[str(package)] = str(e)
Path(sys.argv[2]).write_text(json.dumps(errors))
