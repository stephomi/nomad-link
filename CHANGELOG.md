# Changelog

Every release publishes the section named after its version as the release notes.

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
