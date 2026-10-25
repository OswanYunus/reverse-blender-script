bl_info = {
    "name": "Reverse Blender Script",
    "author": "Oswan Yunus",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Reverse Script  |  Ctrl+Shift+R",
    "description": (
        "Reverse-engineers the selected objects (or the whole scene) into an "
        "executable, heavily-commented Python script. Optional AI Enhance "
        "pass adds clearer comments via the Claude API (your own key)."
    ),
    "category": "Development",
}

import bpy
import json
import os
import time
import urllib.request
import urllib.error

# Meshes with more vertices than this get written to a companion .ply file
# instead of inline Python list literals - keeps the generated script small
# and fast to open even when the scene has dense/sculpted/scanned geometry.
LARGE_MESH_VERTEX_THRESHOLD = 20_000

# Above this many TOTAL vertices across the generated selection, the operator
# asks for confirmation before generating (purely a "this may take a moment
# and write companion files" heads-up, not a hard limit).
CONFIRM_VERTEX_THRESHOLD = 2_000_000

# =========================================================================
# 1. EXTRACTION
#    Walks live bpy objects and pulls out plain-Python data: nothing here
#    talks to the AI, and nothing here is lossy on purpose. If a value
#    can't be captured cleanly, it's skipped rather than guessed.
# =========================================================================

# Modifier properties we never want in generated code (internal/UI state).
_MOD_PROP_SKIP = {
    "rna_type", "name", "type", "show_viewport", "show_render",
    "show_in_editmode", "show_on_cage", "show_expanded", "is_active",
    "is_override_data",
}


def _extract_rna_props(struct_obj, skip=frozenset()):
    """{prop_name: value} for any RNA struct (a modifier, or a nested settings
    block like a Cloth modifier's .settings), skipping read-only/internal
    props, and reducing object/collection references down to their name (so
    the generated script can re-look them up with bpy.data...)."""
    data = {}
    for prop in struct_obj.bl_rna.properties:
        pid = prop.identifier
        if prop.is_readonly or pid in skip:
            continue
        try:
            value = getattr(struct_obj, pid)
        except Exception:
            continue

        if isinstance(value, (int, float, bool, str)):
            data[pid] = value
        elif value is None:
            continue
        elif hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
            try:
                data[pid] = tuple(
                    round(v, 6) if isinstance(v, float) else v for v in value
                )
            except TypeError:
                continue
        else:
            # Likely an ID reference (object/collection/material) - capture its name.
            name = getattr(value, "name", None)
            if name is not None:
                data[pid] = ("__ID_REF__", name)
    return data


# Physics-type modifiers keep their real parameters in a NESTED settings
# struct (e.g. a Cloth modifier's mass/stiffness/damping live on
# modifier.settings, not on the modifier itself). A flat property walk over
# the modifier alone silently misses all of it - these are walked one level
# deeper, explicitly, rather than guessed at generically.
_NESTED_SETTINGS_PROPS = {
    'CLOTH': ('settings', 'collision_settings'),
    'SOFT_BODY': ('settings',),
    'COLLISION': ('settings',),
    'FLUID': ('domain_settings', 'flow_settings', 'effector_settings'),
}


def _clean_modifier_props(mod):
    data = _extract_rna_props(mod, skip=_MOD_PROP_SKIP)
    for nested_name in _NESTED_SETTINGS_PROPS.get(mod.type, ()):
        nested_obj = getattr(mod, nested_name, None)
        if nested_obj is None:
            continue
        nested_data = _extract_rna_props(nested_obj, skip={"rna_type", "name"})
        if nested_data:
            data[f"__NESTED__{nested_name}"] = nested_data
    return data


def _extract_material(mat):
    out = {"name": mat.name, "use_nodes": mat.use_nodes}
    if mat.use_nodes and mat.node_tree:
        bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf:
            wanted = (
                "Base Color", "Metallic", "Roughness", "Specular",
                "Specular IOR Level", "IOR", "Emission Color",
                "Emission Strength", "Alpha",
            )
            inputs = {}
            for key in wanted:
                if key in bsdf.inputs:
                    try:
                        v = bsdf.inputs[key].default_value
                        inputs[key] = (
                            tuple(round(x, 6) for x in v) if hasattr(v, "__len__")
                            else round(v, 6)
                        )
                    except Exception:
                        pass
            out["principled_inputs"] = inputs
    else:
        out["diffuse_color"] = tuple(round(c, 6) for c in mat.diffuse_color)
    return out


def extract_object(obj):
    data = {
        "name": obj.name,
        "type": obj.type,
        "parent": obj.parent.name if obj.parent else None,
        "location": tuple(round(v, 6) for v in obj.location),
        "rotation_mode": obj.rotation_mode,
        "rotation_euler": tuple(round(v, 6) for v in obj.rotation_euler),
        "rotation_quaternion": tuple(round(v, 6) for v in obj.rotation_quaternion),
        "scale": tuple(round(v, 6) for v in obj.scale),
        "modifiers": [],
        "materials": [],
        "mesh": None,
    }

    for mod in obj.modifiers:
        data["modifiers"].append({
            "name": mod.name,
            "type": mod.type,
            "props": _clean_modifier_props(mod),
        })

    for slot in obj.material_slots:
        if slot.material:
            data["materials"].append(_extract_material(slot.material))

    if obj.type == 'MESH':
        mesh = obj.data
        data["mesh"] = {
            "mesh_name": mesh.name,
            "vertices": [tuple(round(c, 6) for c in v.co) for v in mesh.vertices],
            "faces": [tuple(p.vertices) for p in mesh.polygons],
            "use_smooth": [p.use_smooth for p in mesh.polygons],
        }

    return data


def extract_scene(objects):
    return [extract_object(o) for o in objects]


# =========================================================================
# 2. CODE GENERATION
#    Turns the extracted data into a runnable, commented bpy script.
#    Every numeric block gets a short note on what the numbers mean -
#    this is the "math" the tool is meant to surface.
# =========================================================================

def _fmt(value):
    return repr(value)


def _safe_var(name, prefix=""):
    return prefix + "".join(c if c.isalnum() else "_" for c in name)


def _write_ply_ascii(filepath, vertices, faces):
    """Writes geometry as a plain ASCII PLY file by hand (no bpy.ops export
    operator involved) - sidesteps the fact that the ply import/export
    operator names changed between Blender 3.x (import_mesh.ply /
    export_mesh.ply) and 4.x (wm.ply_import / wm.ply_export)."""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("ply\nformat ascii 1.0\n")
        f.write(f"element vertex {len(vertices)}\n")
        f.write("property float x\nproperty float y\nproperty float z\n")
        f.write(f"element face {len(faces)}\n")
        f.write("property list uchar int vertex_indices\nend_header\n")
        for v in vertices:
            f.write(f"{v[0]} {v[1]} {v[2]}\n")
        for face in faces:
            f.write(f"{len(face)} " + " ".join(str(i) for i in face) + "\n")


def _gen_mesh_geometry_block(obj_data, var, mesh_dir):
    """Produces code that ends with a `{var}_mesh` bpy mesh datablock existing.
    Small meshes: inline Python list literals (as before). Meshes over
    LARGE_MESH_VERTEX_THRESHOLD, when mesh_dir is available: geometry is
    written to a companion .ply file instead, written now as a side effect,
    and the generated script imports it - keeps the .py text small and fast
    to open even for dense/sculpted/scanned meshes."""
    mesh = obj_data["mesh"]
    verts, faces = mesh["vertices"], mesh["faces"]

    if mesh_dir is None or len(verts) <= LARGE_MESH_VERTEX_THRESHOLD:
        return "\n".join([
            f'# --- Mesh data for "{obj_data["name"]}" ---',
            f'# Coordinates are LOCAL (object-space): the vertex positions before',
            f'# the object\'s own location/rotation/scale below is applied to them.',
            f'{var}_verts = {_fmt(verts)}',
            f'{var}_faces = {_fmt(faces)}',
            f'{var}_mesh = bpy.data.meshes.new({_fmt(mesh["mesh_name"])})',
            f'# from_pydata(verts, edges, faces) builds the mesh straight from raw',
            f'# geometry - this is a literal reconstruction, not a guessed primitive.',
            f'{var}_mesh.from_pydata({var}_verts, [], {var}_faces)',
        ])

    os.makedirs(mesh_dir, exist_ok=True)
    safe_name = _safe_var(mesh["mesh_name"])
    ply_path = os.path.join(mesh_dir, f"{safe_name}.ply")
    _write_ply_ascii(ply_path, verts, faces)
    return "\n".join([
        f'# --- Mesh data for "{obj_data["name"]}" ({len(verts)} verts) ---',
        f'# Too large for inline Python literals (keeps this script fast to open',
        f'# and parse) - geometry stored in a companion .ply file instead. If you',
        f'# move this script, move the "reverse_script_meshes" folder with it, or',
        f'# fix the path below.',
        f'{var}_ply_path = {_fmt(ply_path)}',
        f'try:',
        f'    bpy.ops.wm.ply_import(filepath={var}_ply_path)       # Blender 4.x',
        f'except AttributeError:',
        f'    bpy.ops.import_mesh.ply(filepath={var}_ply_path)     # Blender 3.x',
        f'{var}_imported = bpy.context.selected_objects[-1]',
        f'{var}_mesh = {var}_imported.data',
        f'bpy.data.objects.remove({var}_imported, do_unlink=True)  # keep the mesh, drop the extra object',
    ])


def _gen_smooth_shading_block(obj_data, var):
    smooth = obj_data["mesh"].get("use_smooth") or []
    if not any(smooth):
        return ""
    return "\n".join([
        f'# per-face smooth/flat shading, captured face-by-face so mixed',
        f'# smooth-body / flat-hard-edge meshes come back exactly as they were.',
        f'{var}_smooth = {_fmt(smooth)}',
        f'for poly, is_smooth in zip({var}_mesh.polygons, {var}_smooth):',
        f'    poly.use_smooth = is_smooth',
    ])


def _gen_transform_block(obj_data, var):
    lines = [
        f'# --- Transform for "{obj_data["name"]}" ---',
        f'# location: XYZ offset in world units from the scene origin.',
        f'{var}.location = {_fmt(obj_data["location"])}',
    ]
    if obj_data["rotation_mode"] == 'QUATERNION':
        lines += [
            f'# rotation stored as a quaternion (w, x, y, z) - used instead of',
            f'# Euler angles here specifically to avoid gimbal lock.',
            f'{var}.rotation_mode = "QUATERNION"',
            f'{var}.rotation_quaternion = {_fmt(obj_data["rotation_quaternion"])}',
        ]
    else:
        lines += [
            f'# rotation stored as Euler angles in RADIANS, applied in order',
            f'# "{obj_data["rotation_mode"]}" (radians = degrees * pi / 180).',
            f'{var}.rotation_mode = {_fmt(obj_data["rotation_mode"])}',
            f'{var}.rotation_euler = {_fmt(obj_data["rotation_euler"])}',
        ]
    lines += [
        f'# scale: per-axis multiplier, applied before rotation/location in the',
        f'# object\'s local->world matrix (T * R * S order).',
        f'{var}.scale = {_fmt(obj_data["scale"])}',
    ]
    return "\n".join(lines)


def _gen_modifier_block(obj_data, var):
    if not obj_data["modifiers"]:
        return ""
    lines = [f'# --- Modifiers for "{obj_data["name"]}" (stack order = execution order) ---']
    for mod in obj_data["modifiers"]:
        mvar = _safe_var(mod["name"], prefix=f"{var}_mod_")
        lines.append(f'{mvar} = {var}.modifiers.new(name={_fmt(mod["name"])}, type={_fmt(mod["type"])})')
        if mod["type"] in _NESTED_SETTINGS_PROPS:
            lines.append(f'# This is a physics setup, not a baked simulation: parameters below are')
            lines.append(f'# restored, but you still need to play/bake the timeline in Blender to')
            lines.append(f'# get matching per-frame motion - no cached simulation data is replayed.')
        for k, v in mod["props"].items():
            if k.startswith("__NESTED__"):
                nested_name = k[len("__NESTED__"):]
                lines.append(f'{mvar}_{nested_name} = {mvar}.{nested_name}')
                for nk, nv in v.items():
                    if isinstance(nv, tuple) and len(nv) == 2 and nv[0] == "__ID_REF__":
                        lines.append(f'# {nested_name}.{nk} referenced "{nv[1]}" in the original scene -')
                        lines.append(f'# {mvar}_{nested_name}.{nk} = bpy.data.objects.get({_fmt(nv[1])})')
                        continue
                    lines.append(f'{mvar}_{nested_name}.{nk} = {_fmt(nv)}')
                continue
            if isinstance(v, tuple) and len(v) == 2 and v[0] == "__ID_REF__":
                lines.append(f'# {k} referenced "{v[1]}" in the original scene -')
                lines.append(f'# re-link it manually if that object/collection exists here:')
                lines.append(f'# {mvar}.{k} = bpy.data.objects.get({_fmt(v[1])})')
                continue
            lines.append(f'{mvar}.{k} = {_fmt(v)}')
    return "\n".join(lines)


def _gen_material_block(obj_data, var):
    if not obj_data["materials"]:
        return ""
    lines = [f'# --- Materials for "{obj_data["name"]}" ---']
    for i, mat in enumerate(obj_data["materials"]):
        mvar = f"{var}_mat_{i}"
        lines.append(f'{mvar} = bpy.data.materials.new({_fmt(mat["name"])})')
        if mat["use_nodes"]:
            lines.append(f'{mvar}.use_nodes = True')
            lines.append(f'{mvar}_bsdf = {mvar}.node_tree.nodes.get("Principled BSDF")')
            lines.append(f'if {mvar}_bsdf:')
            inputs = mat.get("principled_inputs", {})
            if inputs:
                for key, val in inputs.items():
                    lines.append(f'    if {_fmt(key)} in {mvar}_bsdf.inputs:')
                    lines.append(f'        {mvar}_bsdf.inputs[{_fmt(key)}].default_value = {_fmt(val)}')
            else:
                lines.append(f'    pass  # no Principled BSDF inputs captured')
        else:
            lines.append(f'{mvar}.diffuse_color = {_fmt(mat.get("diffuse_color", (0.8, 0.8, 0.8, 1.0)))}')
        lines.append(f'{var}.data.materials.append({mvar})')
    return "\n".join(lines)


def generate_script(scene_data, mesh_dir=None):
    header = [
        '"""',
        "Auto-generated by Reverse Blender Script.",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "Reconstructs the captured objects via direct bpy calls: geometry,",
        "transforms, modifiers, materials and parenting. Run in Blender's",
        "Text Editor (Alt+P) or: blender --background --python this_file.py",
        "",
        "NOT captured in this version: geometry nodes, particle systems/hair,",
        "grass, fluid/smoke/cloth simulation CACHES (cloth/soft-body/collision",
        "SETTINGS are captured - see the comment above each physics modifier",
        "below), drivers, shape keys, animation F-curves. Extend",
        "extract_object()/generate_script() in the source add-on to add them.",
        '"""',
        "import bpy",
        "",
    ]

    body = []
    var_names = {}
    mesh_var_by_dataname = {}  # mesh.name -> "{var}_mesh" python expression,
                               # so linked-duplicate / instanced objects (e.g.
                               # hundreds of grass/prop instances sharing one
                               # mesh datablock) reuse it instead of each
                               # re-emitting a full copy of the geometry.

    for i, obj_data in enumerate(scene_data):
        var = _safe_var(obj_data["name"], prefix=f"obj_{i}_")
        var_names[obj_data["name"]] = var

        body.append("\n# " + "=" * 68)
        body.append(f'# Object {i}: "{obj_data["name"]}" ({obj_data["type"]})')
        body.append("# " + "=" * 68)

        if obj_data["type"] == 'MESH' and obj_data["mesh"]:
            mesh_name = obj_data["mesh"]["mesh_name"]
            if mesh_name in mesh_var_by_dataname:
                body.append(f'# "{mesh_name}" mesh data was already generated above for another')
                body.append(f'# object - this one shares the same mesh datablock (a linked')
                body.append(f'# duplicate/instance in the original scene), so its geometry is')
                body.append(f'# reused instead of being duplicated again.')
                mesh_expr = mesh_var_by_dataname[mesh_name]
            else:
                body.append(_gen_mesh_geometry_block(obj_data, var, mesh_dir))
                smooth_code = _gen_smooth_shading_block(obj_data, var)
                if smooth_code:
                    body.append(smooth_code)
                body.append(f'{var}_mesh.update()')
                mesh_expr = f'{var}_mesh'
                mesh_var_by_dataname[mesh_name] = mesh_expr
            body.append(f'{var} = bpy.data.objects.new({_fmt(obj_data["name"])}, {mesh_expr})')
        else:
            body.append(f'# "{obj_data["type"]}" objects beyond MESH keep only their transform here -')
            body.append(f'# see the NOT-captured list in the file header for extension points.')
            body.append(f'{var} = bpy.data.objects.new({_fmt(obj_data["name"])}, None)')

        body.append(f'bpy.context.collection.objects.link({var})')
        body.append(_gen_transform_block(obj_data, var))

        mods = _gen_modifier_block(obj_data, var)
        if mods:
            body.append(mods)

        mats = _gen_material_block(obj_data, var)
        if mats:
            body.append(mats)

    parent_lines = ['\n# --- Parenting (re-linked once every object exists, so order never matters) ---']
    any_parent = False
    for obj_data in scene_data:
        if obj_data["parent"] and obj_data["parent"] in var_names:
            any_parent = True
            parent_lines.append(
                f'{var_names[obj_data["name"]]}.parent = {var_names[obj_data["parent"]]}'
            )
    if any_parent:
        body.append("\n".join(parent_lines))

    return "\n".join(header) + "\n".join(body) + "\n"


# =========================================================================
# 3. OPTIONAL AI ASSIST
#    Strictly a comment/readability pass over the already-correct script.
#    It's not in the critical path: generation above works with zero AI
#    calls and zero API key. This only runs when the user clicks the button.
# =========================================================================

AI_SYSTEM_PROMPT = (
    "You are assisting inside a Blender add-on. You are given a Python script "
    "that was mechanically generated from a live Blender scene (raw bpy calls "
    "reconstructing geometry, transforms, modifiers, materials). Your job is "
    "ONLY to: (1) improve or add short comments explaining non-obvious math or "
    "geometry, (2) tighten up section headers, (3) flag in a comment anything "
    "that looks like it may have lost precision or data. Do NOT change any "
    "numeric value, do NOT remove or reorder any bpy call, do NOT invent new "
    "functionality. Return the full script and nothing else - no markdown "
    "fences, no commentary outside the script."
)


def ai_enhance_script(script_text, api_key, model="claude-sonnet-5"):
    """POSTs the script to the Claude API for a comments-only pass.
    Requires the user's own Anthropic API key (set in add-on preferences).
    Raises on failure; caller decides how to surface that."""
    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": AI_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": script_text}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(parts).strip()


# =========================================================================
# 4. OPERATORS, PANEL, PREFERENCES, KEYMAP
# =========================================================================

class ReverseScriptPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    api_key: bpy.props.StringProperty(
        name="Anthropic API Key",
        description="Used only by the optional AI Enhance button. Never required for core generation.",
        subtype='PASSWORD',
        default="",
    )
    ai_model: bpy.props.StringProperty(
        name="Model",
        default="claude-sonnet-5",
        description="An Anthropic model ID. Check platform.claude.com/docs/about-claude/models "
                     "if this ever starts erroring - Anthropic retires older dated snapshots "
                     "roughly once a year, and the default here will eventually need updating too.",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "api_key")
        layout.prop(self, "ai_model")
        layout.label(
            text="AI Enhance only adds comments - it never changes the generated logic or numbers.",
            icon='INFO',
        )


class REVSCRIPT_OT_generate(bpy.types.Operator):
    bl_idname = "object.reverse_script_generate"
    bl_label = "Generate Reverse Script"
    bl_description = "Reverse-engineer selected objects (or the whole scene) into a commented, runnable Python script"
    bl_options = {'REGISTER'}

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[
            ('SELECTED', "Selected Objects", ""),
            ('SCENE', "Entire Scene", ""),
        ],
        default='SELECTED',
    )

    def invoke(self, context, event):
        objs = context.selected_objects if self.scope == 'SELECTED' else list(context.scene.objects)
        total_verts = sum(len(o.data.vertices) for o in objs if o.type == 'MESH')
        if total_verts > CONFIRM_VERTEX_THRESHOLD:
            # Only (operator, event) is supported back to Blender 3.6's API -
            # title/message kwargs were added later, so they're deliberately
            # not used here to keep this working on the add-on's stated
            # minimum version. The detail goes out via self.report instead.
            self.report(
                {'INFO'},
                f"~{total_verts:,} vertices across {len(objs)} object(s) - "
                f"confirm to continue, this may take a moment.",
            )
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        objs = context.selected_objects if self.scope == 'SELECTED' else list(context.scene.objects)
        if not objs:
            self.report({'WARNING'}, "Nothing to reverse-engineer: no objects selected / in scene.")
            return {'CANCELLED'}

        mesh_dir = None
        if bpy.data.filepath:
            mesh_dir = os.path.join(os.path.dirname(bpy.data.filepath), "reverse_script_meshes")

        scene_data = extract_scene(objs)

        large_meshes = [
            d["mesh"]["mesh_name"] for d in scene_data
            if d.get("mesh") and len(d["mesh"]["vertices"]) > LARGE_MESH_VERTEX_THRESHOLD
        ]
        if large_meshes and mesh_dir is None:
            self.report(
                {'WARNING'},
                f"{len(large_meshes)} large mesh(es) kept as inline literals - "
                f"save the .blend file first to enable companion .ply export "
                f"for meshes over {LARGE_MESH_VERTEX_THRESHOLD:,} verts.",
            )

        script_text = generate_script(scene_data, mesh_dir=mesh_dir)

        text_name = f"ReverseScript_{time.strftime('%Y%m%d_%H%M%S')}.py"
        text_block = bpy.data.texts.new(text_name)
        text_block.from_string(script_text)

        for area in context.screen.areas:
            if area.type == 'TEXT_EDITOR':
                area.spaces.active.text = text_block
                break

        context.scene["reverse_script_last_text"] = text_name
        self.report({'INFO'}, f"Generated {text_name} ({len(objs)} object(s))")
        return {'FINISHED'}


class REVSCRIPT_OT_ai_enhance(bpy.types.Operator):
    bl_idname = "object.reverse_script_ai_enhance"
    bl_label = "AI Enhance Last Script"
    bl_description = "Send the last generated script to Claude for a comments-only cleanup pass (requires your own API key in preferences)"
    bl_options = {'REGISTER'}

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        if not prefs.api_key:
            self.report({'ERROR'}, "Set your Anthropic API key in Add-on Preferences first.")
            return {'CANCELLED'}

        name = context.scene.get("reverse_script_last_text")
        if not name or name not in bpy.data.texts:
            self.report({'ERROR'}, "No generated script found. Run Generate Reverse Script first.")
            return {'CANCELLED'}

        text_block = bpy.data.texts[name]
        original = text_block.as_string()

        try:
            enhanced = ai_enhance_script(original, prefs.api_key, prefs.ai_model)
        except urllib.error.HTTPError as e:
            self.report({'ERROR'}, f"API error: {e.code} {e.reason}")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"AI Enhance failed: {e}")
            return {'CANCELLED'}

        text_block.clear()
        text_block.write(enhanced)
        self.report({'INFO'}, f"AI-enhanced {name}")
        return {'FINISHED'}


class REVSCRIPT_PT_panel(bpy.types.Panel):
    bl_label = "Reverse Script"
    bl_idname = "REVSCRIPT_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Reverse Script"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        op = col.operator("object.reverse_script_generate", text="Generate (Selected)", icon='CONSOLE')
        op.scope = 'SELECTED'
        op2 = col.operator("object.reverse_script_generate", text="Generate (Whole Scene)", icon='SCENE_DATA')
        op2.scope = 'SCENE'
        col.separator()
        col.operator("object.reverse_script_ai_enhance", text="AI Enhance Last Script", icon='FUND')
        col.separator()
        col.label(text="Shortcut: Ctrl+Shift+R", icon='KEYINGSET')


classes = (
    ReverseScriptPreferences,
    REVSCRIPT_OT_generate,
    REVSCRIPT_OT_ai_enhance,
    REVSCRIPT_PT_panel,
)

addon_keymaps = []


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
        kmi = km.keymap_items.new(
            REVSCRIPT_OT_generate.bl_idname, type='R', value='PRESS',
            ctrl=True, shift=True,
        )
        kmi.properties.scope = 'SELECTED'
        addon_keymaps.append((km, kmi))


def unregister():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
