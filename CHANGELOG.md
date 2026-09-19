# Changelog

Every release publishes the section named after its version as the release notes.

## 0.11.45

- Blender: vertex groups survive a full mesh from Nomad. A layered mesh sends every stroke as
  a full mesh, and the rebuild used to drop the groups with the old geometry. Names always
  come back; weights come back when the vertex count is unchanged.
- No protocol change.

## 0.11.44

- Houdini: a copied *Nomad Link Out* node is a new object in Nomad. The copy used to inherit
  the original's mesh id, so sending from it replaced the original's mesh instead of adding
  one. Pasting now clears the id, and a copy made with an older build heals itself on send.
- No protocol change.

## 0.11.43

- Blender: a shape key value outside [0, 1] reaches Nomad on the layer's offset factor, where
  the slider is unbounded and signed. The raw value used to land on the main intensity, which
  composited it correctly but pinned its [0, 100%] slider display at 100%.
- Blender: the add-on welds UVs before sending — corners meeting at the same point share one
  texcoord. They used to arrive one texcoord per corner, so on the Nomad side every edge read
  as a UV seam and Face Groups built from UV islands came out one group per face.
- Lights travel under Nomad's own type names — `directional`, `point`, `spot`,
  `environment` — where they used to borrow Blender's `SUN`, `POINT`, `SPOT`,
  `ENVIRONMENT`. **Update the bridges along with Nomad**: to an older bridge every light now
  reads as a point light, and an older Nomad reads the new names the same way. The Blender
  add-on and the Toolbag plugin in this release speak the new names.
- Primitives stay primitives between two Nomads: a sphere, tube, or lathe arrives on the
  peer still parametric, config and all, instead of frozen into a mesh. Bridges see no
  difference — the arrays remain the object, so anything that ignores the new block gets
  what it always got.
- Validating a primitive no longer strands it on the peer. The peer kept holding a primitive,
  which cannot take sparse edits, so it refused every stroke after the validate and the
  object stopped updating for the rest of the session.
- A mesh shows the paint it receives rather than ignoring it: a channel that arrives with
  per-vertex color, roughness, metalness, or opacity turns that channel on. A validated
  primitive used to render in its flat material color on the peer while sitting on the paint
  it had been sent.
- Repeaters (array, curve, mirror, radial) travel with their config, so a Nomad peer rebuilds
  the copies itself instead of receiving them one by one. Bridges keep receiving the copies as
  ordinary objects.
- Protocol: `light_type` renamed (breaking), and two new blocks — `primitive` (§7.1.2) and
  `repeater` (§10.4).

## 0.11.42

- Blender: Edit Mode changes reach Nomad while you make them. Blender keeps those edits in its
  own mesh until the mode ends, so until now they only travelled once you went back to Object
  Mode.
- Blender: an add-on that keeps a modal operator open for the whole session no longer freezes
  the link. Updates are only held while a sculpt or paint brush stroke is in flight, the one
  case where applying them could crash Blender.
- Blender: the panel says which mesh paused and why — Edit Mode, Dyntopo or Multires — and
  drops the message once it has caught up.
- No protocol change.

## 0.11.41

- Toolbag: **Follow Nomad's view** is Nomad's shared Working View setting rather than a local
  one, so ticking it in the plugin turns view sync on for the session, and a change made in
  Nomad or another bridge shows up in the checkbox.
- Toolbag: the transfer is faster.
- A flat material color travels as vertex data: primitives were white in Blender's Shading tab.
- No protocol change.

## 0.11.40

- Blender: the connection no longer stalls on "Waiting for Nomad" when another add-on keeps a
  modal operator running. Session packets are applied while one is in flight; scene packets are
  still held until it ends, and the panel shows how many are waiting.
- No protocol change.

## 0.11.39

- Toolbag: color and emissive maps are read as sRGB, every other channel as raw data. The
  color space is set on the texture, not on the slot, so a map never speaks for the vertex
  colors sharing that slot.
- Toolbag: per-channel texture factors, displacement maps, normal-map Y flip, and
  subsurface color. Clearing a channel removes its map instead of leaving the old one.
- No protocol change.

## 0.11.38

- Marmoset Toolbag plugin (`toolbag`, `nomad-link-toolbag.zip`): receives the scene to
  render and bake.
- Houdini digital assets (`houdini`, `nomad-link-houdini.zip`): a Nomad Link In SOP and a
  Nomad Link Out SOP.
- `display_config` is replaced by `shading_config` and `postprocess_config`, one
  capability and one live channel (`sync_shading`, `sync_postprocess`) each. Every setting
  is now listed in §10.1; the old message is gone, with no fallback.
- Assets (§10.3): matcaps and environments travel as `asset` blobs keyed by a hash of the
  file bytes, requested with `request_asset`.
- Procedural copies (§8, §10): `repeat` instances and `repeat` groups, owned by their
  sender. A `mesh_full` naming an unknown `mesh_id` with a known `geometry_id` applies to
  that geometry's owner instead of creating an object.
- Enumerated values travel as strings, never integers — paint blend modes, tone mapping,
  texture filters, curve presets. Every accepted value is listed with its key.
- `mesh_delta` layer strokes send `layer_<channel>_offset` plus its alpha section, never
  alongside `base_*`. A receiver with sculpt layers recomposites `mesh_attributes` paint
  instead of writing the sender's composite into its base.
- JSON frames may be up to 50 MiB. `AREA` is not a `light_type`.

## 0.11.37

- Hierarchy (§3, §10): `parent_id`, `child_index`, `group` nodes, and the `hierarchy`
  capability. An absent `parent_id` leaves the receiver's parenting untouched, so a peer
  that does not model hierarchy never flattens a tree. An unknown `parent_id` keeps the
  node at the root until the parent arrives.
- `scene_batch` (§10): one frame of scene-graph edits applied in order as a single
  undoable step, gated by the `scene_batch` capability.
- `skew` capability: a skewed root sent to a peer without it arrives wrapped in a
  synthetic `<link_id>/skew` group.
- `object_state` carries `visible` and `locked`. `object_delete` removes the node and its
  children — re-parent them first to keep them.
- ZBrush bridge updates (`examples/zbrush.py`).

## 0.11.36

- Hidden faces: `face_hidden_offset` / `face_hidden_format` in `mesh_full`, absent = all
  visible. Face-group ids are limited to 32767.
- Blender extension: face groups mirror to `.sculpt_face_set`, hidden faces to
  `.hide_poly`, and the sculpt mask round trips (inverted, Nomad stores 1 = unmasked).

## 0.11.35

- N-gons: `face_format: "corners"` with the `ngon` capability (§7.1.1). Nomad accepts them
  and splits each into tris/quads on arrival, so a round trip returns `int32x4`.
- Blender extension advertises `ngon` and sends its n-gons unsplit.

## 0.11.34

- First public release: protocol 1, `PROTOCOL.md`, the Python examples, and the Blender
  extension repository.
