# Two protocol additions: hierarchy and visibility

> **Answered in 0.11.37.** Both landed, in the shape this asked for: `parent_id`
> holding the parent's `link_id`, with `world_matrix` still carrying the flattened
> value for peers that ignore it, and `visible` on `mesh_full`. Nomad went further
> than proposed, adding `group` nodes, `child_index`, `scene_batch`, `locked` and a
> `skew` capability. Kept for the record; nothing here is outstanding.

Written while building the Houdini bridge. Both are additive and backwards
compatible (§11 says ignore unknown fields), and the Houdini client already
implements the receiving side of both, so they light up the moment Nomad sends
them.

## 1. `parent_id` — scene hierarchy

**What is missing.** Protocol 1 carries no parent-child relationship. Every
object arrives with a `world_matrix` and nothing that says who it belongs to.
`world_matrix_parent` exists (§3) but is a *matrix* for splitting skewed
transforms, not the identity of a parent object.

**Why it matters.** Nomad's outliner is a tree, and users organise real scenes
with it. A 400-object scene arrives in Houdini as 400 siblings in one flat
namespace, with no way to select "the character" or "the props". It also
matters more in USD than in Blender: Solaris addresses everything by prim path,
so hierarchy is how you write rules, apply variants, or override a branch.
Rebuilding it by hand for every transfer is not practical.

**Nomad already has this.** Exporting the same scene as glTF or USD preserves
the hierarchy, so the relationships exist internally and are already serialised
by two other code paths — Link is the only one that drops them. That is
understandable: Link carries live *edits*, and a world matrix places an object
correctly whether or not the receiver knows its parent. Hierarchy only starts
to matter when the receiver represents the scene rather than just drawing it.
It also suggests the sending side is small, since glTF's node hierarchy needs
the same parent-and-local-transform information this asks for.

**Proposal.** An optional `parent_id` on the object messages — `mesh_full`,
`mesh_instance`, `object_state`, `light`, `camera_object`:

```jsonc
{
    "type": "mesh_full",
    "mesh_id": "6f9c…",
    "parent_id": "a41d…",   // link_id of the parent object; absent or "" = scene root
    // …
}
```

- The value is an existing `link_id` / `mesh_id`, so it needs no new id space.
- `world_matrix` stays as it is. A receiver that nests objects derives the local
  transform itself (`inverse(parent world) × child world`), which is what the
  Houdini client does; a receiver that ignores `parent_id` is unaffected,
  because the world matrix already places the object correctly.
- A parent that has not arrived yet is treated as root, so ordering within a
  scene transfer does not matter.
- Reparenting is then just an `object_state` carrying a new `parent_id`.

Advertising a `hierarchy` capability would let Nomad skip the field for peers
that do not want it, though at one string per object the cost is negligible.

## 2. Visibility on transfer

**What is missing.** `visible` appears on `object_state` (§10) and
`mesh_instance` (§8), but not on `mesh_full` (§7.1). During a scene transfer a
hidden object therefore arrives with no indication that it is hidden, and shows
up visible in the receiving application.

**Proposal.** Either
add `visible` to the `mesh_full` header:

```jsonc
{ "type": "mesh_full", "visible": false, /* … */ }
```

or state in §7.1 that a scene transfer also sends an `object_state` for any
object whose state differs from the defaults. The first is simpler and matches
how `mesh_instance` already works.

The Houdini client honours `visible` on `mesh_full` today, so either fix works
without a client change.

## Also worth knowing

Two things the Houdini bridge hit that are not requests, just data points:

- **`sync_lights` and `sync_materials` default to off.** Light and material
  edits do not stream until a client sets them, which reads as "lights are
  broken" until you find the flags. Defaulting them on, or surfacing them in
  Nomad's Link menu next to the live-sync toggle, would save that confusion.
- **Instanced scenes are large on the wire.** A scene of ~400 objects sharing
  ~12 geometries is handled well by `mesh_instance` (§8) — this is a note that
  the mechanism is being used and works, not a complaint.
