"""Tools Claude can call. Every function here runs on Blender's main thread."""

import base64
import contextlib
import difflib
import io
import json
import linecache
import math
import os
import tempfile
import time
import traceback

import bpy

MAX_OUTPUT_CHARS = 8000
MAX_IMAGE_EDGE = 1568

# ---------------------------------------------------------------------------
# Tool definitions sent to the API
# ---------------------------------------------------------------------------

TOOL_DEFS = [
    {
        "name": "get_scene_summary",
        "description": (
            "Get an overview of the open Blender scene: file, frame range, render settings, "
            "mode, selection, collections, and a list of objects with transforms, materials "
            "and modifiers. Call this before changing the scene so you use real names."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "max_objects": {
                    "type": "integer",
                    "description": "Maximum number of objects to list (default 100).",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_object_details",
        "description": (
            "Get detailed information about one object: transforms, mesh stats, modifiers "
            "and their settings, constraints, material node setups, light/camera data, "
            "animation and custom properties."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Exact object name."}
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "execute_python",
        "description": (
            "Run Python code in the user's live Blender session (bpy, bmesh, mathutils, math "
            "are pre-imported; C = bpy.context, D = bpy.data). The code runs with the 3D "
            "Viewport as context. Variables persist between calls. Use print() or assign to "
            "`result` to return information. Each run becomes one undo step. The user may be "
            "asked to approve the code before it runs and can reject it."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One short line telling the user what this code does.",
                },
                "code": {"type": "string", "description": "Python source to execute."},
            },
            "required": ["summary", "code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "capture_viewport",
        "description": (
            "Take a screenshot of the 3D Viewport exactly as the user sees it, to check your "
            "work visually."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
]


def validate(name, args):
    """Return an error string if the (eagerly streamed) tool input is malformed."""
    if not isinstance(args, dict):
        return "Tool input must be a JSON object."
    if name == "get_scene_summary":
        mo = args.get("max_objects")
        if mo is not None and not isinstance(mo, int):
            return "max_objects must be an integer."
    elif name == "get_object_details":
        if not isinstance(args.get("name"), str) or not args["name"]:
            return "name must be a non-empty string."
    elif name == "execute_python":
        if not isinstance(args.get("code"), str) or not args["code"].strip():
            return "code must be a non-empty string."
        if not isinstance(args.get("summary"), str):
            return "summary must be a string."
        try:
            compile(args["code"], "<claude>", "exec")
        except SyntaxError as e:
            return f"SyntaxError: {e}"
    elif name == "capture_viewport":
        pass
    else:
        return f"Unknown tool: {name}"
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def view3d_override():
    """Context override pointing at the first 3D Viewport, so operators have a valid context."""
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                ov = {"window": win, "screen": win.screen, "area": area}
                if region:
                    ov["region"] = region
                return ov
    if wm.windows:
        return {"window": wm.windows[0], "screen": wm.windows[0].screen}
    return {}


def _r(v, n=3):
    try:
        return [round(x, n) for x in v]
    except TypeError:
        return round(v, n)


def _deg(euler):
    return [round(math.degrees(a), 2) for a in euler]


def _rna_props(struct, limit=60):
    """Dump simple (non-pointer) RNA properties of a struct."""
    out = {}
    for p in struct.bl_rna.properties:
        if len(out) >= limit:
            break
        ident = p.identifier
        if ident in {"rna_type", "name", "type"}:
            continue
        try:
            v = getattr(struct, ident)
        except Exception:
            continue
        if p.type in {"BOOLEAN", "INT", "FLOAT"}:
            if getattr(p, "array_length", 0):
                try:
                    v = [round(x, 4) if isinstance(x, float) else x for x in v]
                except TypeError:
                    continue
            elif isinstance(v, float):
                v = round(v, 4)
            out[ident] = v
        elif p.type == "ENUM":
            out[ident] = sorted(v) if isinstance(v, set) else v
        elif p.type == "STRING":
            out[ident] = v
        elif p.type == "POINTER" and not p.is_readonly and v is not None and hasattr(v, "name"):
            out[ident] = f"<{v.name}>"
    return out


def _socket_value(sock):
    if not hasattr(sock, "default_value"):
        return None
    v = sock.default_value
    try:
        return [round(x, 3) for x in v]
    except TypeError:
        return round(v, 3) if isinstance(v, float) else v


def _material_info(mat):
    if mat is None:
        return None
    info = {"name": mat.name, "users": mat.users}
    tree = getattr(mat, "node_tree", None)
    if tree:
        nodes = []
        for node in list(tree.nodes)[:40]:
            n = {"name": node.name, "type": node.bl_idname}
            inputs = {}
            for sock in node.inputs:
                if sock.is_linked:
                    src = sock.links[0].from_node.name if sock.links else "?"
                    inputs[sock.name] = f"<linked from {src}>"
                elif sock.enabled and not sock.hide:
                    val = _socket_value(sock)
                    if val is not None:
                        inputs[sock.name] = val
            if inputs:
                n["inputs"] = inputs
            if node.type == "TEX_IMAGE" and node.image:
                n["image"] = node.image.filepath or node.image.name
            nodes.append(n)
        info["nodes"] = nodes
        info["links"] = [
            f"{l.from_node.name}.{l.from_socket.name} -> {l.to_node.name}.{l.to_socket.name}"
            for l in list(tree.links)[:60]
        ]
    else:
        info["diffuse_color"] = _r(mat.diffuse_color)
    return info


def _dump(obj):
    return json.dumps(obj, indent=1, default=str)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def get_scene_summary(args):
    max_objects = args.get("max_objects") or 100
    ctx = bpy.context
    scene = ctx.scene
    objs = list(scene.objects)
    counts = {}
    for o in objs:
        counts[o.type] = counts.get(o.type, 0) + 1

    def coll_tree(coll, depth=0):
        node = {"name": coll.name, "objects": len(coll.objects)}
        if coll.children and depth < 6:
            node["children"] = [coll_tree(c, depth + 1) for c in coll.children]
        return node

    listed = []
    for o in objs[:max_objects]:
        entry = {
            "name": o.name,
            "type": o.type,
            "location": _r(o.location),
            "rotation_deg": _deg(o.rotation_euler),
            "scale": _r(o.scale),
            "dimensions": _r(o.dimensions),
        }
        if o.parent:
            entry["parent"] = o.parent.name
        mats = [s.material.name for s in o.material_slots if s.material]
        if mats:
            entry["materials"] = mats
        if o.modifiers:
            entry["modifiers"] = [f"{m.name} ({m.type})" for m in o.modifiers]
        if not o.visible_get():
            entry["hidden"] = True
        if o.select_get():
            entry["selected"] = True
        listed.append(entry)

    info = {
        "blender_version": bpy.app.version_string,
        "file": bpy.data.filepath or "(unsaved)",
        "scene": scene.name,
        "mode": ctx.mode,
        "frame": {
            "current": scene.frame_current,
            "start": scene.frame_start,
            "end": scene.frame_end,
            "fps": scene.render.fps,
        },
        "render": {
            "engine": scene.render.engine,
            "resolution": [
                scene.render.resolution_x,
                scene.render.resolution_y,
                scene.render.resolution_percentage,
            ],
        },
        "units": {"system": scene.unit_settings.system, "scale_length": scene.unit_settings.scale_length},
        "camera": scene.camera.name if scene.camera else None,
        "world": scene.world.name if scene.world else None,
        "active_object": ctx.view_layer.objects.active.name if ctx.view_layer.objects.active else None,
        "object_counts": counts,
        "collections": coll_tree(scene.collection),
        "objects": listed,
    }
    if len(objs) > max_objects:
        info["note"] = f"Showing {max_objects} of {len(objs)} objects."
    info["materials_in_file"] = [m.name for m in bpy.data.materials][:60]
    return _dump(info)


def get_object_details(args):
    name = args["name"]
    o = bpy.data.objects.get(name)
    if o is None:
        close = difflib.get_close_matches(name, [x.name for x in bpy.data.objects], n=5)
        raise ValueError(f"No object named {name!r}. Close matches: {close}")

    info = {
        "name": o.name,
        "type": o.type,
        "data": o.data.name if o.data else None,
        "location": _r(o.location),
        "rotation_mode": o.rotation_mode,
        "rotation_deg": _deg(o.rotation_euler),
        "scale": _r(o.scale),
        "dimensions": _r(o.dimensions),
        "world_location": _r(o.matrix_world.translation),
        "parent": o.parent.name if o.parent else None,
        "children": [c.name for c in o.children],
        "collections": [c.name for c in o.users_collection],
        "visibility": {
            "visible": o.visible_get(),
            "hide_viewport": o.hide_viewport,
            "hide_render": o.hide_render,
        },
        "display_type": o.display_type,
    }
    if o.type == "MESH":
        me = o.data
        info["mesh"] = {
            "vertices": len(me.vertices),
            "edges": len(me.edges),
            "faces": len(me.polygons),
            "uv_maps": [uv.name for uv in me.uv_layers],
            "attributes": [a.name for a in me.attributes][:30],
            "vertex_groups": [vg.name for vg in o.vertex_groups][:30],
            "shape_keys": [k.name for k in me.shape_keys.key_blocks] if me.shape_keys else [],
        }
    elif o.type == "LIGHT":
        info["light"] = _rna_props(o.data, 30)
    elif o.type == "CAMERA":
        info["camera"] = _rna_props(o.data, 30)
    elif o.type in {"CURVE", "FONT", "SURFACE"}:
        info["curve"] = _rna_props(o.data, 30)

    if o.modifiers:
        info["modifiers"] = [
            {"name": m.name, "type": m.type, "settings": _rna_props(m, 40)} for m in o.modifiers
        ]
    if o.constraints:
        info["constraints"] = [{"name": c.name, "type": c.type} for c in o.constraints]
    if o.material_slots:
        info["materials"] = [
            {"slot": i, "link": s.link, "material": _material_info(s.material)}
            for i, s in enumerate(o.material_slots)
        ]
    ad = o.animation_data
    if ad and ad.action:
        act = ad.action
        info["animation"] = {"action": act.name, "frame_range": _r(act.frame_range, 1)}
    keys = [k for k in o.keys() if not k.startswith("_")]
    if keys:
        info["custom_properties"] = {k: str(o[k])[:200] for k in keys[:30]}
    return _dump(info)


_NAMESPACE = {}
_RUN_COUNTER = [0]


def _namespace():
    if not _NAMESPACE:
        import bmesh
        import mathutils

        _NAMESPACE.update(
            {
                "__name__": "__claude__",
                "bpy": bpy,
                "bmesh": bmesh,
                "mathutils": mathutils,
                "Vector": mathutils.Vector,
                "Matrix": mathutils.Matrix,
                "Euler": mathutils.Euler,
                "Quaternion": mathutils.Quaternion,
                "math": math,
            }
        )
    _NAMESPACE["C"] = bpy.context
    _NAMESPACE["D"] = bpy.data
    _NAMESPACE["result"] = None
    return _NAMESPACE


def reset_namespace():
    _NAMESPACE.clear()


def execute_python(args):
    code = args["code"]
    summary = args.get("summary", "")
    _RUN_COUNTER[0] += 1
    filename = f"<claude-run-{_RUN_COUNTER[0]}>"
    # Register source so tracebacks show the offending line.
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)

    buf = io.StringIO()
    ns = _namespace()
    ok = True
    t0 = time.perf_counter()
    try:
        compiled = compile(code, filename, "exec")
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            with bpy.context.temp_override(**view3d_override()):
                exec(compiled, ns)
    except Exception as e:
        ok = False
        tb = traceback.extract_tb(e.__traceback__)
        # Keep only frames from Claude's code plus the final error line.
        frames = [f for f in tb if f.filename == filename] or tb[-1:]
        buf.write("Traceback (most recent call last):\n")
        buf.write("".join(traceback.format_list(frames)))
        buf.write("".join(traceback.format_exception_only(type(e), e)))
    elapsed = time.perf_counter() - t0

    try:
        with bpy.context.temp_override(**view3d_override()):
            bpy.ops.ed.undo_push(message=f"Claude: {summary[:60] or 'run code'}")
    except Exception:
        pass

    out = buf.getvalue()
    res = ns.get("result")
    if res is not None:
        out += f"\nresult = {res!r}"
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:2000] + "\n... (output truncated) ...\n" + out[-(MAX_OUTPUT_CHARS - 2000):]
    status = "OK" if ok else "ERROR"
    text = f"[{status}] ran in {elapsed:.2f}s\n{out.strip() or '(no output)'}"
    if not ok:
        raise RuntimeError(text)
    return text


def capture_viewport(args):
    path = os.path.join(tempfile.gettempdir(), f"claude_viewport_{os.getpid()}.png")
    ov = view3d_override()
    if "area" not in ov:
        raise RuntimeError("No 3D Viewport is open to capture.")
    captured = False
    err = None
    try:
        with bpy.context.temp_override(**ov):
            bpy.ops.screen.screenshot_area(filepath=path)
        captured = os.path.exists(path)
    except Exception as e:
        err = e
    if not captured:
        # Fallback: OpenGL render of the viewport (no UI chrome).
        try:
            with bpy.context.temp_override(**ov):
                bpy.ops.render.opengl(view_context=True)
            bpy.data.images["Render Result"].save_render(path)
            captured = True
        except Exception as e2:
            raise RuntimeError(f"Could not capture viewport: {err or ''} / {e2}")

    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        longest = max(w, h)
        if longest > MAX_IMAGE_EDGE:
            s = MAX_IMAGE_EDGE / longest
            img.scale(max(1, int(w * s)), max(1, int(h * s)))
            img.filepath_raw = path
            img.file_format = "PNG"
            img.save()
        w, h = img.size
    finally:
        bpy.data.images.remove(img)

    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("ascii")
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
        {"type": "text", "text": f"Screenshot of the 3D Viewport ({w}x{h})."},
    ]


HANDLERS = {
    "get_scene_summary": get_scene_summary,
    "get_object_details": get_object_details,
    "execute_python": execute_python,
    "capture_viewport": capture_viewport,
}


def run(name, args):
    """Run a tool. Returns (content, is_error). content is a str or list of content blocks."""
    try:
        if name in {"get_scene_summary", "get_object_details"}:
            with bpy.context.temp_override(**view3d_override()):
                return HANDLERS[name](args), False
        return HANDLERS[name](args), False
    except Exception as e:
        msg = str(e) or traceback.format_exc()
        return msg[:MAX_OUTPUT_CHARS], True


def context_snapshot():
    """Short, volatile description of what the user is doing right now."""
    ctx = bpy.context
    try:
        with ctx.temp_override(**view3d_override()):
            active = ctx.view_layer.objects.active
            sel = [o.name for o in ctx.selected_objects][:12]
            mode = ctx.mode
    except Exception:
        active, sel, mode = None, [], "?"
    scene = ctx.scene
    parts = [
        f"file: {os.path.basename(bpy.data.filepath) or '(unsaved)'}",
        f"mode: {mode}",
        f"active: {active.name if active else None}",
        f"selected: {sel}",
        f"frame: {scene.frame_current}",
        f"objects in scene: {len(scene.objects)}",
    ]
    return "; ".join(parts)
