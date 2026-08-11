# Nomad Link for Houdini

Two SOPs for [Houdini](https://www.sidefx.com), speaking the protocol in
[PROTOCOL.md](../PROTOCOL.md):

- **Nomad Link In** — a source SOP, like a File SOP: it builds whatever Nomad
  sends (selection, whole scene, or live sculpt strokes) as Houdini geometry.
- **Nomad Link Out** — a pass-through SOP: its input geometry becomes a mesh in
  Nomad, on a button press, on every cook, or when Nomad's own *Get* asks.

Nomad is the server, Houdini is a client. One connection is shared by every
node in the session.

## Install

1. Put this repository (or just its `houdini` folder) somewhere permanent.
2. Copy `packages/nomad_link.json` into `$HOUDINI_USER_PREF_DIR/packages`
   (`~/Library/Preferences/houdini/22.0/packages` on macOS). Edit `NOMAD_LINK`
   inside it if the folder is not `$HOME/nomad-link/houdini`.
3. Build the two assets once:

```
hython houdini/build_hda.py
```

That writes `otls/nomad_link.hda`, which the package file already puts on
`HOUDINI_PATH`. A prebuilt copy is committed, so this is only needed after
changing the parameter interface; the Python is loaded live from
`houdini/python/nomad_link`.

## Use

In Nomad, open the **Link** menu to start the server. Connect from Houdini and
approve the request the first time. The pair token is saved to
`$HOUDINI_USER_PREF_DIR/nomad_link_tokens.json`, so later sessions reconnect
silently.

**Nomad → Houdini.** Drop a *Nomad Link In*, press **Connect** (leave Host empty
to discover Nomad by UDP broadcast/Bonjour), then **Get Selection** or
**Get Scene**. The SOP rebuilds itself as new data arrives, including live
`mesh_delta` strokes, so it can sit in the network and keep up with sculpting.

| Nomad | Houdini |
|---|---|
| positions | `P` (quads and n-gons kept) |
| UVs | vertex `uv` (v flipped to Houdini's bottom-left origin) |
| vertex colour / opacity | point `Cd` / `Alpha` |
| roughness, metalness | point `rough`, `metallic` |
| sculpt mask, dyntopo density | point `mask`, `density` |
| face groups | prim `nomad_face_group` |
| object name | prim `name` (Split/Group by `name` separates meshes) |

**Houdini → Nomad.** Drop a *Nomad Link Out* under the geometry you want to send
and press **Send to Nomad**. *Auto Send on Change* sends once per cook (off by
default — that is a lot of traffic for heavy geometry), and *Answer Nomad's Get*
replies to `request_selection` / `request_scene`. The same attributes travel
back. N-gons are sent as `face_format: "corners"` when Nomad advertises `ngon`,
otherwise they are fanned into triangles here first.

Both node types carry the same connection parameters, so you only connect once.

## Conventions

- Both applications are right-handed **Y-up**: positions cross unchanged.
- **Flip Winding** is on by default, because Houdini's polygons are wound
  clockwise from the front while Nomad follows glTF. `tests/test_houdini.py`
  measures this rather than assuming it.
- **Scale** multiplies positions on the way through; use reciprocal values on In
  and Out for a clean round trip.
- **Apply Object Transform**: In bakes Nomad's `world_matrix` into the points,
  Out sends the containing object's transform as the mesh's `world_matrix`.

Not implemented: materials and textures, lights, cameras, view sync, sculpt
layers, and outgoing `mesh_delta` — every Houdini send is a full mesh, which is
one undo step in Nomad. The capabilities in the `hello` are honest about this.

## Layout

```
python/nomad_link/transport.py   copy of examples/transport.py, as in blender/
python/nomad_link/convert.py     mesh_full/mesh_delta <-> flat numpy arrays
python/nomad_link/client.py      the one connection, mesh cache, message handling
python/nomad_link/nodes.py       SOP cooks, parm callbacks, menus
build_hda.py                     builds otls/nomad_link.hda
tests/                           see below
```

The socket lives in `transport.Connection`'s thread; everything else runs on
Houdini's main thread from `hou.ui.addEventLoopCallback`, so SOP cooks only ever
touch the decoded mesh cache. Nomad's data arriving bumps a hidden `revision`
parameter, which is what makes the In SOP recook.

Without the assets you can drive it from the Python Shell:

```python
import nomad_link
nomad_link.connect()                       # empty = discover
nomad_link.client().request("request_selection")
nomad_link.client().meshes                 # decoded meshes by mesh_id
```

## Tests

```
hython tests/run_all.py    # everything
python3 tests/run_all.py   # all but test_houdini.py (needs numpy only)
```

- `test_convert.py` — codec round trips, including the n-gon split path
- `test_link.py` — the client against a mock Nomad: handshake and pairing,
  `mesh_full`, `mesh_delta`, `mesh_instance` and its `request_mesh` recovery,
  `object_state`, `object_delete`
- `test_nodes.py` — SOP array bookkeeping against a fake `hou`
- `test_houdini.py` — the real assets in Houdini: both directions, a live delta
  recooking the In SOP, and Nomad's *Get* being answered

Developed against Houdini 22.0.368 and a real Nomad on iPad.
