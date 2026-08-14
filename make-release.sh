#!/bin/sh
# Maintainer only: publishes the release. Bump the version in README.md, write the
# CHANGELOG.md section, run this.
set -e
cd "$(dirname "$0")"
PWD_ROOT=$PWD

[ -f ../../src/link/LinkProtocol.hpp ] || {
    echo "Nothing to do here — this publishes the release, it is not needed to use the bridges."
    exit 0
}

BLENDER=${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}
command -v gh >/dev/null || { echo "gh is missing: brew install gh && gh auth login" >&2; exit 1; }
[ -x "$BLENDER" ] || { echo "no Blender at $BLENDER, set BLENDER=" >&2; exit 1; }

ver=$(sed -n 's/^Version \*\*\([^*]*\)\*\*.*/\1/p' README.md)
[ -n "$ver" ] || { echo "no version in README.md" >&2; exit 1; }
gh release view "$ver" >/dev/null 2>&1 &&
    { echo "$ver is already released, bump README.md" >&2; exit 1; }

# release notes = the CHANGELOG.md section of that version
notes=$(mktemp)
trap 'rm -f "$notes"' EXIT
awk -v head="## $ver" '$0 == head { on = 1; next } /^## / { on = 0 } on' CHANGELOG.md > "$notes"
grep -q '[^[:space:]]' "$notes" ||
    { echo "no '## $ver' section in CHANGELOG.md" >&2; exit 1; }

# propagate the version
sed -i '' "s/^VERSION = \"[^\"]*\"/VERSION = \"$ver\"/" examples/transport.py
sed -i '' "s/^version = \"[^\"]*\"/version = \"$ver\"/" blender/nomad_blender_link/blender_manifest.toml
sed -i '' "s/BRIDGE_VERSION = \"[^\"]*\"/BRIDGE_VERSION = \"$ver\"/g" ../../src/link/LinkProtocol.hpp
cp examples/transport.py houdini/python/nomad_link/
cp examples/transport.py toolbag/NomadLink/

# bridges archive (stable name, /releases/latest/download/ depends on it)
rm -f nomad-link-bridges.zip
zip -qX -j nomad-link-bridges.zip README.md CHANGELOG.md LICENSE PROTOCOL.md examples/*.py

# houdini archive (unpacks to a nomad-link-houdini folder, matching NOMAD_LINK in it)
rm -f nomad-link-houdini.zip
staging=$(mktemp -d)
cp -R houdini "$staging/nomad-link-houdini"
sed -i '' 's|\$HOME/nomad-link/houdini|$HOME/nomad-link-houdini|' \
    "$staging/nomad-link-houdini/packages/nomad_link.json"
(cd "$staging" && zip -qr -X "$PWD_ROOT/nomad-link-houdini.zip" nomad-link-houdini \
    -x "*.DS_Store" -x "*__pycache__*" -x "*.pyc" -x "*/otls/backup/*")
rm -rf "$staging"

# toolbag archive (unpacks to the NomadLink plugin folder, plus the probe)
rm -f nomad-link-toolbag.zip
staging=$(mktemp -d)
cp -R toolbag "$staging/nomad-link-toolbag"
(cd "$staging" && zip -qr -X "$PWD_ROOT/nomad-link-toolbag.zip" nomad-link-toolbag \
    -x "*.DS_Store" -x "*__pycache__*" -x "*.pyc")
rm -rf "$staging"

# blender extension archive + repository index
cp examples/transport.py blender/nomad_blender_link/
rm -f blender/repository/nomad_blender_link-*.zip
(cd blender/nomad_blender_link &&
    zip -qr -X ../repository/nomad_blender_link-$ver.zip . -x "*.DS_Store" -x "*__pycache__*" -x "*.pyc")
"$BLENDER" --command extension server-generate --repo-dir="$PWD/blender/repository" >/dev/null

gh release create "$ver" nomad-link-bridges.zip nomad-link-houdini.zip \
    nomad-link-toolbag.zip --title "$ver" --notes-file "$notes"

echo "$ver done — upload blender/repository/* to https://nomadsculpt.com/blender/"
