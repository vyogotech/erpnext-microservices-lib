#!/usr/bin/env python
"""Emit Frappe's and ERPNext's own dependency list, minus the excluded packages.

Frappe declares ~75 dependencies in pyproject.toml (there is no requirements.txt
to override), sized for a full Desk install. Installing all of them and then
uninstalling ~35 wastes the download and makes the build log claim we ship
packages we do not.

Instead the Containerfile installs frappe and erpnext with --no-deps, then runs
this script to read what they actually declare -- from their installed metadata,
so a version bump picks up new dependencies automatically -- and drops the names
listed in scripts/excluded_packages.txt. Only the exclusions are hand-maintained.

Requirement strings are emitted verbatim, so git URLs (PyPika, gunicorn) and any
environment markers survive. Output is a requirements file for `pip install -r`,
which avoids shell-quoting the `;` in markers.

    python scripts/resolve_frappe_deps.py > /tmp/frappe-deps.txt
    pip install -r /tmp/frappe-deps.txt

Anything excluded here that is still required transitively by a package we do
keep will be reinstalled by pip. That is a safety net, not a bug.
"""

import os
import re
import sys
from importlib.metadata import PackageNotFoundError, requires

PACKAGES = ("frappe", "erpnext")
DEFAULT_EXCLUDES = os.path.join(os.path.dirname(__file__), "excluded_packages.txt")
DEFAULT_EXTRAS = os.path.join(os.path.dirname(__file__), "extra_packages.txt")


def normalise(name: str) -> str:
    """PEP 503-ish: case-insensitive, - and _ and . equivalent."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def load_excludes(path: str) -> set[str]:
    out = set()
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(normalise(line))
    return out


def requirement_name(req: str) -> str:
    """Extract the distribution name from a PEP 508 requirement string."""
    head = req.split(";", 1)[0]          # drop environment marker
    head = head.split("@", 1)[0]         # drop direct URL
    head = head.split("[", 1)[0]         # drop extras
    head = re.split(r"[<>=!~ (]", head, 1)[0]
    return normalise(head)


def main() -> int:
    excludes = load_excludes(os.getenv("EXCLUDED_PACKAGES", DEFAULT_EXCLUDES))

    seen: set[str] = set()
    kept: list[str] = []
    dropped: list[str] = []

    for pkg in PACKAGES:
        try:
            reqs = requires(pkg) or []
        except PackageNotFoundError:
            print(f"{pkg} is not installed -- run pip install --no-deps first",
                  file=sys.stderr)
            return 1
        for req in reqs:
            # Skip requirements that only apply to an extra we do not install.
            if re.search(r";\s*extra\s*==", req):
                continue
            name = requirement_name(req)
            if not name or name in seen:
                continue
            seen.add(name)
            (dropped if name in excludes else kept).append(req.split(";")[0].strip()
                                                           if name in excludes else req)

    # Packages frappe/erpnext import without declaring. --no-deps makes the gap
    # visible; with the old install-then-uninstall flow they arrived by accident.
    extras_path = os.getenv("EXTRA_PACKAGES", DEFAULT_EXTRAS)
    extras = []
    if os.path.exists(extras_path):
        with open(extras_path) as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line and normalise(line) not in seen:
                    extras.append(line)

    for req in kept + extras:
        print(req)

    print(f"resolved {len(kept)} dependencies, excluded {len(dropped)}, "
          f"added {len(extras)} undeclared", file=sys.stderr)
    for req in extras:
        print(f"  undeclared {req}", file=sys.stderr)
    for req in sorted(dropped):
        print(f"  excluded {req}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
