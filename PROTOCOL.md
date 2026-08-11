# Nomad Link Protocol

Socket protocol for two-way scene synchronization with Nomad Sculpt.

- Nomad is the server. Bridges are clients.
- Up to 8 clients can connect. One client sends live edits (§5).
- `blender/nomad_blender_link` is the reference client (GPL-3.0).
- `examples/` contains standalone Python bridges (MIT).
- Protocol version: **1** (integer; changes only on breaking framing/handshake changes)
- Default TCP port: **48312**

## 1. Transport and framing

TCP. Each packet is one frame:

| bytes | content |
|---|---|
| 4 | JSON payload size, unsigned 32-bit **big-endian** |
| 4 | binary payload size, unsigned 32-bit **big-endian** |
| n | JSON object, UTF-8 |
| m | binary payload (may be empty) |

Limits: JSON ≤ 1 MiB; binary ≤ 1 GiB. Disconnect on larger frames.

JSON payloads are objects with a `"type"` string. Ignore unknown types and fields.
Offsets are byte offsets into the binary payload. When binary data is present,
`"binary_size"` must match its size.

### WebSocket

Browser builds use `ws://host:port/`. Each binary WebSocket message contains one frame
in the format above. The rest of the protocol is unchanged.

The first `hello` sets the browser's role:

- A Nomad host replies to the browser's join `hello` (`nomad_version` is present). The
  browser remains a client.
- A client listener sends its own `hello`. The browser becomes the host.

Nomad accepts TCP and WebSocket connections on the same port.

## 2. Discovery (optional)

A bridge can use either method or accept an address from the user.

- **UDP broadcast**: send the ASCII datagram `NOMAD_LINK_DISCOVER 1` to
  `255.255.255.255:<port>`. Nomad replies to the sender with JSON:
  `{"type": "nomad_link", "name": "Nomad Sculpt", "protocol": 1, "port": 48312}`.
  Use the reply's source IP as the host.
- **Bonjour/mDNS**: Nomad advertises `_nomadlink._tcp` on Apple platforms. The SRV
  record contains the port; use the responder's IP. Prefer this on iPad, where the OS
  restricts UDP broadcast reception.

## 3. Conventions

- **Coordinate system**: right-handed, **Y up** (glTF convention). Headers carry
  `"coordinate_system": "nomad_y_up"` where relevant. Units are arbitrary scene units.
- **Matrices**: 16 floats, **column-major** (`world_matrix[column*4 + row]`).
- **Mesh transforms**: vertex positions are in node-local space; `world_matrix` places
  the node. For skewed transforms, Nomad may also send `world_matrix_parent` and
  `local_matrix` (world = parent × local; both are skew-free). Use the split if needed,
  otherwise use `world_matrix`.
- **Hierarchy**: `parent_id` holds the parent's `link_id`; `""` is the scene root. An
  **absent** `parent_id` leaves the receiver's parenting untouched, so messages from a peer
  that does not model hierarchy never flatten a tree. When it is present,
  `world_matrix_parent` is that parent's world matrix and `local_matrix` is relative to it;
  prefer the pair, `world_matrix` stays the flattened value for peers without hierarchy.
  When it is absent, the pair is the skew split above. A `local_matrix` that is itself
  skewed splits again, and the extra frame belongs between the parent and the node.
  To a peer without the `skew` capability, a skewed **root** object is sent under a
  synthetic `group` whose id is `<link_id>/skew` and whose `world_matrix` is the skew
  frame — a lone object cannot hold skew in every application. The synthetic group is
  wire-only bookkeeping: a peer that can represent skew treats a `/skew` `parent_id` as
  the root, ignores `group`/`object_state`/`object_delete` about `/skew` ids, and trusts
  the child's own messages.
- **Sibling order**: `child_index` is advisory. Peers that order siblings apply it, peers
  that do not ignore it. It never affects transforms.
- **Lights and cameras** aim along their local **-Z** axis with +Y up (glTF convention).
  Convert their `world_matrix` directly between coordinate systems. Do not convert it
  like a mesh transform.
- **Faces**: `int32x4` by default — triangles and quads only, a triangle sets the 4th
  index to -1. Peers advertising `ngon` also accept `face_format: "corners"`, which
  carries any face size (§7.1). Nomad currently splits n-gons into tris/quads on
  arrival, so a mesh sent as `corners` comes back as `int32x4`.
- **Ids**: `mesh_id` / `link_id` / `geometry_id` are opaque strings chosen by whichever
  side names the entity first. UUIDs are recommended. Keep them for the life of the
  link.

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

- Wrong `protocol`: `error`, then disconnect.
- Known `pair_token`: Nomad replies with `hello`.
- Unknown or empty token: Nomad replies with `{"type": "pairing_pending"}`. Keep the
  connection open while the user accepts it in Nomad. Nomad then sends `hello`, or
  `error` and disconnects if refused.

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
The version fields are used by the reference client's updater; other bridges may ignore
them. Use `{"type": "ping"}` / `{"type": "pong"}` as a keepalive. An `error` can arrive
at any time; see §9.

### Capabilities

Both sides send capabilities. Use only capabilities advertised by the peer.

| capability | meaning when advertised |
|---|---|
| `selection_transfer` / `scene_transfer` | peer answers `request_selection` / `request_scene` |
| `scene_edits`, `object_state`, `material`, `light`, `camera_object` | peer understands those live messages |
| `session_config` | peer supports shared config (§5) |
| `mesh_full`, `mesh_delta`, `mesh_attributes`, `sculpt_layers` | Nomad → bridge data kinds |
| `camera` | advertiser sends working-view messages (§10); Nomad shows its view-sync toggle only while a peer advertises this |
| `mesh_delta_receive` | advertiser accepts incoming sparse `mesh_delta`; without it send `mesh_full` |
| `mesh_attributes_receive` | advertiser accepts incoming `mesh_attributes` |
| `mesh_instance` | peer understands shared-geometry instances (§8) |
| `hierarchy` | peer applies `parent_id` / `child_index` and understands `group` (§10) |
| `scene_batch` | peer applies a `scene_batch` (§10) as one undoable step |
| `skew` | peer represents skewed matrices directly; without it, skewed roots arrive wrapped (§3) |
| `ngon` | peer accepts `face_format: "corners"` (§7.1.1); without it, split n-gons before sending |
| `display_config` | peer applies display settings (§10.1); Nomad only sends them to peers advertising this |
| `texture` | peer caches texture blobs by immutable id (§10.2); Nomad only sends blobs to peers advertising this |

A minimal viewer needs only `hello`, `mesh_full`, and `object_state`.

## 5. Session configuration

Nomad owns the live-sync settings and sends revisions to clients.

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
  If the revision is stale, Nomad returns the current config without applying the
  change.
- In `auto` mode, a client sends `{"type": "claim_sync", "source": "client"}` after
  user activity. User activity in Nomad sets the source back to Nomad. Only
  `active_source` sends live edits. Do not claim while local objects are stale (§9).
- With multiple clients, one is the editor and the rest receive Nomad's relay. A
  `claim_sync` promotes that client to editor. Any client can change session settings;
  doing so does not make it the editor.
- A Nomad instance can join another Nomad as a client. It uses the same messages and
  treats the host's `session_config` as current, with `nomad` and `client` interpreted
  from its side of the connection.

("client" always means the connected bridge, whatever the application is.)

## 6. Live flag and channels

Apply `"live_sync": true` messages only when live sync is enabled, the sender is
`active_source`, and the matching channel is enabled. Otherwise drop them.

Always apply explicit transfers (`"live_sync": false`). Echo request `request_id`
values in acks and errors.

## 7. Mesh transfer

### 7.1 `mesh_full`

Complete mesh state in one frame.

- `*_offset` is a byte offset into the binary payload and has a matching `*_format`.
- ⭐ fields are required. Other fields are optional.
- The example is a cube with 8 vertices, 6 quads, 14 UVs, and one layer. Data is
  packed without alignment.

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
    "parent_id": "1b7e…",                 // hierarchy (§3); travels with child_index and
    "child_index": 2,                     //   the matrix pair, exactly as in object_state
    "smooth_shading": true,               // ⭐
    "visible": true,                      // absent = leave as-is (new objects visible)
    "locked": false,                      // object lock (read-only content), absent = leave as-is
    "live_sync": false,                   // ⭐ §6
    "request_id": "…",                    // echoed by mesh_ack / error
    "replace_topology": true,             // allow replacing topology/UVs/layers of the
                                          // linked mesh, else the receiver errors (§9);
                                          // always true on Nomad's outgoing fulls

    "position_offset": 0,                 // ⭐ node-local positions, 8 × 12 B
    "position_format": "float32x3",
    "face_offset": 96,                    // ⭐ tris + quads; triangle: 4th index -1
    "face_format": "int32x4",             //   6 × 16 B, n-gons use "corners" (§7.1.1)

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
    "face_group_format": "uint16",        // 6 × 2 B, id ≤ 32767
    "face_hidden_offset": 492,            // per face, 0 visible / 1 hidden,
    "face_hidden_format": "uint8",        // 6 × 1 B, absent = all visible
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

Reply with `{"type": "mesh_ack", "mesh_id": ..., "request_id": ...}`. The sender uses
the returned `mesh_id` as the object's link id.

### 7.1.1 `face_format: "corners"` — n-gons

Only for peers advertising `ngon`; otherwise split the n-gons and send `int32x4`.
The four fields replace `face_offset` / `face_uv_offset`, everything else is unchanged —
`face_count` still indexes `face_group_offset`, and the corners of face `i` are the
`face_size[i]` entries starting at the sum of the preceding sizes.

```jsonc
{
    "face_format": "corners",
    "face_count": 6,                      // as usual
    "corner_count": 26,                   // = Σ face sizes
    "face_size_offset": 96,               // int32 per face, >= 3, 6 × 4 B
    "corner_vertex_offset": 120,          // int32 per corner, 26 × 4 B
    "corner_texcoord_offset": 224         // int32 per corner, required with texcoords
}
```

Nomad accepts this format but has no n-gons of its own yet: each is split into
tris/quads on arrival (face groups follow the split), so a round trip returns
`int32x4`. Peers that keep the topology should not re-send what they receive.

### 7.2 `mesh_delta` — sparse updates (both directions)

Send one delta per completed sculpt or paint stroke. It contains only touched vertices
and becomes one undo step on the receiver.

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

Send deltas only when both sides have identical topology. Otherwise send `mesh_full`.
Nomad rejects deltas for procedural primitives (`error`; §9).

For meshes with sculpt layers, base strokes use `base_*`; layer strokes use
`layer_index` and `layer_*`. Clients without layer support use the plain composite
paint sections.

### 7.3 `mesh_attributes`

Refreshes paint arrays and layer settings without topology. It is one undoable step.
The receiver must advertise `mesh_attributes_receive`. Match `layers` by index.

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

Paint arrays contain the sender's composite. A receiver with sculpt layers applies
only the `layers` settings and recomposites locally.

## 8. Instances (`mesh_instance`)

Each `mesh_full` identifies its shared geometry with `geometry_id`. After the geometry
has been sent, send its other instances as:

```json
{
    "type": "mesh_instance",
    "mesh_id": "...",
    "geometry_id": "...",
    "name": "...",
    "visible": true,
    "locked": false,
    "world_matrix": [...],
    "parent_id": "...",
    "child_index": 2,
    "live_sync": ...,
    "request_id": "..."
}
```

The receiver creates an object that shares the group's geometry. If its `mesh_id`
already uses other geometry, reassign it.

If `geometry_id` is unknown, send `error` and
`{"type": "request_mesh", "link_id": <mesh_id>}`. The peer must reply with
`mesh_full`, not `mesh_instance`.

If an existing node receives a different `geometry_id` in `mesh_full`, detach it from
its old group. If it receives new topology with the same `geometry_id`, replace the
geometry for the whole group. Send one delta per shared geometry, not per instance.

Instances share geometry, not hierarchy. There is no instanced subtree: a peer that
instances a whole branch sends every copy as its own nodes.

## 9. Recovery

- After any `error`, clear delta and known-instance caches. Send `mesh_full` next.
- For `{"type": "request_mesh", "link_id": ...}`, send that object as `mesh_full`,
  never `mesh_instance`.
- Without `link_id`, `request_mesh` and `request_selection` request the current
  selection. `request_scene` requests all objects.
- If a live edit cannot be applied, mark the object stale and do not send its geometry.
  When it becomes writable, request a targeted `mesh_full`. Do not claim sync while any
  local object is stale.
- An unknown `parent_id` is not an error: keep the node at the root with its
  `world_matrix` and re-parent it when the parent arrives. Never drop it.
- Legacy `mesh_invalidated` requests a refresh. Legacy `request_active_mesh` requests
  the current selection.

## 10. Scene objects

Values shown are defaults. Map supported fields and ignore the rest.

`object_state` — rename/move/hide without geometry:

```jsonc
{
    "type": "object_state",
    "link_id": "6f9c…",
    "name": "Sphere",
    "visible": true,
    "locked": false,                     // object lock (read-only content)
    "parent_id": "1b7e…",                // §3; "" = root, absent = leave parenting alone
    "child_index": 2,                    // §3, advisory sibling order
    "world_matrix": [ /* 16 floats, column-major */ ],
    "world_matrix_parent": [ /* … */ ],  // the parent's world when parent_id is set,
    "local_matrix": [ /* … */ ],         // else the skew split; world = parent × local
    "smooth_shading": true,              // meshes only
    "live_sync": true
}
```

`group` — a transform-only node (Nomad group, Blender empty). It carries no geometry and
needs no ack. Peers without `hierarchy` ignore it and keep every object at the root:

```jsonc
{
    "type": "group",
    // …object_state fields: link_id, name, visible, parent_id, child_index, matrices, live_sync…
}
```

`object_delete` — removes the node **and its children**. To keep the children, re-parent
them first, in the same `scene_batch` when the peer supports it:

```jsonc
{ "type": "object_delete", "link_id": "6f9c…", "live_sync": true }
```

`scene_batch` — one frame applied in array order as a single undoable step. Scene-graph
edits touch many nodes at once, and the order inside the batch is what makes them safe:
a re-parent must land before the delete that orphans it.

```jsonc
{
    "type": "scene_batch",
    "live_sync": true,
    "messages": [ /* object_state | group | object_delete | material | light | camera_object */ ]
}
```

- Entries never carry binary, so `mesh_full` and `mesh_delta` stay outside a batch.
- Validate the whole batch first; reject it entirely if an entry would parent a node under
  its own descendant. Apply nothing on rejection and reply with `error`.
- Split a batch that would exceed the JSON limit; each part is its own step.
- Send the entries individually to peers without `scene_batch`, re-parents first.

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

† Auto settings use two fields. Apply `<name>_value`. `<name>_auto` records whether the
value comes from the receiver's default. Send `<name>_auto: true` only to restore that
default.

Send only edited fields. Absent fields are unchanged. The matcap channel is not
supported. `mesh_full` also has top-level `smooth_shading`.

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

Send only edited fields. Other applications may ignore Nomad-specific settings such as
`attachment` and shadow tuning.

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

`camera` is the working view, not a scene object. Apply the newest message to the
viewport and discard older pending messages.

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

Postprocess and shading settings. Both peers must advertise `display_config`. Live
messages also require the `sync_display` channel.

```json
{"type": "display_config", "live_sync": true, "display": { ... }}
```

`display` uses the keys from Nomad settings files: shading (`shader_type`, `matcap_*`,
`env_*`, `show_*`, `background_blur`, `lights_enable`) and postprocess (`pp_*`). Other
applications may map only supported settings.

A scene transfer also sends one `display_config` with `"live_sync": false`. Apply it
even when `sync_display` is off.

### 10.2 Textures (`texture`, `request_texture`)

Material blocks reference texture blobs by `texture_id`. An id always refers to the
same bytes. Cache blobs for the session and do not request an id already cached.

```jsonc
{
    "type": "texture",
    "texture_id": "9c2e…",
    "name": "Sphere color.png",   // basename with extension, display/file-name sugar
    "binary_size": 182734         // binary payload = the image file bytes as-is
                                  // (png/jpg…), no re-encode
}
```

- **Sender**: send the blob before its first reference. Do not send it again. Do not
  send blobs to peers without the `texture` capability.
- **Receiver**: for an unknown id, keep the current texture and send
  `{"type": "request_texture", "texture_id": "…"}`. Assign it when the blob arrives.
  Reply with `error` if an id cannot be provided.
- Texture blobs are not live edits. They are not gated by `live_sync`; ignore
  duplicates and relay them to viewers.
- Treat `name` as untrusted display data (basename only), never as a path.

## 11. Versioning

- `protocol` (int): breaking changes only. Disconnect on mismatch.
- Ignore unknown fields and message types. Use capabilities for new behavior.
- `bridge_version` and `minimum_bridge_version` are for the reference client's updater.

## 12. Minimum implementation

**Viewer**: framing, `hello` and pairing, apply
`mesh_full`, `mesh_instance`, `object_state`, `object_delete`; optionally `mesh_delta`,
`material`, `light`, `camera`. Advertise only what you handle. Hierarchy is optional:
without it, place every object by `world_matrix` and ignore `parent_id` and `group`.

**Editor**: also send `mesh_full` with your own `mesh_id` and `geometry_id`,
handle `mesh_ack`, `request_*`, `session_config`/`claim_sync`, follow §9, and send one
edit per completed user action.

See `blender/nomad_blender_link` for a complete implementation.
