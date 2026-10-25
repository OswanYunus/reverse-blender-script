# Reverse Blender Script

Select objects in Blender, hit **Ctrl+Shift+R**, and get a fully commented, runnable
Python script that reconstructs exactly what you selected — geometry, transforms (with
the math explained), modifiers, materials, and parenting. There's also an optional
**AI Enhance** button that sends the generated script to Claude for a comments/readability
pass only; it never touches the actual logic or numbers.

## Install

1. Download `reverse_blender_script.py`.
2. In Blender:
   - Blender 3.6 - 4.1: `Edit > Preferences > Add-ons > Install...`, pick the file, enable the checkbox next to "Reverse Blender Script".
   - Blender 4.2+: `Edit > Preferences > Get Extensions`, click the top-right menu arrow, select `Install from Disk...`, pick the file, and enable it.
3. (Optional, for AI Enhance only) Expand the add-on's preferences and paste your own
   Anthropic API key. Core script generation works with zero key and zero network calls.

## Use

- Select one or more objects → **Ctrl+Shift+R** (or the sidebar panel,
  `View3D > N-panel > Reverse Script` tab).
- A new text block named `ReverseScript_<timestamp>.py` opens in the Text Editor.
- "Generate (Whole Scene)" in the panel does the same for every object in the scene,
  not just the selection.
- Run the generated script anytime with Alt+P in the Text Editor, or headlessly:
  `blender --background --python ReverseScript_....py`

### AI Enhance

Click "AI Enhance Last Script" to send the most recently generated script to Claude.
It's instructed to only improve comments and section headers and flag anything that
looks lossy — never to change a bpy call or a number. If you don't set an API key,
this button just errors out cleanly; the generated script itself is unaffected.

The default model ID in the add-on preferences will eventually need updating — Anthropic
retires older dated model snapshots roughly once a year. If AI Enhance starts failing
with a "model not found" error, check
[platform.claude.com/docs/about-claude/models](https://platform.claude.com/docs/about-claude/models)
for the current model ID and update it in the add-on preferences.

## What gets captured (v0.1)

- Mesh geometry — raw vertex/face data via `from_pydata`, so it's a literal
  reconstruction, not a guessed primitive. Meshes over 20,000 vertices are written to a
  companion `.ply` file next to the `.blend` instead of inline Python literals, so the
  generated script stays small and fast to open even for dense/sculpted/scanned meshes
  (requires the `.blend` to have been saved at least once; otherwise large meshes stay
  inline and the operator warns about it).
- Per-face smooth/flat shading, so a mesh with mixed smooth-body/flat-hard-edge faces
  comes back exactly as it was, not forced uniformly one way.
- Linked duplicates / instances — objects that share the same underlying mesh data (a
  common pattern for foliage, props, and other repeated geometry) reuse that mesh
  datablock in the generated script instead of each re-emitting a full copy of the
  geometry.
- Transform — location, rotation (Euler *with* the radians/order noted, or quaternion
  if that's the object's rotation mode), scale.
- Modifiers — every settable property on each modifier in stack order, including the
  nested settings that Cloth, Soft Body, Collision, and Fluid modifiers store separately
  from the modifier itself (mass, stiffness, damping, friction, and so on). This restores
  the *setup*, not a baked simulation — you still need to play or bake the timeline in
  Blender afterward to get matching per-frame motion; no cached simulation data is
  replayed. Object/collection references inside a modifier (e.g. a Mirror modifier's
  mirror object) are captured as a name and left as a commented-out re-link line, since
  the referenced object may not exist in the target scene.
- Materials — Principled BSDF inputs (base color, metallic, roughness, specular, IOR,
  emission, alpha), or flat diffuse color for non-node materials.
- Parenting — resolved in a second pass after all objects exist, so child-before-parent
  ordering in the scene never breaks the output.

## What's not captured yet (extension points)

Geometry nodes, particle systems (hair, grass, and other particle-based scatter),
fluid/smoke/cloth simulation *caches* (the baked per-frame result — see above for what
physics modifiers do capture), drivers, shape keys, and animation F-curves aren't pulled
out in this version — the generated script notes this in its header. These aren't simple
additions: particle/hair systems and simulation caches use fundamentally different data
structures (point clouds, curve data, voxel caches) rather than the static per-object
properties this tool is built around, so supporting them well means new extraction logic,
not just extending the existing property walk. To add one: extend `extract_object()` in
`reverse_blender_script.py` to pull the relevant data, then add a matching
`_gen_..._block()` function and call it from `generate_script()`. The extraction/codegen
split is deliberate so each data type is a self-contained pair of functions.

Geometry that _can_ already be captured (static meshes, however dense) doesn't need
primitive-guessing to scale — see the companion-`.ply` behavior above. Deliberately not
implemented: detecting that a mesh "is probably a cube/sphere/cylinder" and re-emitting it
as a primitive operator call. That would mean guessing at authorial intent from geometry,
which runs against the one rule this tool is built on — every reconstruction is literal,
never inferred.

## Design notes

- Extraction and code generation are both pure, local, and deterministic — no AI
  involved. This is what makes the output trustworthy: the numbers in the script are
  read straight off the live Blender objects.
- The AI step is bolted on afterward, reads the already-correct script, and is
  explicitly told not to change values or remove calls — it's there for comments and
  structure, not for generating the reconstruction itself.
- Single-file add-on on purpose, so install is a one-step "Install..." with no zipping.
