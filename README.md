# Nomad Link

[Nomad Sculpt](https://nomadsculpt.com) hosts a socket protocol for live two-way scene
sync with other applications: meshes, sculpt layers, paint, materials, lights, cameras.

**[PROTOCOL.md](PROTOCOL.md) is the wire specification** — enough to write a bridge for
any application, in any language.

## Contents

- `PROTOCOL.md` — the specification
- `transport.py` — framing and discovery, imported by the examples
- `viewer.py` — minimal read-only client, prints every scene message
- `zbrush.py` — desktop ZBrush through GoZ (run it on the ZBrush computer)
- `cozyblanket.py` — CozyBlanket retopology round trip
- `blender/nomad_blender_link` — the Blender extension, and the complete reference
  client: it implements every part of the specification
- `blender/repository` — the published extension repository (`index.json` + archive)

```
python3 zbrush.py --help
```

In Nomad, open the **Link** menu to start the server.

## Blender extension

Install it in Blender by adding the remote repository
`https://nomadsculpt.com/blender/index.json`, which serves the contents of
`blender/repository`. Installing from the repository (rather than a bare ZIP) lets the
extension update itself when the protocol minimum changes.

Never rebuild an archive for a version that has already been published — an index and an
archive that disagree will break existing installs. Any change ships as a new version.

## License

MIT, except `blender/nomad_blender_link`, which is GPL-3.0-or-later as Blender
extensions must be. Each carries its own `LICENSE`.
