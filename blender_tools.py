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

MAX_OUTPUT_CHARS = 20000  # Claude Code caps a tool result at about 25k tokens
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
            "work visually. If the screenshot comes back black, an offscreen render of the "
            "viewport is returned instead and the result says so."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "view_image",
        "description": (
            "Look at an image by eye: a file on disk (such as a render you saved), an image "
            "datablock by name (such as a texture you baked), or, with neither, the last "
            "render in Blender's Render Result. Use it to check renders and bakes; "
            "capture_viewport shows the viewport instead."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path of an image file to view."},
                "image": {
                    "type": "string",
                    "description": "Name of an image in bpy.data.images to view.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_animation_loop",
        "description": (
            "Check that a looping animation is seamless, using the evaluated scene rather than "
            "key values. Compares each object's geometry at the end frame with the start frame "
            "(nearest-vertex distance, so symmetric parts that rotate onto each other count as "
            "matching), and checks the motion across the seam for jumps and stalls (end keys "
            "with flat handles). Pass the frame where the loop starts and the frame whose pose "
            "should equal it. Restores the frame and NLA mute states afterwards."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "integer", "description": "First frame of the loop."},
                "end": {
                    "type": "integer",
                    "description": "Frame whose pose should equal the start frame (e.g. 73 for a 72-frame loop starting at 1).",
                },
                "objects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Objects to check (default: all visible meshes).",
                },
                "nla_track": {
                    "type": "string",
                    "description": "Play only the NLA tracks with this name during the check (e.g. 'idle').",
                },
            },
            "required": ["start", "end"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_detached_parts",
        "description": (
            "Find pieces of a model that float free of it or are buried inside it, which one "
            "rendered view easily hides. Splits the evaluated meshes into connected pieces, "
            "joins pieces whose surfaces cross or come within a small tolerance, and reports "
            "every group not joined to the main body with its position, size and gap. Pass "
            "root (or objects) to check one model when the scene holds several. Check at "
            "several frames if parts move."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Check the meshes in this object's hierarchy (it and all its descendants).",
                },
                "objects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Mesh objects to check (default: all visible meshes).",
                },
                "frames": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Frames to check at, up to 8 (default: the current frame).",
                },
                "tolerance": {
                    "type": "number",
                    "description": "Gap still counted as touching, in scene units (default 0.2% of the model's size).",
                },
            },
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
    elif name == "view_image":
        for key in ("path", "image"):
            if key in args and (not isinstance(args[key], str) or not args[key]):
                return f"{key} must be a non-empty string."
        if args.get("path") and args.get("image"):
            return "Pass either path or image, not both."
    elif name == "check_animation_loop":
        s, e = args.get("start"), args.get("end")
        if not isinstance(s, int) or not isinstance(e, int) or isinstance(s, bool) or isinstance(e, bool):
            return "start and end must be integers."
        if e - s < 8:
            return "The loop must be at least 8 frames long (end - start >= 8)."
        objs = args.get("objects")
        if objs is not None and (not isinstance(objs, list) or not all(isinstance(o, str) and o for o in objs)):
            return "objects must be a list of object names."
        tr = args.get("nla_track")
        if tr is not None and (not isinstance(tr, str) or not tr):
            return "nla_track must be a non-empty string."
    elif name == "check_detached_parts":
        if args.get("root") is not None and args.get("objects") is not None:
            return "Pass either root or objects, not both."
        r = args.get("root")
        if r is not None and (not isinstance(r, str) or not r):
            return "root must be a non-empty string."
        objs = args.get("objects")
        if objs is not None and (not isinstance(objs, list) or not all(isinstance(o, str) and o for o in objs)):
            return "objects must be a list of object names."
        fr = args.get("frames")
        if fr is not None and (not isinstance(fr, list) or not 1 <= len(fr) <= 8
                               or not all(isinstance(f, int) and not isinstance(f, bool) for f in fr)):
            return "frames must be a list of 1 to 8 integers."
        tol = args.get("tolerance")
        if tol is not None and (not isinstance(tol, (int, float)) or isinstance(tol, bool) or tol < 0):
            return "tolerance must be a non-negative number."
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
    # Skip `result` when it was also printed (e.g. a file read and printed), so long
    # text isn't sent twice and cut in half by the limit below.
    if res is not None and not (isinstance(res, str) and res.strip() and res.strip() in out):
        out += f"\nresult = {res!r}"
    if len(out) > MAX_OUTPUT_CHARS:
        head = MAX_OUTPUT_CHARS // 3
        tail = MAX_OUTPUT_CHARS - head
        cut = len(out) - head - tail
        out = (out[:head] + f"\n... ({cut:,} characters cut from the middle; print smaller "
               f"pieces to see them) ...\n" + out[-tail:])
    status = "OK" if ok else "ERROR"
    text = f"[{status}] ran in {elapsed:.2f}s\n{out.strip() or '(no output)'}"
    if not ok:
        raise RuntimeError(text)
    return text


def _temp_png(tag):
    return os.path.join(tempfile.gettempdir(), f"claude_{tag}_{os.getpid()}.png")


def _remove_file(path):
    with contextlib.suppress(OSError):
        os.remove(path)


def _mostly_black(img):
    """True if nearly every pixel of a loaded image is black (a failed capture)."""
    import numpy as np

    w, h = img.size
    ch = img.channels
    if not w or not h or not ch:
        return True
    px = np.empty(w * h * ch, dtype=np.float32)
    img.pixels.foreach_get(px)
    rgb = px.reshape(-1, ch)[:, : min(ch, 3)]
    return float((rgb.max(axis=1) > 0.02).mean()) < 0.01


def _is_black_file(path):
    img = bpy.data.images.load(path, check_existing=False)
    try:
        return _mostly_black(img)
    finally:
        bpy.data.images.remove(img)


def _png_blocks(src, out, caption):
    """Load the image at src, shrink it to MAX_IMAGE_EDGE, write it to out as PNG and
    return it as tool-result blocks. src is never modified unless it equals out."""
    img = bpy.data.images.load(src, check_existing=False)
    try:
        w, h = img.size
        if not w or not h:
            raise RuntimeError(f"Could not read image data from {src}.")
        black = _mostly_black(img)
        longest = max(w, h)
        if longest > MAX_IMAGE_EDGE:
            s = MAX_IMAGE_EDGE / longest
            img.scale(max(1, round(w * s)), max(1, round(h * s)))
        img.filepath_raw = out
        img.file_format = "PNG"
        img.save()
        sw, sh = img.size
    finally:
        bpy.data.images.remove(img)

    with open(out, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("ascii")
    text = f"{caption} ({w}x{h}"
    text += f", shown at {sw}x{sh})." if (sw, sh) != (w, h) else ")."
    if black:
        text += " Note: the image is almost entirely black."
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
        {"type": "text", "text": text},
    ]


def capture_viewport(args):
    path = _temp_png("viewport")
    ov = view3d_override()
    if "area" not in ov:
        raise RuntimeError("No 3D Viewport is open to capture.")
    _remove_file(path)  # never return a stale capture from an earlier call
    note = ""
    err = None
    try:
        with bpy.context.temp_override(**ov):
            bpy.ops.screen.screenshot_area(filepath=path)
        if os.path.exists(path):
            if not _is_black_file(path):
                return _png_blocks(path, path, "Screenshot of the 3D Viewport")
            # Screenshots read the window's on-screen image, which can be black when
            # Blender is minimized or covered. An offscreen draw doesn't depend on that.
            note = (
                "The screenshot came back black (Blender's window may be minimized or "
                "covered), so this is an offscreen render of the viewport instead, without "
                "overlays."
            )
            _remove_file(path)
    except Exception as e:
        err = e
    # Fallback: OpenGL render of the viewport (no UI chrome).
    try:
        with bpy.context.temp_override(**ov):
            bpy.ops.render.opengl(view_context=True)
        bpy.data.images["Render Result"].save_render(path)
    except Exception as e2:
        first = err or ("the screenshot was black" if note else "no screenshot was written")
        raise RuntimeError(f"Could not capture viewport: {first} / {e2}")
    caption = "Offscreen render of the 3D Viewport" if note else "Viewport render"
    blocks = _png_blocks(path, path, caption)
    if note:
        blocks[-1]["text"] = note + " " + blocks[-1]["text"]
    return blocks


def view_image(args):
    out = _temp_png("image")
    _remove_file(out)
    path = args.get("path")
    if path:
        path = os.path.abspath(bpy.path.abspath(path))
        if not os.path.isfile(path):
            raise RuntimeError(f"No file at {path}.")
        return _png_blocks(path, out, f"Image file {path}")

    name = args.get("image") or "Render Result"
    img = bpy.data.images.get(name)
    if img is None:
        if args.get("image"):
            names = ", ".join(sorted(i.name for i in bpy.data.images)[:30]) or "none"
            raise RuntimeError(f"No image named {name!r}. Images in the file: {names}.")
        raise RuntimeError("There is no render yet. Render first, or pass path or image.")

    src = _temp_png("image_src")
    _remove_file(src)
    if img.type in {"RENDER_RESULT", "COMPOSITING"}:
        # Render Result has no readable pixels; save_render writes it with the scene's
        # colour management, as the user would see it.
        try:
            img.save_render(src)
        except Exception as e:
            raise RuntimeError(f"Could not read {name!r} (has anything been rendered?): {e}")
        return _png_blocks(src, out, f"Image {name!r}")

    # Ordinary image (e.g. a baked texture, possibly packed or never saved): copy its
    # pixels into a temporary image so the original is left untouched.
    w, h = img.size
    if not w or not h or not img.has_data:
        raise RuntimeError(f"Image {name!r} has no pixel data.")
    px = [0.0] * (w * h * img.channels)
    img.pixels.foreach_get(px)
    tmp = bpy.data.images.new("claude_view_tmp", w, h, alpha=img.channels == 4,
                              float_buffer=img.is_float)
    try:
        tmp.colorspace_settings.name = img.colorspace_settings.name
        if img.channels == 4:
            tmp.pixels.foreach_set(px)
        else:  # expand to RGBA
            import numpy as np
            a = np.array(px, dtype=np.float32).reshape(-1, img.channels)
            rgba = np.ones((w * h, 4), dtype=np.float32)
            rgba[:, :3] = a[:, :1] if img.channels == 1 else a[:, :3]
            tmp.pixels.foreach_set(rgba.ravel())
        tmp.filepath_raw = src
        tmp.file_format = "PNG"
        tmp.save()
    finally:
        bpy.data.images.remove(tmp)
    return _png_blocks(src, out, f"Image {name!r}")


def _world_points(ev):
    """World-space points of an evaluated object: its vertices, or for objects without
    geometry (empties) the origin and three axis tips."""
    mw = ev.matrix_world
    me = None
    with contextlib.suppress(RuntimeError):
        me = ev.to_mesh()
    if me is not None:
        pts = [mw @ v.co for v in me.vertices]
        ev.to_mesh_clear()
        if pts:
            return pts
    from mathutils import Vector

    s = getattr(ev, "empty_display_size", 1.0) or 1.0
    return [mw @ Vector(p) for p in ((0, 0, 0), (s, 0, 0), (0, s, 0), (0, 0, s))]


def _len(d):
    if d < 0.01:
        return f"{d * 1000:.2f} mm"
    return f"{d * 100:.1f} cm" if d < 1 else f"{d:.3f} m"


def _action_fcurves(action, slot):
    layers = getattr(action, "layers", None)
    if layers is None:  # Blender before 4.4
        return list(action.fcurves)
    out = []
    for layer in layers:
        for st in layer.strips:
            if slot is not None:
                cb = st.channelbag(slot)
                if cb:
                    out += list(cb.fcurves)
            else:
                for cb in st.channelbags:
                    out += list(cb.fcurves)
    return out


def _playing_fcurves(o):
    """(fcurve, scene frame -> action frame) for every F-curve currently driving o."""
    ad = o.animation_data
    if not ad:
        return []
    out = []
    if ad.action:
        out += [(fc, lambda f: f) for fc in _action_fcurves(ad.action, getattr(ad, "action_slot", None))]
    if ad.use_nla:
        tracks = list(ad.nla_tracks)
        if any(t.is_solo for t in tracks):
            tracks = [t for t in tracks if t.is_solo]
        for t in tracks:
            if t.mute:
                continue
            for s in t.strips:
                if s.mute or not s.action:
                    continue

                def amap(f, s=s):
                    f = min(max(f, s.frame_start), s.frame_end)
                    return s.action_frame_start + (f - s.frame_start) / (s.scale or 1.0)
                out += [(fc, amap) for fc in _action_fcurves(s.action, getattr(s, "action_slot", None))]
    return out


def _stalled_channels(o, start, end):
    """Channels of o that keep moving the same way through the loop point but slow almost
    to a stop there (typically flat auto handles on the end keys). A channel that turns
    around at the loop point is a normal extreme, not a stall."""
    found = []
    for fc, amap in _playing_fcurves(o):
        v = {f: fc.evaluate(amap(f)) for f in (start, start + 1, start + 2, start + 3,
                                                end - 3, end - 2, end - 1, end)}
        seam = (abs(v[start + 1] - v[start]) + abs(v[end] - v[end - 1])) / 2
        near = (abs(v[start + 3] - v[start + 2]) + abs(v[end - 2] - v[end - 3])) / 2
        through = (v[end] - v[end - 2]) * (v[start + 2] - v[start]) > 0
        if through and near > 1e-6 and seam < 0.4 * near:
            found.append(f"{fc.data_path}[{fc.array_index}]")
    return found


def _with_ancestors(objs):
    seen = []
    for o in objs:
        while o is not None and o not in seen:
            seen.append(o)
            o = o.parent
    return seen


def check_animation_loop(args):
    from mathutils import Vector
    from mathutils.kdtree import KDTree

    scene = bpy.context.scene
    start, end = int(args["start"]), int(args["end"])
    mid = (start + end) // 2
    track = args.get("nla_track")
    names = args.get("objects")
    if names:
        missing = [n for n in names if n not in bpy.data.objects]
        if missing:
            raise RuntimeError(f"No object named: {', '.join(missing)}.")
        objs = [bpy.data.objects[n] for n in names]
    else:
        objs = [o for o in bpy.context.view_layer.objects
                if o is not None and o.type == "MESH" and o.visible_get()]
    if not objs:
        raise RuntimeError("No visible mesh objects to check.")

    drivers = _with_ancestors(objs)  # the objects and everything that moves them
    notes = []
    saved_mutes = []
    if track:
        found = False
        for o in bpy.data.objects:
            ad = o.animation_data
            if not ad or not len(ad.nla_tracks):
                continue
            for t in ad.nla_tracks:
                saved_mutes.append((t, t.mute))
                t.mute = t.name != track
                found = found or t.name == track
            if ad.action and o in drivers:
                notes.append(f"{o.name} also has an active action ({ad.action.name}), "
                             "which plays on top of its NLA tracks.")
        if not found:
            for t, m in saved_mutes:
                t.mute = m
            raise RuntimeError(f"No NLA track named {track!r} on any object.")

    frames = sorted({start, start + 1, start + 2, start + 3, mid, end - 3, end - 2, end - 1, end})
    mats = {o.name: {} for o in objs}
    pts = {}
    stalls = {}
    orig = (scene.frame_current, scene.frame_subframe)
    try:
        for o in drivers:  # while only the requested tracks are playing
            ch = _stalled_channels(o, start, end)
            if ch:
                stalls[o.name] = ch
        for f in frames:
            scene.frame_set(f)
            dg = bpy.context.evaluated_depsgraph_get()
            for o in objs:
                ev = o.evaluated_get(dg)
                mats[o.name][f] = ev.matrix_world.copy()
                if f in (start, mid, end):
                    pts[(o.name, f)] = _world_points(ev)
    finally:
        for t, m in saved_mutes:
            t.mute = m
        scene.frame_set(orig[0], subframe=orig[1])

    head = f"Loop check, frame {end} against frame {start}"
    head += f" (only NLA tracks named {track!r} playing):" if track else ":"
    lines, ok, static = [head], [], []
    for o in objs:
        n = o.name
        p0, p1, pm = pts[(n, start)], pts[(n, end)], pts[(n, mid)]
        lo = Vector((min(p.x for p in p0), min(p.y for p in p0), min(p.z for p in p0)))
        hi = Vector((max(p.x for p in p0), max(p.y for p in p0), max(p.z for p in p0)))
        diag = (hi - lo).length
        tol = max(1e-5, diag * 0.002)
        tiny = max(1e-7, diag * 1e-4)
        M = mats[n]
        moved = any(abs(x) > 1e-7 for f in frames for ra, rb in zip(M[f], M[start]) for x in ra - rb)
        if not moved and len(pm) == len(p0):
            moved = any((a - b).length > tiny for a, b in zip(pm, p0))
        if not moved and len(p1) == len(p0):
            moved = any((a - b).length > tiny for a, b in zip(p1, p0))
        if not moved:
            static.append(n)
            continue

        problems = []
        # Pose: every end-frame point should sit on a start-frame point.
        kd = KDTree(len(p0))
        for i, p in enumerate(p0):
            kd.insert(p, i)
        kd.balance()
        dists = [(kd.find(p)[2], p) for p in p1]
        worst, wp = max(dists, key=lambda x: x[0])
        median = sorted(d for d, _ in dists)[len(dists) // 2]
        if len(p1) != len(p0):
            problems.append(f"vertex count changes ({len(p0)} -> {len(p1)})")
        if worst > tol:
            problems.append(
                f"POSE MISMATCH: frame {end} is off frame {start} by up to {_len(worst)} "
                f"(median {_len(median)}; bounding-box diagonal {_len(diag)}), worst near "
                f"({wp.x:.2f}, {wp.y:.2f}, {wp.z:.2f}). A rotating part that is off its pivot "
                "drifts and snaps back; a part whose symmetry doesn't match the turn (three "
                "claws on a disc turning 1/7, say) jumps.")

        # Motion across the seam, from world-space transform deltas applied to the
        # object's extent (so a symmetric spin compares equal at both ends).
        corners = [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]

        def disp(a, b):
            D = M[b] @ M[a].inverted_safe()
            return [D @ c - c for c in corners]

        def mag(ds):
            return max(d.length for d in ds)

        ds, de = disp(start, start + 1), disp(end - 1, end)
        acc_s = max((a - b).length for a, b in zip(disp(start + 1, start + 2), ds))
        acc_e = max((a - b).length for a, b in zip(de, disp(end - 2, end - 1)))
        jump = max((a - b).length for a, b in zip(ds, de))
        if jump > tiny and jump > 3 * max(acc_s, acc_e):
            problems.append(
                f"JUMP: the motion changes abruptly at the seam (moves {_len(mag(de))}/frame "
                f"into frame {end}, {_len(mag(ds))}/frame out of frame {start}, in different "
                "directions).")

        # Stalls are judged per F-curve on the object and its parents: a channel that
        # keeps going the same way through the loop point but nearly stops there.
        src = [(a.name, stalls[a.name]) for a in _with_ancestors([o]) if a.name in stalls]
        if src:
            which = "; ".join(f"{a} {', '.join(ch)}" for a, ch in src)
            problems.append(
                f"STALL: moved by channels that slow almost to a stop at the loop point while "
                f"still moving the same way ({which}), so it pauses at every loop. Usually "
                "flat handles on the end keys: give those F-curves a Cycles modifier so the "
                "handles wrap around the loop.")
        if problems:
            lines.append(f"- {n}: " + " ".join(problems))
        else:
            ok.append(n)
    if ok:
        lines.append("- Seamless: " + ", ".join(ok))
    if static:
        lines.append(f"- Not moving in this range: {', '.join(static[:20])}"
                     + (f" and {len(static) - 20} more" if len(static) > 20 else ""))
    lines += [f"Note: {x}" for x in notes]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Detached parts
# ---------------------------------------------------------------------------


def _mesh_pieces(obj, depsgraph):
    """Connected pieces of an evaluated mesh, in world space. Only faces join vertices (a
    loose edge renders as nothing), and vertices at the same spot count as one, since
    importers split flat-shaded meshes per face."""
    from mathutils.bvhtree import BVHTree

    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        mw = ev.matrix_world
        verts = [mw @ v.co for v in me.vertices]
        parent = list(range(len(verts)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def join(a, b):
            a, b = find(a), find(b)
            if a != b:
                parent[b] = a

        seen = {}
        for i, p in enumerate(verts):
            j = seen.setdefault((round(p.x, 5), round(p.y, 5), round(p.z, 5)), i)
            if j != i:
                join(j, i)
        polys = [tuple(p.vertices) for p in me.polygons]
        for vs in polys:
            for v in vs[1:]:
                join(vs[0], v)
        groups = {}
        for vs in polys:
            groups.setdefault(find(vs[0]), []).append(vs)
        out = []
        for faces in groups.values():
            idx = sorted({v for vs in faces for v in vs})
            remap = {v: n for n, v in enumerate(idx)}
            pts = [verts[v] for v in idx]
            local = [[remap[v] for v in vs] for vs in faces]
            out.append({
                "obj": obj.name,
                "verts": pts,
                "polys": local,
                "tree": BVHTree.FromPolygons(pts, local),
                "lo": tuple(min(p[k] for p in pts) for k in range(3)),
                "hi": tuple(max(p[k] for p in pts) for k in range(3)),
            })
        return out
    finally:
        ev.to_mesh_clear()


def _union_tree(pieces):
    from mathutils.bvhtree import BVHTree

    verts, polys = [], []
    for p in pieces:
        off = len(verts)
        verts.extend(p["verts"])
        polys.extend([[off + v for v in vs] for vs in p["polys"]])
    return BVHTree.FromPolygons(verts, polys)


def _min_dist(points, tree, limit):
    best = limit
    for p in points:
        hit = tree.find_nearest(p, best)
        if hit[0] is not None and hit[3] < best:
            best = hit[3]
    return best


def _enclosed(point, tree):
    """True if a ray along each of the six axes leaves the surface from inside."""
    from mathutils import Vector

    for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        d = Vector(d)
        hit = tree.ray_cast(point, d)
        if hit[0] is None or hit[1].dot(d) <= 0:
            return False
    return True


def _detached_at_frame(objs, tol):
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    dg = bpy.context.evaluated_depsgraph_get()
    pcs = [p for o in objs for p in _mesh_pieces(o, dg)]
    if not pcs:
        raise RuntimeError("The meshes to check have no faces.")
    lo = Vector([min(p["lo"][k] for p in pcs) for k in range(3)])
    hi = Vector([max(p["hi"][k] for p in pcs) for k in range(3)])
    size = (hi - lo).length
    if tol is None:
        tol = max(size * 0.002, 1e-5)

    n = len(pcs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Sweep along X so only pieces whose boxes come near each other are compared.
    order = sorted(range(n), key=lambda i: pcs[i]["lo"][0])
    for k, a in enumerate(order):
        A = pcs[a]
        for b in order[k + 1:]:
            B = pcs[b]
            if B["lo"][0] > A["hi"][0] + tol:
                break
            if find(a) == find(b):
                continue
            if any(A["lo"][j] - tol > B["hi"][j] or B["lo"][j] - tol > A["hi"][j] for j in (1, 2)):
                continue
            small, big = (A, B) if len(A["verts"]) <= len(B["verts"]) else (B, A)
            if (A["tree"].overlap(B["tree"])
                    or _min_dist(small["verts"], big["tree"], tol) < tol
                    or _min_dist(big["verts"], small["tree"], tol) < tol):
                parent[find(b)] = find(a)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    ordered = sorted(groups.values(), key=lambda g: sum(len(pcs[i]["verts"]) for i in g), reverse=True)
    main = ordered[0]
    main_tree = _union_tree([pcs[i] for i in main])

    floating, buried = [], []
    for g in ordered[1:]:
        pts = [v for i in g for v in pcs[i]["verts"]]
        glo = Vector([min(p[k] for p in pts) for k in range(3)])
        ghi = Vector([max(p[k] for p in pts) for k in range(3)])
        entry = {
            "objects": sorted({pcs[i]["obj"] for i in g}),
            "pieces": len(g),
            "centre": (glo + ghi) / 2,
            "size": max(ghi - glo),
            "gap": _min_dist(pts, main_tree, size),
        }
        # Buried: sealed inside one main-body piece. Test piece by piece, since shells
        # nested inside each other confuse a ray test against the whole body.
        sample = pts[::max(1, len(pts) // 12)]
        hidden = any(
            all(pcs[m]["lo"][k] <= glo[k] and ghi[k] <= pcs[m]["hi"][k] for k in range(3))
            and all(_enclosed(p, pcs[m]["tree"]) for p in sample)
            for m in main)
        (buried if hidden else floating).append(entry)
    floating.sort(key=lambda e: -e["gap"])
    return {
        "pieces": n,
        "tol": tol,
        "main": sorted({pcs[i]["obj"] for i in main}),
        "floating": floating,
        "buried": buried,
    }


def check_detached_parts(args):
    root, names = args.get("root"), args.get("objects")
    if root:
        if root not in bpy.data.objects:
            raise RuntimeError(f"No object named {root!r}.")
        r = bpy.data.objects[root]
        objs = [o for o in [r, *r.children_recursive] if o.type == "MESH"]
    elif names:
        missing = [n for n in names if n not in bpy.data.objects]
        if missing:
            raise RuntimeError(f"No object named: {', '.join(missing)}.")
        objs = [bpy.data.objects[n] for n in names if bpy.data.objects[n].type == "MESH"]
    else:
        objs = [o for o in bpy.context.view_layer.objects
                if o is not None and o.type == "MESH" and o.visible_get()]
    if not objs:
        raise RuntimeError("No mesh objects to check.")

    scene = bpy.context.scene
    orig = (scene.frame_current, scene.frame_subframe)
    frames = args.get("frames") or [orig[0]]
    results = []
    try:
        for f in frames:
            scene.frame_set(f)
            results.append((f, _detached_at_frame(objs, args.get("tolerance"))))
    finally:
        scene.frame_set(orig[0], subframe=orig[1])

    def where(e):
        c = e["centre"]
        names = ", ".join(e["objects"][:4]) + (" ..." if len(e["objects"]) > 4 else "")
        count = f"{e['pieces']} pieces" if e["pieces"] > 1 else "1 piece"
        return f"{names} ({count}) around ({c.x:.2f}, {c.y:.2f}, {c.z:.2f}), {_len(e['size'])} across"

    lines = []
    for f, r in results:
        lines.append(f"Frame {f}: {len(objs)} meshes, {r['pieces']} connected pieces, "
                     f"touching within {_len(r['tol'])}.")
        if not r["floating"] and not r["buried"]:
            lines.append("- Every piece is joined to the main body.")
            continue
        main = r["main"]
        lines.append("- Main body: " + ", ".join(main[:15])
                     + (f" and {len(main) - 15} more" if len(main) > 15 else ""))
        for e in r["floating"][:40]:
            lines.append(f"- FLOATING {where(e)}, {_len(e['gap'])} from the main body")
        if len(r["floating"]) > 40:
            lines.append(f"- ... and {len(r['floating']) - 40} more floating groups")
        for e in r["buried"][:20]:
            lines.append(f"- BURIED {where(e)}, {_len(e['gap'])} inside the main body, so it never shows")
        if len(r["buried"]) > 20:
            lines.append(f"- ... and {len(r['buried']) - 20} more buried groups")
    return "\n".join(lines)


HANDLERS = {
    "get_scene_summary": get_scene_summary,
    "get_object_details": get_object_details,
    "execute_python": execute_python,
    "capture_viewport": capture_viewport,
    "view_image": view_image,
    "check_animation_loop": check_animation_loop,
    "check_detached_parts": check_detached_parts,
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
