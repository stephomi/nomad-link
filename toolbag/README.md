# Nomad Link for Marmoset Toolbag

Sends a Nomad Sculpt scene straight into Toolbag 4/5 — geometry, vertex paint, UVs,
materials, textures and the environment — and keeps it updated while you sculpt.

Geometry travels one way. Toolbag renders and bakes; it does not sculpt, so the bridge
never sends meshes back and its handshake says so.

## Install

1. In Toolbag: **Edit → Plugins → Show User Plugin Folder**.
2. Copy the `NomadLink` folder into it.
3. **Edit → Plugins → Refresh**, then run **NomadLink**.

That is everything — no dependencies, no setup.

## Use

1. In Nomad, open the **Link** menu and start the server.
2. In Toolbag's Nomad Link window, press **Connect**. The first time, approve the request
   in Nomad; after that it reconnects on its own. The same button reads **Disconnect**
   while a connection is wanted.
3. In Nomad, press **Send to other** — or press **Get scene** in Toolbag, which asks for
   it from this end. **Get selection** takes only what is selected in Nomad, and
   **Replace all** destroys everything the bridge created here before asking again.

The sculpt appears in Toolbag and keeps updating stroke by stroke. Toolbag pauses every
plugin while it is in the background -- that is the application, not the bridge -- so an
unfocused window catches up the moment you click back into it.


## What travels

| | |
|---|---|
| Meshes | positions, quads kept as quads, UVs, vertex paint (color + opacity) |
| Updates | per-stroke deltas, paint refreshes, rename, hide, move, delete |
| Instances | shared geometry, each copy placed by its own matrix |
| Materials | roughness, metalness, color, opacity, and the texture channels below |
| Textures | color, roughness, metalness, normal, emissive, occlusion, opacity, displacement, each with its factor |
| Environment | Nomad's HDRI onto the Toolbag sky, with its rotation, exposure and blur |
| Lights | sun/point/spot as Toolbag lights: color or kelvin, brightness, cone, shadows |
| Cameras | Nomad cameras as scene cameras to render through, incl. orthographic |
| View | Nomad's camera, when **Follow Nomad's view** is ticked — the same shared setting Nomad and the Blender add-on call Working View, so it moves for every connected app at once |

Deliberately not mapped:

- **Hierarchy.** Everything arrives at the scene root, placed by its world matrix, which
  `PROTOCOL.md` §12 allows explicitly.
- **Sculpt layers and masks.** They composite into what you see before sending.
- **Texture placement.** Projection, wrapping, offset/scale/rotation stay at Toolbag's
  defaults; triplanar is a global Toolbag setting rather than a per-material one.

## Speed

Per mesh update, on an M-series laptop: 22 k vertices ~110 ms, 90 k ~330 ms, 490 k ~1.8 s.
Halve those where Toolbag makes its own normals.

Topology is computed once and reused, so a stroke only re-sends positions and colors.

Standard library only. Toolbag runs plugins in a Python sub-interpreter, which numpy warns
it does not support and can crash in, so there is nothing to install and no fast path to
miss.

## How geometry is mapped

- **Transforms are baked into the vertices** and the object stays at the origin. Toolbag
  exposes only Euler angles and does not document their order, and baking is exact for
  skewed nodes and instances too. Moving an object in Nomad re-bakes it.
- **Vertices are split along UV seams**, because Nomad indexes UVs per face corner and
  Toolbag stores one UV per vertex. Meshes without UVs keep Nomad's own indexing.
- **Quads become triangles.** Toolbag's polygon table rejects everything the bridge
  offers it (`convert.SEND_POLYGONS`), so the quad grouping is held back until the
  units it wants are known.
- **Normals are computed here**, area-weighted, unless Toolbag turns out to make its
  own — the first mesh of a session measures that on a throwaway object.
- **Vertex paint** becomes Toolbag vertex colors and the albedo slot switches to a
  vertex-color shader. A color texture wins over paint when a mesh has both, because
  Toolbag's albedo slot holds one or the other.

## Checking Toolbag's conventions

Press **Run probe** in the Nomad Link window. It answers the questions Toolbag's
documentation leaves open on your build: mesh API shapes, its Euler rotation order, the
real material field names, and whether it computes normals for you. Read the result in
the console (Cmd+Shift+C / Ctrl+~); it also writes `nomad_probe_report.txt` into the
plugin folder.

It has to run inside Toolbag, since `mset` exists only there — `python3 probe.py` in a
terminal only reports that the module is missing.

It leaves one textured **Nomad probe quad** in the scene for the two things a script
cannot read back:

- The quad's **top-left corner should be red**. If it is blue, set `FLIP_V = False` in
  `NomadLink/convert.py`.
- The quad should be **solid seen from the front**. If it is culled, the winding needs
  reversing in `triangulate`.

Delete the probe quad and its material when you are done; nothing else in the scene is
touched. Running it again reuses them rather than piling up copies.

## When Toolbag crashes

Toolbag dies inside its own C++ on data it does not like, with no traceback and nothing
in the console, so both paths name the call they are about to make in a file first:

- `nomad_probe_last.txt` — the probe. Whatever is left in it is what killed Toolbag;
  running the probe again says so, skips that one call and finishes the rest of the
  report. A run that reaches the end empties the file.
- `nomad_link_last.txt` — the live bridge, holding the last dozen steps: the message
  being handled and every mset call the write makes (`scene.TRACE`).

Both sit in the plugin folder. In the bridge's file, `queue drained` as the last line
means every call the bridge made returned and Toolbag died afterwards, on its own work
on data it had accepted — for that, the first thing to try is `LEARN_NORMALS = False` in
`NomadLink/scene.py`, which sends our normals instead of letting Toolbag build them.

## Tests

No Toolbag needed — `tests/fake_mset.py` stands in for it and `tests/mock_nomad.py` for
Nomad:

```
python3 tests/run_all.py
```
