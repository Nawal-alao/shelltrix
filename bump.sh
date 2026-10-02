#!/bin/sh
#
# shelltrix — release version bump.
#
# The version is hardcoded in six places (pyproject, __init__, install.sh x2,
# README x3). Forgetting one of them ships an installer pinned to the previous
# tag while the package claims a newer version, so the edit is done here once
# and verified by `tests/test_security.py::test_pinned_ref_matches_the_declared_version`.
#
# Usage:
#   ./bump.sh <x.y.z>            # rewrite the version everywhere
#   ./bump.sh <x.y.z> --tag      # ... and create the annotated tag
#
# Write the CHANGELOG section first: the script refuses to bump a version that
# has no `## [x.y.z]` entry, so release notes can never lag the tag.
#
# This script only edits files. Review the diff, then commit and push yourself.

set -e

cd "$(dirname "$0")"

[ $# -ge 1 ] || {
    echo "usage: ./bump.sh <x.y.z> [--tag]" >&2
    exit 2
}

NEW="$1"
shift
MAKE_TAG=0
for arg in "$@"; do
    case "$arg" in
        --tag) MAKE_TAG=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

python3 - "$NEW" "$MAKE_TAG" <<'PY'
import re
import subprocess
import sys
from pathlib import Path

new = sys.argv[1]
make_tag = sys.argv[2] == "1"

if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", new):
    sys.exit(f"refusing {new!r}: expected a bare x.y.z version")

targets = {
    "pyproject.toml": None,
    "src/shelltrix/__init__.py": None,
    "install.sh": None,
    "README.md": None,
    "CHANGELOG.md": None,
}
for name in targets:
    if not Path(name).is_file():
        sys.exit(f"{name} not found (wrong directory?)")

dirty = subprocess.run(
    ["git", "status", "--porcelain", "--untracked-files=no"],
    capture_output=True, text=True,
).stdout.strip()
if dirty:
    # Only tracked changes matter: the bump rewrites tracked files, and a
    # stray untracked file (a scratch note, a .venv symlink) is irrelevant.
    sys.exit("tracked files are modified; commit or stash before bumping")

# The current version comes from pyproject.toml: exactly one `version = "..."`.
raw = Path("pyproject.toml").read_text()
found = re.findall(r'^version = "([^"]+)"', raw, re.M)
if len(found) != 1:
    sys.exit(f"expected exactly one `version = \"...\"` in pyproject.toml, found {len(found)}")
old = found[0]

if new == old:
    sys.exit(f"pyproject.toml already declares {new}")

# Anchored on the *old* version, so a stale or already-hand-edited file fails
# loudly instead of silently keeping the previous release number.
edits = [
    ("pyproject.toml", f'version = "{old}"', f'version = "{new}"'),
    ("src/shelltrix/__init__.py", f'__version__ = "{old}"', f'__version__ = "{new}"'),
    ("install.sh", f'# `v{old}`', f'# `v{new}`'),
    ("install.sh", f'SHELLTRIX_REF="${{SHELLTRIX_REF:-v{old}}}"', f'SHELLTRIX_REF="${{SHELLTRIX_REF:-v{new}}}"'),
    ("README.md", f"/v{old}/install.sh", f"/v{new}/install.sh"),
    ("README.md", f"`v{old}`", f"`v{new}`"),
    ("README.md", f"@v{old}", f"@v{new}"),
]
for path, before, after in edits:
    text = Path(path).read_text()
    n = text.count(before)
    if n != 1:
        sys.exit(f"{path}: expected exactly 1 occurrence of {before!r}, found {n}")

# Release notes first: a tag must never ship without them.
changelog = Path("CHANGELOG.md").read_text()
if f"## [{new}]" not in changelog:
    sys.exit(f"CHANGELOG.md has no `## [{new}]` section: write the release notes first")

for path, before, after in edits:
    p = Path(path)
    p.write_text(p.read_text().replace(before, after))

tag = f"v{new}"
if make_tag:
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"],
        capture_output=True,
    ).returncode == 0
    if exists:
        sys.exit(f"tag {tag} already exists: tags are immutable, pick another version")

print(f"{old} -> {new} in {len({p for p, _, _ in edits})} files ({len(edits)} edits)")
print(f"changelog section for [{new}] found")

# The coherence test runs against the working tree, before anything is tagged.
# Prefer the project venv: the system python3 has no pytest.
venv = Path(".venv/bin/python")
python = str(venv) if venv.is_file() else sys.executable
proc = subprocess.run(
    [python, "-m", "pytest",
     "tests/test_security.py", "-q", "-k", "SupplyChain"],
    capture_output=True, text=True,
)
sys.stdout.write(proc.stdout)
if proc.returncode != 0:
    sys.stderr.write(proc.stderr)
    sys.exit("coherence test failed: nothing committed, nothing tagged")

if make_tag:
    subprocess.run(["git", "tag", "-a", tag, "-m", f"shelltrix {new}"], check=True)
    print(f"tag {tag} created on HEAD")

print("\nnext:")
print(f'  git commit -am "chore(release): bump version to {new}"')
if make_tag:
    print(f"  git push origin {tag}   # tag first, then main: never the reverse")
print("  git push origin main")
PY