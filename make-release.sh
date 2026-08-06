#!/bin/sh
# Bump the version in README.md, run this. Everything else follows from it.
set -e
cd "$(dirname "$0")"

BLENDER=${BLENDER:-/Applications/Blender.app/Contents/MacOS/Blender}
ver=$(sed -n 's/^Version \*\*\([^*]*\)\*\*.*/\1/p' README.md)
[ -n "$ver" ] || { echo "no version in README.md" >&2; exit 1; }
ls blender/repository/nomad_blender_link-$ver.zip >/dev/null 2>&1 &&
    { echo "$ver is already published, bump README.md" >&2; exit 1; }

# propagate the version
sed -i '' "s/^VERSION = \"[^\"]*\"/VERSION = \"$ver\"/" examples/transport.py
sed -i '' "s/^version = \"[^\"]*\"/version = \"$ver\"/" blender/nomad_blender_link/blender_manifest.toml
[ -f ../../src/link/LinkProtocol.hpp ] &&
    sed -i '' "s/BRIDGE_VERSION = \"[^\"]*\"/BRIDGE_VERSION = \"$ver\"/g" ../../src/link/LinkProtocol.hpp

# bridges archive (stable name, /releases/latest/download/ depends on it)
rm -f nomad-link-bridges.zip
zip -qX -j nomad-link-bridges.zip README.md LICENSE PROTOCOL.md examples/*.py

# blender extension archive + repository index
cp examples/transport.py blender/nomad_blender_link/
rm -f blender/repository/nomad_blender_link-*.zip
(cd blender/nomad_blender_link &&
    zip -qr -X ../repository/nomad_blender_link-$ver.zip . -x "*.DS_Store" -x "*__pycache__*" -x "*.pyc")
"$BLENDER" --command extension server-generate --repo-dir="$PWD/blender/repository" >/dev/null

gh release create "$ver" nomad-link-bridges.zip --title "$ver" --notes ""

echo "$ver done — upload blender/repository/* to https://nomadsculpt.com/blender/"
