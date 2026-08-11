# Nomad Link

[Nomad Sculpt](https://nomadsculpt.com) hosts a socket protocol for live two-way scene
sync with other applications: meshes, sculpt layers, paint, materials, lights, cameras.

**[PROTOCOL.md](PROTOCOL.md) is the wire specification** — enough to write a bridge for
any application, in any language.

Version **0.11.36**, protocol **1**.

## Contents

- `PROTOCOL.md` — the specification
- `examples/transport.py` — framing and discovery, imported by the others
- `examples/viewer.py` — minimal read-only client, prints every scene message
- `examples/zbrush.py` — desktop ZBrush through GoZ (run it on the ZBrush computer)
- `examples/cozyblanket.py` — CozyBlanket retopology round trip
- `blender/nomad_blender_link` — the Blender extension, and the complete reference
  client: it implements every part of the specification
- `blender/repository` — the published extension repository (`index.json` + archive)

## Running a bridge

Download **[nomad-link-bridges.zip](https://github.com/stephomi/nomad-link/releases/latest/download/nomad-link-bridges.zip)**
— the scripts only, no Blender extension. Unzip it, then run the one you want from that
folder (Python 3, no dependencies to install):

```
python3 zbrush.py --help
```

In Nomad, open the **Link** menu to start the server. The bridge finds it on the local
network; approve the connection request in Nomad.

## Blender extension

Install it in Blender by adding the remote repository
`https://nomadsculpt.com/blender/index.json`, which serves the contents of
`blender/repository`. Installing from the repository (rather than a bare ZIP) lets the
extension update itself when the protocol minimum changes.

## License

MIT, except `blender/nomad_blender_link`, which is GPL-3.0-or-later as Blender
extensions must be. Each carries its own `LICENSE`.

<!--
Release: bump the version above, then

sh make-release.sh
-->
