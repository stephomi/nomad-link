# Nomad Link Protocol

Nomad Sculpt exposes a socket protocol for live two-way scene synchronization with
external applications.
- The Blender extension (`blender/nomad_blender_link`, GPL-3.0) is
the complete reference client;
- The Python bridges next to this file (`viewer.py`,
`zbrush.py`, `cozyblanket.py`, MIT) are standalone examples.

This document specifies the
wire protocol so bridges for other applications (Maya, Houdini, game engines, custom
tools) can be written in any language.

Nomad is always the **server**; the bridge is always the **client**. Up to 8 clients may
connect at once; exactly one of them is the *editor* that sends live edits (§5).

- Protocol version: **1** (integer; changes only on breaking framing/handshake changes)
- Default TCP port: **48312**

## 1. Transport and framing

Plain TCP. Every packet in both directions is one frame:

| bytes | content |
|---|---|
| 4 | JSON payload size, unsigned 32-bit **big-endian** |
| 4 | binary payload size, unsigned 32-bit **big-endian** |
| n | JSON object, UTF-8 |
| m | binary payload (may be empty) |

Limits: JSON ≤ 1 MiB, binary ≤ 1 GiB. Oversized frames should cause a disconnect.
Every JSON payload is an object with a `"type"` string; unknown types and unknown
fields must be ignored (forward compatibility). All binary offsets in headers are byte
offsets into the frame's binary payload; headers carrying binary also carry
`"binary_size"` which must equal the payload size.

**WebSocket variant**: Nomad running in a browser cannot open raw TCP, so it dials a
WebSocket (`ws://host:port/`) instead; each *binary WebSocket message* carries exactly
one frame in the layout above. Everything else — handshake, roles, messages — is
identical. The browser adopts its role from the first hello it receives: a Nomad host
acks its join hello (`nomad_version` present) and the browser stays a joined client; a
client listener (the Blender extension's "Listen for Nomad Web") sends its own client
hello and the browser takes the host role. Nomad hosts accept the HTTP upgrade on the
same TCP port they listen on, so a browser can join them directly.

## 2. Discovery (optional)

Two mechanisms; a bridge may implement either or both, or let the user type the address.

- **UDP broadcast**: send the ASCII datagram `NOMAD_LINK_DISCOVER 1` to
  `255.255.255.255:<port>`. Nomad replies to the sender with JSON:
  `{"type": "nomad_link", "name": "Nomad Sculpt", "protocol": 1, "port": 48312}`.
  Use the reply's source IP as the host.
- **Bonjour/mDNS**: Nomad advertises `_nomadlink._tcp` on Apple platforms. The SRV
  record carries the port; use the responder's IP as the host. (Preferred for iPad,
  where receiving raw broadcasts is restricted by the OS.)

## 3. Conventions

- **Coordinate system**: right-handed, **Y up** (glTF convention). Headers carry
  `"coordinate_system": "nomad_y_up"` where relevant. Units are arbitrary scene units.
- **Matrices**: 16 floats, **column-major** (`world_matrix[column*4 + row]`).
- **Mesh transforms**: vertex positions are in node-local space; `world_matrix` places
  the node. When a transform contains skew that a target application cannot represent,
  Nomad also sends `world_matrix_parent` and `local_matrix` (world = parent × local,
  both skew-free); a bridge may reproduce the split with a helper parent or ignore it
  and use `world_matrix`.
- **Lights and cameras** aim along their local **-Z** axis with +Y up (glTF convention).
  Their `world_matrix` is a world frame — no vertex data exists to re-express, so map it
  directly between coordinate systems (do not conjugate like a mesh transform).
- **Faces**: triangles and quads only, as `int32x4`; a triangle sets the 4th index to -1.
- **Ids**: `mesh_id` / `link_id` / `geometry_id` are opaque strings chosen by whichever
  side first names the entity (UUIDs recommended). They are stable for the life of the
  link and should be persisted by both sides.

## 4. Handshake and pairing

The client connects and immediately sends:

```json
{
    "type": "hello",
    "protocol": 1,
    "pair_token": "<stored token or empty>",
    "bridge_version": "1.2.3",
    "client_name": "Blender",
    "capabilities": ["selection_transfer", "scene_transfer", "..."]
}
```

- Wrong `protocol` → `error` + disconnect.
- Known `pair_token` → Nomad replies `hello` at once.
- Unknown/empty token → Nomad replies `{"type": "pairing_pending"}` and waits for the
  user to accept the connection in Nomad's UI. Show "waiting for approval" and keep the
  connection open. On acceptance the `hello` reply arrives; on refusal, `error` +
  disconnect.

Nomad's `hello` reply:

```json
{
    "type": "hello",
    "protocol": 1,
    "nomad_version": "2.x",
    "bridge_version": "...",
    "minimum_bridge_version": "...",
    "capabilities": ["..."],
    "pair_token": "<present only when a new pairing was just approved>"
}
```

Store `pair_token` persistently and send it in future hellos for silent reconnects.
`bridge_version`/`minimum_bridge_version` implement the reference client's self-update
against its extension repository; third-party bridges may ignore them (compare your own
versioning independently). `{"type": "ping"}` → `{"type": "pong"}` is available as a
keepalive. `{"type": "error", "message": "...", "request_id": "..."}` may arrive at any
time; treat it as "resynchronize anything you were assuming" (see §9).

### Capabilities

Sent by both sides; act only on what the peer advertises — it may be an older Nomad or
a partial bridge, and unadvertised messages vanish silently (§1). Current values:

| capability | meaning when advertised |
|---|---|
| `selection_transfer` / `scene_transfer` | peer answers `request_selection` / `request_scene` |
| `scene_edits`, `object_state`, `material`, `light`, `camera_object` | peer understands those live messages |
| `session_config` | peer participates in shared config (§5) |
| `mesh_full`, `mesh_delta`, `mesh_attributes`, `sculpt_layers` | Nomad → bridge data kinds |
| `camera` | advertiser sends working-view messages (§10); Nomad shows its view-sync toggle only while a peer advertises this |
| `mesh_delta_receive` | advertiser accepts incoming sparse `mesh_delta`; without it send `mesh_full` |
| `mesh_attributes_receive` | advertiser accepts incoming `mesh_attributes` |
| `mesh_instance` | peer understands shared-geometry instances (§8) |
| `display_config` | peer applies display settings (§10.1); Nomad only sends them to peers advertising this |
| `texture` | peer caches texture blobs by immutable id (§10.2); Nomad only sends blobs to peers advertising this |

A minimal one-way bridge (e.g. a game-engine viewer) can implement only `hello`,
`mesh_full` receive, and `object_state` receive.

## 5. Session configuration and source arbitration

Live sync settings are shared state owned by Nomad, replicated by revision number:

- Nomad → client, sent after hello and whenever anything changes:
```jsonc
{
    "type": "session_config",
    "revision": 7,               // monotonic; replicate, echo as base_revision
    "live_sync": true,
    "sync_mode": "auto",         // auto | nomad | client
    "active_source": "client",   // none | nomad | client
    "sync_view": true,           // channel flags (§6)
    "sync_objects": true,
    "sync_materials": true,
    "sync_lights": true,
    "sync_cameras": true,
    "sync_display": false,       // §10.1; optional in set_session_config
    // informational — the masked active_source stays authoritative:
    "peers": ["Nomad iPad"],     // other connected devices, recipient excluded
    "source_name": "Nomad iPad"  // device currently sending live edits (may be you)
}
```
- Client → Nomad: `{"type": "set_session_config", "base_revision": n, ...same flags}`.
  If `base_revision` is stale Nomad answers with the current config instead of applying.
- `{"type": "claim_sync", "source": "client"}` — in `auto` mode, the client claims
  authorship when its user shows meaningful activity; Nomad claims it back on its own
  user activity. Only the current `active_source` sends live edits; the other side
  applies them. Do not claim while you hold stale data (§9).
- With several clients connected, one client at a time is the *editor*; the others are
  viewers fed by Nomad's relay and see `active_source: "nomad"`. A `claim_sync` from a
  live-sync-capable viewer promotes it to editor (the previous editor becomes a viewer),
  so the last client with user activity is the one that sends. `set_session_config` is
  accepted from any client — the session config is shared, changing it does not change
  the editor.
- Nomad itself can join another Nomad's session (Link menu → nearby devices): the
  joining side speaks this exact client protocol — `hello` with a pair token,
  `claim_sync` on activity, the host's `session_config` as the authority — while
  swapping the wire's `nomad`/`client` labels to its own perspective.

("client" always means the connected bridge, whatever the application is.)

## 6. Live flag and channels

Messages that mirror ongoing edits carry `"live_sync": true` and must be **dropped by
the receiver** unless live sync is enabled, the sender is the current `active_source`,
and the matching channel flag (`sync_objects`, `sync_materials`, …) is on. Messages
from explicit user transfers (Send/Get buttons) carry `"live_sync": false` and are
always applied. Requests carry a `request_id` echoed by acks and errors.

## 7. Mesh transfer

### 7.1 `mesh_full`

Complete mesh state in one frame. 
- `*_offset` = byte offset into the binary payload
(§1), paired with a `*_format`.
- ⭐ = always present, the rest is optional. Offsets
below: a cube (8 vertices, 6 quads, 14 UVs, one layer) packed back-to-back — no
alignment required.

```jsonc
{
    "type": "mesh_full",
    "mesh_id": "6f9c…",                   // ⭐
    "geometry_id": "a41d…",               // ⭐ shared-geometry group (§8)
    "name": "Cube",                       // ⭐
    "vertex_count": 8,                    // ⭐
    "face_count": 6,                      // ⭐
    "binary_size": 620,                   // ⭐ must equal the binary payload size
    "coordinate_system": "nomad_y_up",    // ⭐
    "world_matrix": [ /* 16 floats */ ],  // ⭐ column-major (+ skew split, §3)
    "smooth_shading": true,               // ⭐
    "live_sync": false,                   // ⭐ §6
    "request_id": "…",                    // echoed by mesh_ack / error
    "replace_topology": true,             // allow replacing topology/UVs/layers of the
                                          // linked mesh, else the receiver errors (§9);
                                          // always true on Nomad's outgoing fulls

    "position_offset": 0,                 // ⭐ node-local positions, 8 × 12 B
    "position_format": "float32x3",
    "face_offset": 96,                    // ⭐ tris + quads; triangle: 4th index -1
    "face_format": "int32x4",             //   6 × 16 B

    "texcoord_count": 14,                 // UVs — the four fields travel together
    "texcoord_offset": 192,               // 14 × 8 B; v origin is top-left (glTF style),
    "texcoord_format": "float32x2",       //   Blender-style consumers flip v = 1 - v
    "face_uv_offset": 304,                // int32x4 per face: texcoord indices,
                                          // corner order matching the face, 6 × 16 B

    "color_offset": 400,                  // Nomad's native color bytes (u8 r,g,b,m):
    "color_format": "rgbm8",              //   linear rgb = rgb * (m / 65025), 8 × 4 B
    "opacity_offset": 432,                // per vertex paint opacity, 8 × 1 B
    "opacity_format": "uint8_norm",       //   absent = fully opaque
    "roughness_offset": 440,              // per vertex, 8 × 1 B
    "roughness_format": "uint8_norm",
    "metalness_offset": 448,              // per vertex, 8 × 1 B
    "metalness_format": "uint8_norm",

    "base_color_offset": 620,             // un-composited base paint, present only when
    "base_color_format": "rgbm8",         //   the mesh has layers (else base = the
    "base_opacity_offset": 652,           //   composited channels above). Same four
    "base_roughness_offset": 660,         //   channels and formats, "base_" prefixed.
    "base_metalness_offset": 668,         //   Layer-aware peers apply these + the layer
                                          //   paint below; others read the composited set

    "mask_offset": 456,                   // sculpt mask, 1 = unmasked, 8 × 2 B
    "mask_format": "uint16_norm",         //   optional working state, absent = none
    "density_offset": 472,                // dyntopo density paint, 8 × 1 B
    "density_format": "uint8_norm",       //   optional working state, absent = none

    "face_group_offset": 480,             // per face, index into face_groups,
    "face_group_format": "uint16",        // 6 × 2 B
    "face_groups": [
        { "name": "Group 1", "color": [0.8, 0.2, 0.2] }
    ],

    "layers": [                           // sculpt layers, user-layer order
        {
            "name": "Layer 1",
            "factor": 1.0,                // applied weight = factor × factor_offset
            "factor_offset": 1.0,
            "visible": true,              // shown = visible && visible_offset
            "visible_offset": true,
            "factor_color": 1.0,          // per-channel paint weights and visibility
            "factor_roughness": 1.0,
            "factor_metalness": 1.0,
            "factor_opacity": 1.0,
            "visible_color": true,
            "visible_roughness": true,
            "visible_metalness": true,
            "visible_opacity": true,
            "blend_color": 0,             // LayerHelper::BlendMode; a negative
            "blend_roughness": 0,         //   per-channel mode follows blend_color
            "blend_metalness": 0,
            "blend_opacity": 0,
            "offset": 492,                // count sparse records of (uint32 vertex
            "count": 8,                   // index, float32x3 offset), 8 × 16 B;
            "format": "uint32_float32x3", // final position = base + Σ weight·offset

            "color_offset": 676,          // per-layer paint, sparse records of (uint32
            "color_count": 8,             //   index, rgbm8 value, uint8 alpha), 8 × 9 B;
            "color_format": "uint32_rgbm8_alpha8", // absent = unpainted channel
            "roughness_offset": 748,      // gray records (uint32 index, uint8 value,
            "roughness_count": 8,         //   uint8 alpha), 8 × 6 B; metalness and
            "roughness_format": "uint32_uint8_alpha8" // opacity travel likewise
        }
    ],
    "layer_active": -1,                   // layer being edited, -1 = base mesh
                                          // (Blender: active shape key, Basis = -1)

    "material": { /* §10 */ }
}
```

Reply: `{"type": "mesh_ack", "mesh_id": ..., "request_id": ...}` — adopt the acked
`mesh_id` as the link id for the object you sent.

### 7.2 `mesh_delta` — sparse updates (both directions)

After a sculpt/paint stroke, only touched vertices travel. One delta per completed
stroke; each is one undoable step on the receiver.

```jsonc
{
    "type": "mesh_delta",
    "mesh_id": "6f9c…",
    "count": 3,                       // touched vertices
    "vertex_count": 8,                // topology guard, rejected on mismatch
    "binary_size": 60,
    "live_sync": true,
    "world_matrix": [ /* … */ ],      // Nomad → client only

    "index_offset": 0,                // 3 × 4 B
    "index_format": "uint32",
    "position_offset": 12,            // absolute node-local positions, 3 × 12 B
    "position_format": "float32x3",
    "color_offset": 48,               // 3 × 4 B; every §7.1 per-vertex channel may
    "color_format": "rgbm8",          // travel likewise — all channels optional;
                                      // paint sections carry the COMPOSITED values

    "base_color_offset": 60,          // the exact payload from a layered Nomad: the
                                      // un-composited base values for the same indices
                                      // ("base_" + channel, §7.1 formats). A layer-aware
                                      // receiver applies these and skips the composited set
    "layer_index": 0,                 // stroke on a layer: user-order index, plus the
    "layer_color_offset": 64,         //   touched channels as "layer_" + channel value
    "layer_color_alpha_offset": 76    //   sections and their uint8_norm alpha sections
}
```

Deltas require identical topology on both ends — never send one unless your last known
full state matches; on any doubt send `mesh_full`. Nomad rejects deltas for procedural
primitives (`error` → §9). Meshes with sculpt layers accept them: base strokes carry
`base_*` sections, layer strokes carry `layer_index` + `layer_*` sections, and clients
that only track the composite read the plain paint sections either way.

### 7.3 `mesh_attributes`

Full-array refresh of paint channels and layer settings without topology. Used by
Nomad for layer-factor changes and paint undo; accepted (as one undoable step) when
the peer advertises `mesh_attributes_receive`, `layers` configs matched to user
layers by index.

```jsonc
{
    "type": "mesh_attributes",
    "mesh_id": "6f9c…",
    "vertex_count": 8,                // must match, else error (§9)
    "binary_size": 56,
    "live_sync": true,

    "color_offset": 0,                // optional full per-vertex arrays,
    "color_format": "rgbm8",          // formats as §7.1
    "opacity_offset": 32,
    "opacity_format": "uint8_norm",
    "roughness_offset": 40,
    "roughness_format": "uint8_norm",
    "metalness_offset": 48,
    "metalness_format": "uint8_norm",

    "layers": [ /* §7.1 configs only, no offsets or paint */ ]
}
```

The paint arrays are the sender's composite. A receiver whose linked mesh has sculpt
layers applies the `layers` configs only and recomposites locally; the arrays are for
clients that track the composite (bridges).

## 8. Instances (`mesh_instance`)

Shared geometry is all-or-nothing on both ends (Nomad instances / e.g. Blender objects
sharing one mesh datablock). Every `mesh_full` names its geometry group via
`geometry_id`. Additional nodes of an already-transferred group travel as:

```json
{
    "type": "mesh_instance",
    "mesh_id": "...",
    "geometry_id": "...",
    "name": "...",
    "visible": true,
    "world_matrix": [...],
    "live_sync": ...,
    "request_id": "..."
}
```

Receiver: create a node/object sharing the geometry of the group; if the `mesh_id`
already exists but references other geometry, re-point it. If `geometry_id` is unknown,
answer `error` **and** `{"type": "request_mesh", "link_id": <mesh_id>}` — the peer then
sends that node as a forced `mesh_full`. A `mesh_full` arriving for a node whose group
gains a *different* `geometry_id` means the peer un-shared it (single-user); detach that
node from its group. A `mesh_full` with the *same* `geometry_id` but new topology
replaces the geometry for the entire group. Edits applied to shared geometry reach all
nodes of the group implicitly — never send per-sibling copies of the same delta.

## 9. Recovery discipline

The protocol favors "resend fully" over clever reconciliation. A bridge must:

- On any `error` from the peer: drop delta caches / instance-known state; the next
  geometry send is a `mesh_full`.
- On `{"type": "request_mesh", "link_id": ...}`: send that object as a forced
  `mesh_full` (never `mesh_instance`). Without `link_id`, `request_mesh` /
  `request_selection` mean "send your current selection"; `request_scene` all objects.
- If an incoming live edit cannot be applied right now (target busy/uneditable), mark
  the object stale, refuse to *send* geometry for it, and ask for a fresh `mesh_full`
  (targeted `request_mesh`) once it becomes writable. Never claim `auto` authorship
  while holding stale objects.
- `mesh_invalidated` and `request_active_mesh` are legacy message types; handle them as
  "refresh the link with a Get" and "send selection" respectively if received.

## 10. Scene objects

Values shown are defaults; map what you can and ignore the rest.

`object_state` — rename/move/hide without geometry:

```jsonc
{
    "type": "object_state",
    "link_id": "6f9c…",
    "name": "Sphere",
    "visible": true,
    "world_matrix": [ /* 16 floats, column-major */ ],
    "world_matrix_parent": [ /* … */ ],  // both only present when world_matrix
    "local_matrix": [ /* … */ ],         // has skew (§3); world = parent × local
    "smooth_shading": true,              // meshes only
    "live_sync": true
}
```

`object_delete`:

```jsonc
{ "type": "object_delete", "link_id": "6f9c…", "live_sync": true }
```

`material` — the same `material` object is embedded in `mesh_full` headers:

```jsonc
{
    "type": "material",
    "mesh_id": "6f9c…",
    "live_sync": true,
    "material": {
        "color": [1.0, 1.0, 1.0],             // linear RGB, [0, 1]
        "opacity": 1.0,                       // [0, 1]
        "roughness": 0.25,                    // [0, 1]
        "metalness": 0.0,                     // [0, 1]
        "material_type": "opaque",            // opaque | subsurface | blending | additive
                                              // | refraction | dithering | shadow_catcher
        "reflectance": 0.5,                   // [0, 1] specular, 0.5 = 4% F0
        "shadow_color": [0.0, 0.0, 0.0],      // linear RGB (shadow_catcher)
        "refraction_ior": 1.33,
        "refraction_surface_roughness": 0.0,  // [0, 1]
        "refraction_interior_roughness": 0.0, // [0, 1]
        "absorption_enable": false,           // refraction interior absorption
        "absorption_albedo": true,
        "absorption_factor": 1.0,
        "absorption_color": [1.0, 1.0, 1.0],  // linear RGB
        "subsurface_color": [1.0, 0.2, 0.1],  // linear RGB
        "subsurface_depth": 0.15,             // scene units; < 0 = auto
        "translucency": true,
        "translucency_factor": 1.0,
        "use_color_opacity_auto": true,       // color texture alpha drives opacity †
        "use_color_opacity_value": true,
        "wireframe_visible_auto": true,       // †
        "wireframe_visible_value": false,
        "two_sided_auto": true,               // †
        "two_sided_value": false,
        "backface_colored_auto": true,        // †
        "backface_colored_value": false,
        "cast_shadow_auto": true,             // †
        "cast_shadow_value": true,
        "receive_shadow_auto": true,          // †
        "receive_shadow_value": true,
        "pre_refract_auto": true,             // †
        "pre_refract_value": false,
        "always_unlit": false,
        "smooth_shading_auto": true,          // †
        "smooth_shading_value": true,
        "flip_culling": false,
        "textures": {                         // texture channels, pixels by blob id (§10.2).
                                              // A present channel is authoritative (no
                                              // texture_id = cleared); an absent channel —
                                              // or no "textures" object at all — keeps the
                                              // receiver's current assignment, so send only
                                              // the channels your application models
            "color": {
                "texture_id": "9c2e…",        // blob reference (§10.2)
                "name": "Sphere color.png",   // display name, basename with extension
                "projection": "uv",           // auto | uv | triplanar
                "wrap_s": "repeat",           // repeat | clamp | mirror
                "wrap_t": "repeat",
                "min_filter": "auto",         // auto | linear | nearest | linear_mipmap_linear | …
                "mag_filter": "auto",
                "offset": [0.0, 0.0],         // uv transform
                "scale": [1.0, 1.0],
                "rotation": 0.0,              // radians
                "triplanar_hardness": [0.9, 0.9, 0.9],
                "triplanar_world": true,
                "factor": [1.0, 1.0, 1.0]     // multiplies the sample; rgb on color and
                                              // emissive, scalar on the other channels.
                                              // color multiplies vertex paint on top, the
                                              // other channels replace it
            },
            "normal": { "texture_id": "…", "factor": 1.0, "neg_y": false },
            "emissive": { "texture_id": "…", "factor": [1.0, 1.0, 1.0], "strength": 1.0 },
            "roughness": {}                   // explicit clear. Channels: color, roughness,
                                              // metalness, normal, emissive, occlusion,
                                              // displacement, opacity
        }
    }
}
```

† auto-capable settings travel as a pair: `<name>_value` is the **resolved** state —
apply it directly; `<name>_auto` records whether it was an explicit choice or Nomad's
default. Senders set `<name>_value`; add `<name>_auto: true` only to hand the choice
back to the receiver's default.

Send only the fields your application edits: absent fields keep their current values.
The matcap channel is not part of the protocol. (`mesh_full` also carries a top-level
`smooth_shading` for receivers that skip the material block.)

`light`:

```jsonc
{
    "type": "light",
    // …object_state fields (link_id, name, visible, world_matrix, live_sync)…
    "light_type": "POINT",     // POINT | SUN | SPOT | AREA | ENVIRONMENT
    "color": [1.0, 1.0, 1.0],  // linear RGB
    "use_kelvin": false,       // true: kelvin replaces color outright (no tint on top)
    "kelvin": 6500,
    "intensity": 1.0,          // SUN strength (normalized)
    "power": 1.0,              // POINT/SPOT/AREA strength (world space)
    "factor": 1.0,             // ENVIRONMENT multiplier
    "spot_angle": 0.785,       // radians [0, π], full outer cone angle
    "spot_softness": 0.5,      // [0, 1] blend: inner = (1 - softness) × outer
    "angle": 0.0,              // radians [0, π], SUN angular size (softness)
    "size": 0.0,               // scene units, POINT/SPOT radius
    "attachment": "fixed",     // fixed | camera (the light follows the working view)
    "shadow_type": "shadow_map", // shadow_map | screen_space
    "shadow_cast": true,
    "shadow_tolerance": 0.0,
    "contact_shadow": false,
    "contact_tolerance": 0.0
}
```

As with `material`, send only what your application edits; Nomad-specific fields like
`attachment` or the shadow tuning may be ignored by other applications.

`camera_object`:

```jsonc
{
    "type": "camera_object",
    // …object_state fields…
    "orthographic": false,
    "fov_y": 50.0,             // degrees, vertical
    "pivot": [0.0, 0.0, 0.0]   // world orbit point; absent when the camera has none set
}
```

`camera` — the **working view** (not a scene object). Sent by the current source on
navigation; apply to your viewport. Coalesce: only the newest matters.

```jsonc
{
    "type": "camera",
    "world_from_view": [ /* 16 floats, column-major, view → world */ ],
    "pivot": [0.0, 0.0, 0.0],  // world orbit point
    "fov_y": 50.0,             // degrees, vertical
    "orthographic": false,
    "ortho_scale": 1.0,        // world height of the ortho frustum
    "coordinate_system": "nomad_y_up"
}
```

### 10.1 `display_config`

Postprocess and shading state, exchanged only between peers that both advertise the
`display_config` capability and gated by the `sync_display` channel:

```json
{"type": "display_config", "live_sync": true, "display": { ... }}
```

`display` carries Nomad's display settings with the same keys as its settings files:
shading (`shader_type`, `matcap_*`, `env_*`, `show_*`, `background_blur`,
`lights_enable`) and postprocess (`pp_*`). Sent by the current source on any change.
Between two Nomads this reproduces the full display state; other applications map
selectively (e.g. exposure, DOF) and ignore the rest.

A scene transfer (`request_scene` or an explicit send-all) also emits one
`display_config` with `"live_sync": false` — the one-shot form is accepted regardless
of the `sync_display` channel toggle, so a pulled scene arrives with its look.

### 10.2 Textures (`texture`, `request_texture`)

Texture pixels travel as blobs referenced from the material block (§10) by
`texture_id`. **An id names exact pixel content, immutably**: cache blobs by id for the
session and never re-request one you hold. Undo/redo of a bake on the sender only flips
the material back to ids every peer already cached — no pixels travel twice.

```jsonc
{
    "type": "texture",
    "texture_id": "9c2e…",
    "name": "Sphere color.png",   // basename with extension, display/file-name sugar
    "binary_size": 182734         // binary payload = the image file bytes as-is
                                  // (png/jpg…), no re-encode
}
```

- **Sender**: emit the blob once, before the first message referencing its id; after
  that reference freely. Skip the blob for peers not advertising `texture`.
- **Receiver**: on a material referencing an unknown id, keep the channel's current
  texture, send `{"type": "request_texture", "texture_id": "…"}` back to the sender,
  and finish the assignment when the blob arrives. This is also how late joiners
  catch up. On an unanswerable request the peer replies `error`.
- Blobs are cache fills, not edits: no `live_sync` gate, duplicates are ignored, and
  they are relayed to viewers like scene messages.
- Treat `name` as untrusted display data (basename only), never as a path.

## 11. Versioning

- `protocol` (int) — breaking changes only; mismatched peers must not talk.
- Fields and message types are added backwards-compatibly; ignore unknowns, gate new
  behavior behind `capabilities`.
- The reference client's `bridge_version` / `minimum_bridge_version` handshake drives
  its self-update; unrelated to third-party bridges.

## 12. Writing a new bridge: minimum checklists

**One-way viewer** (engine/renderer): framing, `hello` (+pairing wait), apply
`mesh_full`, `mesh_instance`, `object_state`, `object_delete`; optionally `mesh_delta`,
`material`, `light`, `camera`. Advertise only what you handle.

**Two-way editor**: additionally send `mesh_full` with your own `mesh_id`/`geometry_id`,
handle `mesh_ack`, `request_*`, `session_config`/`claim_sync`, implement the §9
recovery rules, and batch edits per completed user action (one undoable step each).

`blender/nomad_blender_link` demonstrates every part of this document, including the
sparse-delta caching, instance bookkeeping, and stale/recovery handling.
