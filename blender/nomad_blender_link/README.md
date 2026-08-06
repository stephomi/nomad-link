# Nomad Blender Link

Install this folder as a Blender extension ZIP. In Nomad, open the **Link** menu and enable the server; in Blender's **3D Viewport → Nomad** panel, find and connect to it, then accept the connection request shown in Nomad. The granted pairing token is stored in the extension preferences (with the host and port), so later connections are silent; either side can forget the pairing to require a new approval.

The extension supports:

- LAN discovery (UDP broadcast and Bonjour) and approval-based pairing;
- full triangle/quad mesh transfers in both directions;
- mesh instances both ways: Nomad instances become Blender objects sharing one mesh datablock and vice versa (all-or-nothing geometry sharing, matching both applications);
- Selection and Scene transfer scopes for meshes, lights, and camera objects;
- shared live scene channels for objects/geometry, materials, lights, and camera objects;
- one explicit live scene writer and one explicit working-view writer;
- full object transforms, with skew split across a generated parent Empty;
- UVs, visible vertex color/opacity/roughness/metalness, and material defaults;
- Nomad sculpt layers as Blender shape keys;
- face groups as the `nomad_face_group` face-domain integer attribute;
- sparse Nomad sculpt and vertex-paint updates after completed strokes and undo/redo, applied live even while the linked object is in Blender's Sculpt Mode;
- debounced Blender geometry and gizmo updates: sparse sculpt deltas while the topology is unchanged, full topology replacement otherwise;
- debounced layer-factor and visibility updates without retransmitting topology;
- exact perspective and orthographic working-view framing for the Blender viewport or scene camera.

The live settings are shared by both panels. **Live Sync** enables or pauses all live channels. One **Auto / Nomad → Blender / Blender → Nomad** source controls every enabled channel. **Auto** keeps the last application with meaningful pointer or keyboard activity as the source; passive pointer motion and received updates do not take ownership. The resolved Auto direction is shown in both panels. **Working View** is an independent channel checkbox, while Blender can use either the 3D viewport containing the Nomad panel or the scene camera.

Use **Send** or **Get** once after connecting to establish persistent link IDs for existing objects. Live sync then follows those linked objects and detects later additions and deletions. Live Nomad edits apply while the linked object is in Object Mode, Sculpt Mode, or any paint mode: sculpt sessions are refreshed in place, and topology changes hop through Object Mode and back automatically. Updates missed in Edit Mode, Dyntopo, or Multires sculpting pause that mesh; the extension automatically requests a fresh copy from Nomad once the object can be written again, and Auto will not send stale Blender geometry in the meantime.

Painting layers are flattened to their visible PBR result. Sculpt offset layers stay editable as shape keys. Nomad sends its active resolution and Blender sends its base mesh; Blender Multires modifiers and Nomad's multires hierarchy are not mirrored.

Linked meshes keep a persistent link ID. Topology-changing edits, remesh operations, and primitive edits replace the linked geometry instead of creating duplicates.

Nomad-generated materials occupy their own Blender material slot. A custom Blender shader is left alone if the generated material has been removed; enable material sync only when shader translation is wanted.

The wire protocol is versioned independently of Nomad, so ordinary Nomad releases do not require an extension update. If the package is installed from a Blender extension repository, a handshake can automatically install the required compatible version from that same repository. A ZIP-only installation has no remote update source and therefore keeps running until the protocol minimum changes.

## Extension repository

`server-generate` does not upload or serve anything. It scans a directory of extension ZIPs and writes an `index.json` beside them. Keep only the newest ZIP for each Blender compatibility range in that directory; store older ZIPs elsewhere, otherwise Blender may reinstall the first compatible archive instead of updating. On macOS, generate it with:

```sh
"/Applications/Blender.app/Contents/MacOS/Blender" --command extension server-generate --repo-dir="/path/to/blender"
```

For local testing, add `file:///path/to/blender/index.json` as a remote repository in Blender. For distribution, upload `index.json` and the ZIPs to the same public static web directory, then add its HTTPS `index.json` URL to Blender. Install the extension from that repository once so Blender remembers where updates come from.

The Blender extension source is licensed under GPL-3.0-or-later. The separate Nomad application communicates with it over a versioned socket protocol and is not part of the extension package.
