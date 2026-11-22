"""Tools Claude can call. Every function here runs on Blender's main thread."""

import base64
import contextlib
import difflib
import io
import json
import linecache
import math
import os
import re
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
            "work visually. If the screenshot comes back black, or unchanged although the "
            "view or scene changed, an offscreen render of the viewport is returned instead "
            "and the result says so."
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
            "capture_viewport shows the viewport instead. The result gives the average colour "
            "and lightness of the subject (a plain background is left out), to check colour "
            "words such as 'near-black'. Images under 256 px are enlarged with nearest-pixel "
            "sampling, so a test render at the size a game draws the model can be judged. "
            "With crop, shows and measures only part of the image (such as the eyes or one "
            "glowing part), adding the average colour of its brightest pixels with its hue, "
            "saturation and value as numbers, which is the colour a glow actually renders at. "
            "With target, also how far that is from a colour the spec names."
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
                "crop": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": (
                        "[x_min, y_min, x_max, y_max] as fractions 0-1 of the image, origin at "
                        "the bottom left, x to the right and y up: the coordinates "
                        "bpy_extras.object_utils.world_to_camera_view returns, so a part's "
                        "projected corners can be passed directly."
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Hex colour the part should render at (e.g. '#9e47ff' from the spec). "
                        "Adds how far the brightest pixels are from it in hue, saturation "
                        "and value. Use with crop."
                    ),
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
                "collection": {
                    "type": "string",
                    "description": "Check every mesh in this collection and its child collections.",
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
            "root, collection or objects to check one model when the scene holds several. "
            "Check at several frames if parts move."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "description": "Check the meshes in this object's hierarchy (it and all its descendants).",
                },
                "collection": {
                    "type": "string",
                    "description": "Check every mesh in this collection and its child collections (for a model built as separate, unparented parts).",
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
    {
        "name": "measure_model",
        "description": (
            "Measure a model: height, width, depth, bounding-box centre, the lowest and highest "
            "points with the parts that reach them, and the main body's height leaving out thin "
            "parts (antennas, masts, fangs, rails, welded on or not), saying when such a part "
            "sets the height or the lowest point. With path, measures an exported .glb/.gltf instead: all meshes "
            "and primitives together at the rest pose (skins applied), in Blender's axes, plus "
            "the triangle count, animation names and time ranges, channels some clips key and "
            "others don't, texture sizes and vertex colours. With reference (an object whose "
            "hierarchy is measured, hidden or not, or a .glb path), adds a side-by-side "
            "comparison of width, depth, height, main-body height and squatness. Use it for "
            "final numbers rather than reading the file by hand."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Exported .glb or .gltf file to measure."},
                "root": {
                    "type": "string",
                    "description": "Measure the meshes in this object's hierarchy.",
                },
                "collection": {
                    "type": "string",
                    "description": "Measure every mesh in this collection and its child collections (for a model built as separate, unparented parts).",
                },
                "objects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Mesh objects to measure (default: all visible meshes).",
                },
                "frame": {"type": "integer", "description": "Frame to measure at (default: current)."},
                "reference": {
                    "type": "string",
                    "description": "Reference to compare with: an object name (e.g. 'REF_tank'; its hierarchy, hidden meshes included) or a .glb/.gltf path.",
                },
                "height": {
                    "type": "number",
                    "description": "Target height from the spec, to report how far off the model is.",
                },
                "ground": {"type": "number", "description": "Ground height (default 0)."},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_ground_contact",
        "description": (
            "Check how an animation meets the ground, frame by frame in the evaluated scene: "
            "the lowest point at each frame, frames where the model sinks below the ground or "
            "nothing touches it, and how fast points on the ground move while they touch it "
            "(foot sliding, or the walking speed of a cycle made in place). Use it on walks, "
            "runs and anything that stands. Restores the frame and NLA mute states afterwards."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "integer", "description": "First frame to check."},
                "end": {"type": "integer", "description": "Last frame to check."},
                "root": {
                    "type": "string",
                    "description": "Check the meshes in this object's hierarchy.",
                },
                "collection": {
                    "type": "string",
                    "description": "Check every mesh in this collection and its child collections (for a model built as separate, unparented parts).",
                },
                "objects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Mesh objects to check (default: all visible meshes).",
                },
                "nla_track": {
                    "type": "string",
                    "description": "Play only the NLA tracks with this name during the check (e.g. 'walk').",
                },
                "ground": {"type": "number", "description": "Ground height (default 0)."},
                "tolerance": {
                    "type": "number",
                    "description": "Distance still counted as touching the ground (default 0.5% of the model's height, at least 5 mm).",
                },
            },
            "required": ["start", "end"],
            "additionalProperties": False,
        },
    },
    {
        "name": "render_map_test",
        "description": (
            "Render the model as a game draws it on a strategy map: an orthographic view from "
            "above at the size the spec gives (px_height pixels for the model's height, or "
            "px_width pixels for its width on screen), framed so the whole model fits with a margin, "
            "with the dark outline the game adds, over a dark map colour. Returns the image "
            "enlarged pixel for pixel and the model's size in pixels. Uses the scene's render "
            "engine and colour management and its own sun (lighting='scene' uses the scene's "
            "lights instead), hides other objects for the render, and restores every scene "
            "setting afterwards. A unit turns on the map, so pass azimuths (for example "
            "[30, 120, 210]) to see it from several sides in one image. Counts the glow "
            "pixels (those showing a material with emission, found by a second render with "
            "those materials black) and gives their average colour. Also counts the model's bright, saturated "
            "pixels by hue (teal, violet and so on), to show which glows still read. With path "
            "it tests an exported file. Use it whenever a spec says how many pixels tall or "
            "wide the game draws the model."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "px_height": {
                    "type": "integer",
                    "description": "How many pixels tall the spec says the game draws the model (e.g. 30). Pass this or px_width.",
                },
                "px_width": {
                    "type": "integer",
                    "description": (
                        "How many pixels wide the spec says the game draws the model (e.g. 50), "
                        "instead of px_height: the model's width on screen in the view, the "
                        "widest of the views when there are several."
                    ),
                },
                "metres_per_px": {
                    "type": "number",
                    "description": "Size of one pixel in metres, when the spec gives it (e.g. 0.07). Default: from px_height or px_width.",
                },
                "elevation": {
                    "type": "number",
                    "description": "Camera angle above the horizon in degrees (default 55; 90 looks straight down).",
                },
                "azimuth": {
                    "type": "number",
                    "description": "Camera direction in degrees around the model, 0 = in front (-Y), 90 = on its +X side (default 30).",
                },
                "azimuths": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": (
                        "Several camera directions in one call (up to 6, instead of azimuth): "
                        "the views are returned side by side, left to right, with the size and "
                        "glow count of each."
                    ),
                },
                "lighting": {
                    "type": "string",
                    "enum": ["sun", "scene"],
                    "description": (
                        "'sun' (default): the scene's lights are left out and one sun lights "
                        "the model from above and to the side of the camera, the same in every "
                        "view. 'scene': the scene's own lights."
                    ),
                },
                "outline_px": {
                    "type": "integer",
                    "description": "Outline width in pixels (default 1; 0 for none).",
                },
                "background": {
                    "type": "string",
                    "description": "Map colour behind the model, as hex (default '#2a2b32').",
                },
                "view_transform": {
                    "type": "string",
                    "description": (
                        "Colour management view transform for this render, restored afterwards: "
                        "the one closest to the game's tonemapper ('Filmic' for Godot's Filmic, "
                        "'ACES 1.3' for ACES, 'AgX', 'Standard'). Default: the scene's. Glow colours and "
                        "the glow pixel counts depend on it."
                    ),
                },
                "root": {"type": "string", "description": "Render the meshes in this object's hierarchy."},
                "collection": {"type": "string", "description": "Render every mesh in this collection."},
                "objects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Mesh objects to render (default: all visible meshes).",
                },
                "frame": {"type": "integer", "description": "Frame to render (default: current)."},
                "path": {
                    "type": "string",
                    "description": (
                        "An exported .glb or .gltf to test instead of the scene: it is imported "
                        "for the render, in the pose the file stores, and removed afterwards. "
                        "Use it to run the map test on the final export."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
]


def _target_error(args, keys=("root", "collection", "objects")):
    """Error string if the root/collection/objects arguments in keys are malformed."""
    given = [k for k in keys if args.get(k) is not None]
    if len(given) > 1:
        return f"Pass only one of {', '.join(keys)}."
    for k in ("root", "collection"):
        v = args.get(k)
        if k in keys and v is not None and (not isinstance(v, str) or not v):
            return f"{k} must be a non-empty string."
    objs = args.get("objects")
    if objs is not None and (not isinstance(objs, list) or not all(isinstance(o, str) and o for o in objs)):
        return "objects must be a list of object names."
    return None


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
        crop = args.get("crop")
        if crop is not None:
            if (not isinstance(crop, list) or len(crop) != 4
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in crop)):
                return "crop must be four numbers: [x_min, y_min, x_max, y_max]."
            x0, y0, x1, y1 = crop
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                return "crop needs 0 <= x_min < x_max <= 1 and 0 <= y_min < y_max <= 1."
        tgt = args.get("target")
        if tgt is not None and (not isinstance(tgt, str) or not re.fullmatch(r"#?[0-9a-fA-F]{6}", tgt.strip())):
            return "target must be a hex colour such as '#9e47ff'."
    elif name == "render_map_test":
        sizes = [k for k in ("px_height", "px_width") if args.get(k) is not None]
        if len(sizes) != 1:
            return "Pass px_height or px_width (one of them)."
        px = args[sizes[0]]
        if not isinstance(px, int) or isinstance(px, bool) or not 8 <= px <= 256:
            return f"{sizes[0]} must be an integer from 8 to 256."
        err = _target_error(args)
        if err:
            return err
        glb = args.get("path")
        if glb is not None:
            if not isinstance(glb, str) or not glb.lower().endswith((".glb", ".gltf")):
                return "path must be a .glb or .gltf file."
            clash = [k for k in ("root", "collection", "objects", "frame") if args.get(k) is not None]
            if clash:
                return f"path tests the exported file at its stored pose; don't pass {', '.join(clash)} with it."
        for key, lo, hi in (("elevation", 5, 90), ("azimuth", -180, 360),
                            ("metres_per_px", 1e-4, 10), ("outline_px", 0, 4)):
            v = args.get(key)
            if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool)
                                  or not lo <= v <= hi):
                return f"{key} must be a number from {lo:g} to {hi:g}."
        azs = args.get("azimuths")
        if azs is not None:
            if args.get("azimuth") is not None:
                return "Pass azimuth or azimuths, not both."
            if (not isinstance(azs, list) or not 1 <= len(azs) <= 6
                    or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                           or not -180 <= v <= 360 for v in azs)):
                return "azimuths must be a list of 1 to 6 numbers from -180 to 360."
        if args.get("lighting") not in (None, "sun", "scene"):
            return "lighting must be 'sun' or 'scene'."
        fr = args.get("frame")
        if fr is not None and (not isinstance(fr, int) or isinstance(fr, bool)):
            return "frame must be an integer."
        bg = args.get("background")
        if bg is not None and (not isinstance(bg, str) or not re.fullmatch(r"#?[0-9a-fA-F]{6}", bg.strip())):
            return "background must be a hex colour such as '#2a2b32'."
        vt = args.get("view_transform")
        if vt is not None and (not isinstance(vt, str) or not vt.strip()):
            return "view_transform must be a view transform name such as 'Filmic' or 'AgX'."
    elif name == "check_animation_loop":
        s, e = args.get("start"), args.get("end")
        if not isinstance(s, int) or not isinstance(e, int) or isinstance(s, bool) or isinstance(e, bool):
            return "start and end must be integers."
        if e - s < 8:
            return "The loop must be at least 8 frames long (end - start >= 8)."
        err = _target_error(args, ("collection", "objects"))
        if err:
            return err
        tr = args.get("nla_track")
        if tr is not None and (not isinstance(tr, str) or not tr):
            return "nla_track must be a non-empty string."
    elif name == "check_detached_parts":
        err = _target_error(args)
        if err:
            return err
        fr = args.get("frames")
        if fr is not None and (not isinstance(fr, list) or not 1 <= len(fr) <= 8
                               or not all(isinstance(f, int) and not isinstance(f, bool) for f in fr)):
            return "frames must be a list of 1 to 8 integers."
        tol = args.get("tolerance")
        if tol is not None and (not isinstance(tol, (int, float)) or isinstance(tol, bool) or tol < 0):
            return "tolerance must be a non-negative number."
    elif name in ("measure_model", "check_ground_contact"):
        err = _target_error(args)
        if err:
            return err
        nums = ("height", "ground") if name == "measure_model" else ("ground", "tolerance")
        for key in nums:
            v = args.get(key)
            if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool)):
                return f"{key} must be a number."
        if name == "measure_model":
            p = args.get("path")
            if p is not None:
                if not isinstance(p, str) or not p:
                    return "path must be a non-empty string."
                if any(args.get(k) is not None for k in ("root", "collection", "objects")):
                    return "Pass either path or root/collection/objects, not both."
            fr = args.get("frame")
            if fr is not None and (not isinstance(fr, int) or isinstance(fr, bool)):
                return "frame must be an integer."
            h = args.get("height")
            if h is not None and h <= 0:
                return "height must be positive."
            ref = args.get("reference")
            if ref is not None and (not isinstance(ref, str) or not ref):
                return "reference must be a non-empty string."
        else:
            s, e = args.get("start"), args.get("end")
            if not isinstance(s, int) or not isinstance(e, int) or isinstance(s, bool) or isinstance(e, bool):
                return "start and end must be integers."
            if not 1 <= e - s <= 500:
                return "end must be 1 to 500 frames after start."
            tr = args.get("nla_track")
            if tr is not None and (not isinstance(tr, str) or not tr):
                return "nla_track must be a non-empty string."
            tol = args.get("tolerance")
            if tol is not None and tol < 0:
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


def _srgb(lin):
    import numpy as np

    lin = np.clip(lin, 0.0, 1.0)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)


def _hex(rgb):
    return "#" + "".join(f"{int(round(float(c) * 255)):02x}" for c in rgb)


def _colour_summary(img):
    """One sentence with the average colour of an image's subject (leaving out a plain
    background that fills the corners, and transparent pixels) and its lightness."""
    import numpy as np

    w, h = img.size
    ch = img.channels
    if ch < 3:
        return ""
    px = np.empty(w * h * ch, dtype=np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(-1, ch)
    rgb = px[:, :3]
    if img.is_float:  # float buffers hold linear values; byte ones hold sRGB as stored
        rgb = _srgb(rgb)
    corner_idx = [0, w - 1, (h - 1) * w, h * w - 1]
    mask = px[:, 3] > 0.5 if ch == 4 else np.ones(len(px), dtype=bool)
    corners = rgb[corner_idx]
    bg = None
    if mask[corner_idx].all() and np.ptp(corners, axis=0).max() < 0.03:
        bg = corners.mean(axis=0)
        mask &= np.abs(rgb - bg).max(axis=1) > 0.03
    if mask.sum() < 0.01 * len(px):
        return ""
    mean = rgb[mask].mean(axis=0)
    lin = np.where(mean <= 0.04045, mean / 12.92, ((mean + 0.055) / 1.055) ** 2.4)
    Y = float(lin @ np.array([0.2126, 0.7152, 0.0722]))
    L = 116 * (Y ** (1 / 3) if Y > 0.008856 else 7.787 * Y + 16 / 116) - 16
    where = (f"the subject ({mask.mean() * 100:.0f}% of the image; the background "
             f"{_hex(bg)} left out)" if bg is not None else "the image")
    return (f" Average colour of {where}: {_hex(mean)} (sRGB), lightness L* {L:.0f} of 100; "
            "near-black is roughly below 20.")


# Images smaller than this on their longest side are enlarged with nearest-pixel
# sampling, so a test render at game size (say 48 px) can be seen pixel for pixel.
SMALL_IMAGE_EDGE = 256


# Teal runs to 205: under Filmic a teal glow drifts toward cyan (about 180-200 deg).
_HUE_NAMES = ((15, "red"), (45, "orange"), (70, "yellow"), (150, "green"), (205, "teal"),
              (245, "blue"), (290, "violet"), (330, "magenta"), (345, "pink"), (361, "red"))


def _hsv(rgb):
    """(hue in degrees, saturation 0-1, value 0-1) of an sRGB colour given as 0-1 floats."""
    import colorsys

    h, s, v = colorsys.rgb_to_hsv(*(min(1.0, max(0.0, float(c))) for c in rgb[:3]))
    return h * 360.0, s, v


def _hue_name(hue, sat):
    if sat < 0.15:
        return "grey or white"
    return next(name for top, name in _HUE_NAMES if hue < top)


def _sat_word(sat):
    # HSV saturation, not channel spread: a bright pale colour has a large spread but low
    # saturation. Calibrated so pale lavender (#b1a2ef, 32%) reads pale and a glow colour
    # such as #9e47ff (72%) reads saturated.
    if sat < 0.15:
        return "nearly white or grey"
    if sat < 0.4:
        return "pale"
    return "fairly saturated" if sat < 0.65 else "saturated"


def _hsv_words(rgb):
    hue, sat, val = _hsv(rgb)
    return (f"hue {hue:.0f}deg ({_hue_name(hue, sat)}), saturation {sat * 100:.0f}% "
            f"({_sat_word(sat)}), value {val * 100:.0f}%")


def _parse_hex(text):
    t = text.strip().lstrip("#")
    return [int(t[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def _bright_summary(img, target=None):
    """One sentence with the average colour of the brightest tenth of an image's pixels
    (by HSV value): the hue a glowing part really renders at, which the overall average
    dilutes. Crop tightly, or pale surfaces next to the glow count too. With target (a hex
    colour), also how far the brightest pixels are from it."""
    import numpy as np

    w, h = img.size
    ch = img.channels
    if ch < 3:
        return ""
    px = np.empty(w * h * ch, dtype=np.float32)
    img.pixels.foreach_get(px)
    px = px.reshape(-1, ch)
    rgb = _srgb(px[:, :3]) if img.is_float else px[:, :3]
    if ch == 4:
        rgb = rgb[px[:, 3] > 0.5]
    if not len(rgb):
        return ""
    value = rgb.max(axis=1)  # HSV value: a saturated glow is bright here, unlike luma
    k = max(1, len(rgb) // 10)
    top = rgb[np.argsort(value)[-k:]].mean(axis=0)
    text = f" Brightest tenth of the pixels averages {_hex(top)} (sRGB: {_hsv_words(top)})."
    if target:
        goal = _parse_hex(target)
        h0, s0, v0 = _hsv(top)
        h1, s1, v1 = _hsv(goal)
        dh = (h0 - h1 + 180) % 360 - 180

        def diff(d, unit, up, down, same):
            n = round(abs(d))
            return same if n == 0 else f"{n}{unit} {up if d > 0 else down}"

        text += (f" Target {_hex(goal)}: {_hsv_words(goal)}. The brightest pixels are "
                 f"{diff(dh, 'deg', 'higher in hue', 'lower in hue', 'the same hue')}, "
                 f"{diff((s0 - s1) * 100, ' points', 'more saturated', 'less saturated', 'as saturated')} and "
                 f"{diff((v0 - v1) * 100, ' points', 'brighter', 'darker', 'as bright')}.")
    return text


def _crop_image(img, crop):
    """Crop a loaded image in place to crop = [x0, y0, x1, y1] (fractions, origin bottom
    left, as Blender stores pixels). Returns the crop's pixel box (x, y, w, h)."""
    import numpy as np

    w, h = img.size
    ch = img.channels
    x0, y0, x1, y1 = crop
    px0, py0 = min(w - 1, int(math.floor(x0 * w))), min(h - 1, int(math.floor(y0 * h)))
    px1, py1 = max(px0 + 1, int(math.ceil(x1 * w))), max(py0 + 1, int(math.ceil(y1 * h)))
    px = np.empty(w * h * ch, dtype=np.float32)
    img.pixels.foreach_get(px)
    part = px.reshape(h, w, ch)[py0:py1, px0:px1].copy()
    cw, chh = px1 - px0, py1 - py0
    img.scale(cw, chh)
    img.pixels.foreach_set(part.ravel())
    return px0, py0, cw, chh


def _png_blocks(src, out, caption, stats=False, crop=None, target=None):
    """Load the image at src, shrink it to MAX_IMAGE_EDGE (or enlarge a small one with
    nearest-pixel sampling), write it to out as PNG and return it as tool-result blocks.
    With stats, the caption adds the average colour. With crop ([x0, y0, x1, y1] as
    fractions, origin bottom left) only that part is shown and measured. src is never
    modified unless it equals out."""
    img = bpy.data.images.load(src, check_existing=False)
    try:
        w, h = img.size
        if not w or not h:
            raise RuntimeError(f"Could not read image data from {src}.")
        if crop:
            fw, fh = w, h
            cx, cy, w, h = _crop_image(img, crop)
            caption += (f", cropped to {w}x{h} px at x {cx}-{cx + w}, y {cy}-{cy + h} "
                        f"(from the bottom left) of {fw}x{fh}")
        black = _mostly_black(img)
        summary = _colour_summary(img) if stats else ""
        if stats and (crop or target):
            summary += _bright_summary(img, target)
        longest = max(w, h)
        if longest > MAX_IMAGE_EDGE:
            s = MAX_IMAGE_EDGE / longest
            img.scale(max(1, round(w * s)), max(1, round(h * s)))
        elif longest < SMALL_IMAGE_EDGE:
            import numpy as np

            f = max(2, (2 * SMALL_IMAGE_EDGE) // longest)
            ch = img.channels
            px = np.empty(w * h * ch, dtype=np.float32)
            img.pixels.foreach_get(px)
            big = px.reshape(h, w, ch).repeat(f, axis=0).repeat(f, axis=1)
            img.scale(w * f, h * f)
            img.pixels.foreach_set(big.ravel())
        img.filepath_raw = out
        img.file_format = "PNG"
        img.save()
        sw, sh = img.size
    finally:
        bpy.data.images.remove(img)

    with open(out, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("ascii")
    text = f"{caption} ({w}x{h}"
    if (sw, sh) == (w, h):
        text += ")."
    elif sw > w:
        text += f", enlarged {sw // w}x with nearest-pixel sampling to {sw}x{sh})."
    else:
        text += f", shown at {sw}x{sh})."
    if black:
        text += " Note: the image is almost entirely black."
    text += summary
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
        {"type": "text", "text": text},
    ]


# (pixel digest, view/scene fingerprint) of the last screenshot that was returned.
_LAST_SHOT = [None, None]


def _view_fingerprint(ov):
    """What the viewport should show: its view, shading and the visible objects. Only
    used to tell a stale screenshot from a scene that really hasn't changed."""
    parts = []
    space = ov["area"].spaces.active
    r3d = getattr(space, "region_3d", None)
    if r3d is not None:
        parts.append(tuple(round(x, 5) for row in r3d.view_matrix for x in row))
        parts.append(r3d.view_perspective)
    with contextlib.suppress(AttributeError):
        parts.append(space.shading.type)
    parts.append(bpy.context.scene.frame_current)
    for o in bpy.context.view_layer.objects:
        if not o.visible_get():
            continue
        data = o.data
        n = len(data.vertices) if o.type == "MESH" and data is not None else 0
        parts.append((o.name, n, tuple(round(x, 5) for row in o.matrix_world for x in row)))
    return hash(tuple(parts))


def capture_viewport(args):
    import hashlib

    path = _temp_png("viewport")
    ov = view3d_override()
    if "area" not in ov:
        raise RuntimeError("No 3D Viewport is open to capture.")
    _remove_file(path)  # never return a stale capture from an earlier call
    note = ""
    err = None
    fp = _view_fingerprint(ov)
    try:
        # Changes made from a timer send no redraw notifiers, so draw the window now
        # rather than read whatever it last put on screen.
        with contextlib.suppress(Exception):
            ov["area"].tag_redraw()
            with bpy.context.temp_override(**ov):
                bpy.ops.wm.redraw_timer(type="DRAW_WIN_SWAP", iterations=1)
        with bpy.context.temp_override(**ov):
            bpy.ops.screen.screenshot_area(filepath=path)
        if os.path.exists(path):
            with open(path, "rb") as f:
                digest = hashlib.sha1(f.read()).hexdigest()
            stale = digest == _LAST_SHOT[0] and fp != _LAST_SHOT[1]
            if not stale and not _is_black_file(path):
                _LAST_SHOT[:] = [digest, fp]
                return _png_blocks(path, path, "Screenshot of the 3D Viewport")
            # Screenshots read the window's on-screen image, which can be black or left
            # over from an earlier draw when Blender is minimized, covered or not
            # redrawing. An offscreen draw doesn't depend on that.
            if stale:
                note = (
                    "The screenshot was pixel-identical to the previous one although the "
                    "view or scene has changed since (Blender's window is not redrawing), "
                    "so this is an offscreen render of the viewport instead, without "
                    "overlays."
                )
            else:
                note = (
                    "The screenshot came back black (Blender's window may be minimized or "
                    "covered), so this is an offscreen render of the viewport instead, "
                    "without overlays."
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
    crop = args.get("crop")
    out = _temp_png("image")
    _remove_file(out)
    path = args.get("path")
    if path:
        path = os.path.abspath(bpy.path.abspath(path))
        if not os.path.isfile(path):
            raise RuntimeError(f"No file at {path}.")
        return _png_blocks(path, out, f"Image file {path}", stats=True, crop=crop,
                       target=args.get("target"))

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
        vt = bpy.context.scene.view_settings.view_transform
        return _png_blocks(src, out, f"Image {name!r} (shown with view transform {vt})",
                           stats=True, crop=crop, target=args.get("target"))

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
    return _png_blocks(src, out, f"Image {name!r}", stats=True, crop=crop,
                       target=args.get("target"))


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
            cyc = any(m.type == "CYCLES" and not m.mute for m in fc.modifiers)
            found.append((f"{fc.data_path}[{fc.array_index}]", cyc))
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
    if args.get("collection"):
        objs = _named_meshes({"collection": args["collection"]})
    elif names:
        missing = [n for n in names if n not in bpy.data.objects]
        if missing:
            raise RuntimeError(
                f"No object named: {', '.join(missing)}. Pass collection to check every mesh "
                "in a collection, or leave out objects to use every visible mesh.")
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
        _nla_refresh(t for t, _ in saved_mutes)

    frames = sorted({start, start + 1, start + 2, start + 3, mid, end - 3, end - 2, end - 1, end}
                    | {start + (end - start) * k // 8 for k in range(1, 8)})
    # Frames whose deformed shape is compared: a skinned cycle such as a breath can be back
    # at its start pose exactly halfway, so the quarters are checked too.
    shape_frames = {start, start + (end - start) // 4, mid, start + 3 * (end - start) // 4, end}
    mats = {o.name: {} for o in objs}
    scales = {o.name: {} for o in objs}  # the object's own animated scale
    centres = {}  # middle of the object's geometry, in its local space
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
                scales[o.name][f] = ev.matrix_basis.to_scale()
                if f == start:
                    centres[o.name] = sum((Vector(c) for c in ev.bound_box), Vector()) / 8
                if f in shape_frames:
                    pts[(o.name, f)] = _world_points(ev)
    finally:
        for t, m in saved_mutes:
            t.mute = m
        _nla_refresh(t for t, _ in saved_mutes)
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
        for f in sorted(shape_frames - {start}):
            pf = pts[(n, f)]
            if moved:
                break
            if len(pf) == len(p0):
                moved = any((a - b).length > tiny for a, b in zip(pf, p0))
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
            which = "; ".join(f"{a} {', '.join(c for c, _ in ch)}" for a, ch in src)
            if all(cyc for _, ch in src for _, cyc in ch):
                fix = ("Those F-curves already have a Cycles modifier, so the handles are not the "
                       "cause: the keyed values themselves flatten or kink at the loop point. Move "
                       "the loop point to where everything moves steadily (for a generated cycle, "
                       "shift its phase, say by a quarter), or smooth the keys around it.")
            else:
                fix = ("Usually flat handles on the end keys: give those F-curves a Cycles "
                       "modifier so the handles wrap around the loop.")
            problems.append(
                f"STALL: moved by channels that slow almost to a stop at the loop point while "
                f"still moving the same way ({which}), so it pauses at every loop. {fix}")

        # Scaling around an origin far from the geometry also slides the geometry.
        c, s0 = centres[n], scales[n][start]
        ws = M[start].to_scale()
        to_world = Vector(tuple(w / s if abs(s) > 1e-9 else 0.0 for w, s in zip(ws, s0)))
        off = Vector(tuple(a * b * k for a, b, k in zip(c, s0, to_world))).length
        drift, drift_f = 0.0, start
        for f in frames:
            d = Vector(tuple(a * (sf - si) * k for a, sf, si, k in zip(c, scales[n][f], s0, to_world))).length
            if d > drift:
                drift, drift_f = d, f
        if drift > tol:
            problems.append(
                f"OFF-PIVOT SCALE: it scales around its origin, which is {_len(off)} from the "
                f"middle of its geometry, so the scaling also slides it (by {_len(drift)} at "
                f"frame {drift_f}). A pulsing light or part then bounces back and forth. Set "
                "its origin to the centre of its geometry, keeping it in place, before "
                "animating scale.")
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
    objs = _named_meshes(args)
    if objs is None:
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


# ---------------------------------------------------------------------------
# Measuring models and exported files
# ---------------------------------------------------------------------------


_TARGET_HINT = (
    " To check a model built as separate parts, pass collection (the collection's name) or "
    "root (an object whose hierarchy holds them); leave out root, collection and objects to "
    "use every visible mesh."
)


def _named_meshes(args):
    """Mesh objects named by args root, collection or objects (hidden ones included), or
    None when none of them is given."""
    root, coll, names = args.get("root"), args.get("collection"), args.get("objects")
    if root:
        if root not in bpy.data.objects:
            raise RuntimeError(f"No object named {root!r}." + _TARGET_HINT)
        r = bpy.data.objects[root]
        return [o for o in [r, *r.children_recursive] if o.type == "MESH"]
    if coll:
        scene = bpy.context.scene
        c = scene.collection if coll == scene.collection.name else bpy.data.collections.get(coll)
        if c is None:
            known = ", ".join(repr(x.name) for x in list(bpy.data.collections)[:20])
            raise RuntimeError(f"No collection named {coll!r}. Collections: {known or 'none'}.")
        return [o for o in c.all_objects if o.type == "MESH"]
    if names:
        missing = [n for n in names if n not in bpy.data.objects]
        if missing:
            raise RuntimeError(f"No object named: {', '.join(missing)}." + _TARGET_HINT)
        return [bpy.data.objects[n] for n in names if bpy.data.objects[n].type == "MESH"]
    return None


def _target_meshes(args, include_hidden=False):
    """Visible mesh objects named by args root/collection/objects (or all visible meshes),
    and how many hidden ones were left out. include_hidden keeps hidden ones (for a reference)."""
    objs = _named_meshes(args)
    if objs is None:
        objs = [o for o in bpy.context.view_layer.objects if o is not None and o.type == "MESH"]
    shown = objs if include_hidden else [o for o in objs if o.visible_get()]
    if not shown:
        raise RuntimeError("No visible mesh objects to check.")
    return shown, len(objs) - len(shown)


def _eval_mesh(ev, tris=False, faces=False):
    """World-space vertex positions (n x 3 numpy array) of an evaluated object, and its
    triangle count if asked. With faces=True also returns its faces as arrays of vertex
    indices."""
    import numpy as np

    me = ev.to_mesh()
    try:
        co = np.empty(len(me.vertices) * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        n_tris = 0
        if tris:
            me.calc_loop_triangles()
            n_tris = len(me.loop_triangles)
        face_list = None
        if faces:
            lv = np.empty(len(me.loops), dtype=np.int64)
            me.loops.foreach_get("vertex_index", lv)
            ls = np.empty(len(me.polygons), dtype=np.int64)
            lt = np.empty(len(me.polygons), dtype=np.int64)
            me.polygons.foreach_get("loop_start", ls)
            me.polygons.foreach_get("loop_total", lt)
            face_list = [lv[s:s + t] for s, t in zip(ls, lt)]
    finally:
        ev.to_mesh_clear()
    M = np.array(ev.matrix_world, dtype=np.float64)
    P = co.reshape(-1, 3).astype(np.float64) @ M[:3, :3].T + M[:3, 3]
    if faces:
        return P, n_tris, face_list
    return P, n_tris


# The main body is found slice by slice. The surface is sampled, cut into horizontal
# slices, and each slice is drawn onto a grid seen from above. A patch of touching cells
# counts as body if it spans at least this share of the model's longer side; antennas,
# fangs, rails and masts make small patches and are left out, welded on or not.
BODY_SPAN_SHARE = 0.15
_BODY_SLICES = 48
_BODY_GRID = 50  # cells along the model's longer side


def _triangles(faces):
    """k x 3 vertex-index array from faces (sequences of vertex indices), fan-split."""
    import numpy as np

    tris = []
    for f in faces:
        f = np.asarray(f, dtype=np.int64)
        if len(f) == 3:
            tris.append(f[None])
        elif len(f) > 3:
            tris.append(np.c_[np.full(len(f) - 2, f[0]), f[1:-1], f[2:]])
    return np.concatenate(tris) if tris else np.zeros((0, 3), dtype=np.int64)


def _extent_stats(P, faces=None):
    """Bounds of an n x 3 point array, plus the z range of its main body (see
    BODY_SPAN_SHARE). main_lo/main_hi equal the full range when faces aren't known."""
    import numpy as np

    lo, hi = P.min(axis=0), P.max(axis=0)
    st = {"lo": lo, "hi": hi, "main_lo": float(lo[2]), "main_hi": float(hi[2])}
    size = hi - lo
    H, side = float(size[2]), float(max(size[0], size[1]))
    if not faces or H <= 1e-9 or side <= 1e-9:
        return st
    T = _triangles(faces)
    if not len(T):
        return st
    A, B, Cc = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(B - A, Cc - A), axis=1)
    total = float(area.sum())
    if total <= 0:
        return st
    cell = side / _BODY_GRID
    n = int(min(200000, max(30000, 16 * total / cell ** 2)))
    rng = np.random.default_rng(0)
    pick = rng.choice(len(T), size=n, p=area / total)
    r1, r2 = np.sqrt(rng.random(n)), rng.random(n)
    S = ((1 - r1)[:, None] * A[pick] + (r1 * (1 - r2))[:, None] * B[pick]
         + (r1 * r2)[:, None] * Cc[pick])
    S = np.vstack([S, P])  # the vertices too, so tips and edges are always present
    gx = np.floor((S[:, 0] - lo[0]) / cell).astype(np.int64).tolist()
    gy = np.floor((S[:, 1] - lo[1]) / cell).astype(np.int64).tolist()
    gz = np.minimum(((S[:, 2] - lo[2]) / H * _BODY_SLICES).astype(np.int64), _BODY_SLICES - 1)
    need = BODY_SPAN_SHARE * _BODY_GRID
    body = np.zeros(len(S), dtype=bool)
    order = np.argsort(gz, kind="stable")
    bounds = np.searchsorted(gz[order], np.arange(_BODY_SLICES + 1))
    for z in range(_BODY_SLICES):
        ids = order[bounds[z]:bounds[z + 1]]
        if not len(ids):
            continue
        cells = {(gx[i], gy[i]) for i in ids}
        flag = {}
        for c in cells:
            if c in flag:
                continue
            flag[c] = None
            stack, members = [c], [c]
            while stack:  # 8-connected patch of occupied cells
                x, y = stack.pop()
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nb = (x + dx, y + dy)
                        if nb in cells and nb not in flag:
                            flag[nb] = None
                            stack.append(nb)
                            members.append(nb)
            xs = [m[0] for m in members]
            ys = [m[1] for m in members]
            big = max(max(xs) - min(xs), max(ys) - min(ys)) + 1 >= need
            for m in members:
                flag[m] = big
        body[ids] = [flag[(gx[i], gy[i])] for i in ids]
    if body.any():
        z = S[body, 2]
        st["main_lo"], st["main_hi"] = float(z.min()), float(z.max())
    return st


def _ranges(frames):
    """[0, 1, 2, 5, 9, 10] -> '0-2, 5, 9-10'."""
    out, run = [], []
    for f in frames:
        if run and f == run[-1] + 1:
            run.append(f)
        else:
            if run:
                out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = [f]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


def _offset_words(x, y):
    """'37.6 cm behind (+Y) and 2.0 cm to its left (+X)' for an X/Y offset from the origin
    of a model that faces -Y (its left is +X)."""
    out = []
    if abs(y) >= 0.005:
        out.append(f"{_len(abs(y))} {'behind' if y > 0 else 'in front of'} the origin "
                   f"({'+' if y > 0 else '-'}Y)")
    if abs(x) >= 0.005:
        out.append(f"{_len(abs(x))} to the model's {'left' if x > 0 else 'right'} "
                   f"({'+' if x > 0 else '-'}X)")
    return " and ".join(out) or "on the origin"


def _centre_lines(P, lo, hi, info=None):
    """Lines flagging a model whose bounding box, or the points it stands on, are off the
    origin in X/Y (a game places a unit by its origin)."""
    size = hi - lo
    side = float(max(size[0], size[1]))
    H = float(size[2])
    if side <= 1e-6:
        return []
    limit = max(0.01, 0.05 * side)
    cx, cy = float(lo[0] + hi[0]) / 2, float(lo[1] + hi[1]) / 2
    contact = P[P[:, 2] <= lo[2] + max(0.005, 0.005 * H)]
    fx = fy = None
    if len(contact) > 1:
        clo, chi = contact.min(axis=0), contact.max(axis=0)
        fx, fy = float(clo[0] + chi[0]) / 2, float(clo[1] + chi[1]) / 2
    off_box = math.hypot(cx, cy) > limit
    off_feet = fx is not None and math.hypot(fx, fy) > limit
    if not off_box and not off_feet:
        return ["Centred: the bounding box and the points it stands on are within "
                f"{_len(limit)} of the origin in X and Y."]
    lines = []
    if off_box:
        lines.append(f"NOT CENTRED: the bounding-box centre is {_offset_words(cx, cy)}.")
    else:
        lines.append(f"The bounding box is centred (within {_len(limit)}).")
    if fx is not None:
        lines.append(f"The points it stands on (its lowest {_len(max(0.005, 0.005 * H))}) "
                     f"have their centre {_offset_words(fx, fy)}.")
    lines.append(
        "A game places a unit by its origin, so if the spec asks for the model centred, "
        "move it (the root object or armature, not mesh data inside an animated hierarchy) "
        "and say whether you centred the bounding box or the footprint.")
    if info is not None:
        info.update(centre_lines=lines, centre_box=(cx, cy), centre_side=side,
                    centre_feet=None if fx is None else (fx, fy))
    return lines


def _describe_extent(P, labels, height, ground, faces=None, stats=None):
    """Report lines for an n x 3 array of world points (Blender axes, Z up, front -Y).
    labels[i] names the part point i belongs to; faces index into P. If stats
    is a dict, the measurements are stored in it (for comparing with a reference)."""
    import numpy as np

    st = _extent_stats(P, faces)
    if stats is not None:
        stats.update(st)
    lo, hi = st["lo"], st["hi"]
    size = hi - lo
    H = float(size[2])
    top = int(P[:, 2].argmax())
    low = int(P[:, 2].argmin())
    lines = [
        f"Height (Z): {_len(H)}, z {lo[2]:.3f} to {hi[2]:.3f}.",
        f"Width (X): {_len(float(size[0]))}, x {lo[0]:.3f} to {hi[0]:.3f}. "
        f"Depth (Y, front is -Y): {_len(float(size[1]))}, y {lo[1]:.3f} to {hi[1]:.3f}.",
        f"Bounding-box centre: ({(lo[0] + hi[0]) / 2:.3f}, {(lo[1] + hi[1]) / 2:.3f}, "
        f"{(lo[2] + hi[2]) / 2:.3f}).",
    ]
    lines += _centre_lines(P, lo, hi, stats)
    gap = float(lo[2]) - ground
    if abs(gap) <= max(1e-4, H * 1e-3):
        lines.append(f"Lowest point is on the ground (z = {ground:g}).")
    else:
        lines.append(f"Lowest point is {_len(abs(gap))} {'above' if gap > 0 else 'below'} "
                     f"z = {ground:g}.")
    lp = P[low]
    lines.append(f"Lowest point: {labels[low]} at ({lp[0]:.3f}, {lp[1]:.3f}, {lp[2]:.3f}).")
    tol = max(1e-4, 0.02 * H)
    below = st["main_lo"] - float(lo[2])
    above = float(hi[2]) - st["main_hi"]
    if below > tol or above > tol:
        lines.append(
            f"Main body (leaving out thin parts such as antennas, masts, fangs or rails, welded on or not): "
            f"z {st['main_lo']:.3f} to {st['main_hi']:.3f}, "
            f"height {_len(st['main_hi'] - st['main_lo'])}.")
        if below > tol:
            lines.append(
                f"Thin parts reach {_len(below)} below the main body, so they set the "
                "lowest point: a game that puts the lowest point on the ground (or hovers "
                "the model from it) lifts the main body by that much.")
        if above > tol:
            lines.append(f"Thin parts reach {_len(above)} above the main body.")
    tp = P[top]
    lines.append(f"Highest point: {labels[top]} at ({tp[0]:.3f}, {tp[1]:.3f}, {tp[2]:.3f}).")
    # What sets the height: if only a thin piece reaches the top 5%, say so.
    band = P[:, 2] >= hi[2] - 0.05 * H
    bp = P[band]
    bw = bp[:, :2].max(axis=0) - bp[:, :2].min(axis=0)
    parts = sorted({labels[i] for i in np.nonzero(band)[0]})
    lines.append(
        f"Top 5% of the height (above z {hi[2] - 0.05 * H:.3f}) is reached by "
        f"{', '.join(parts[:6])}{' ...' if len(parts) > 6 else ''}, spanning "
        f"{_len(float(bw[0]))} x {_len(float(bw[1]))}"
        + (" - a thin piece sets the height." if max(bw) < 0.15 * max(size[0], size[1]) else "."))
    if height:
        r = H / height
        lines.append(
            f"Target height {height:g}: the model is {abs(r - 1) * 100:.1f}% "
            f"{'taller' if r > 1 else 'shorter'}, so a game that scales it by its bounds to "
            f"{height:g} draws it at {1 / r:.3f}x.")
    return lines


def _gltf_load(path):
    """(json, [buffer bytes]) for a .glb or .gltf file."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"glTF":
        import struct

        pos, js, bins = 12, None, []
        while pos + 8 <= len(data):
            clen, ctype = struct.unpack_from("<II", data, pos)
            chunk = data[pos + 8:pos + 8 + clen]
            if ctype == 0x4E4F534A:
                js = json.loads(chunk)
            elif ctype == 0x004E4942:
                bins.append(chunk)
            pos += 8 + clen
        if js is None:
            raise RuntimeError("No JSON chunk in the .glb.")
    else:
        js, bins = json.loads(data), []
    bufs = []
    for i, b in enumerate(js.get("buffers", [])):
        uri = b.get("uri")
        if uri is None:
            bufs.append(bins[0] if bins else b"")
        elif uri.startswith("data:"):
            bufs.append(base64.b64decode(uri.split(",", 1)[1]))
        else:
            with open(os.path.join(os.path.dirname(path), uri), "rb") as f:
                bufs.append(f.read())
    return js, bufs


_GL_TYPES = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2", 5125: "u4", 5126: "f4"}
_GL_COMPONENTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _gltf_accessor(js, bufs, i):
    """An accessor as a float64 numpy array (count x components), or None if its data
    isn't plainly readable (compressed, sparse or an unusual layout)."""
    import numpy as np

    a = js["accessors"][i]
    if "bufferView" not in a or "sparse" in a or a["type"] not in _GL_COMPONENTS:
        return None
    bv = js["bufferViews"][a["bufferView"]]
    dt = np.dtype("<" + _GL_TYPES[a["componentType"]])
    nc = _GL_COMPONENTS[a["type"]]
    stride = bv.get("byteStride") or dt.itemsize * nc
    off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    arr = np.ndarray((a["count"], nc), dtype=dt, buffer=bufs[bv["buffer"]], offset=off,
                     strides=(stride, dt.itemsize)).astype(np.float64)
    if a.get("normalized") and dt.kind in "iu":
        arr = np.maximum(arr / np.iinfo(dt).max, -1.0)
    return arr


def _gltf_local(n):
    import numpy as np

    if "matrix" in n:
        return np.array(n["matrix"], dtype=np.float64).reshape(4, 4).T
    x, y, z, w = n.get("rotation", (0, 0, 0, 1))
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    M = np.eye(4)
    M[:3, :3] = R * np.array(n.get("scale", (1, 1, 1)))
    M[:3, 3] = n.get("translation", (0, 0, 0))
    return M


def _png_size(js, bufs, img):
    if "bufferView" not in img:
        return None
    bv = js["bufferViews"][img["bufferView"]]
    head = bufs[bv["buffer"]][bv.get("byteOffset", 0):bv.get("byteOffset", 0) + 24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    import struct

    return struct.unpack(">II", head[16:24])


def _measure_gltf(path, height, stats=None):
    import numpy as np

    js, bufs = _gltf_load(path)
    nodes = js.get("nodes", [])
    scenes = js.get("scenes") or [{"nodes": list(range(len(nodes)))}]
    roots = scenes[js.get("scene", 0)].get("nodes", [])
    world = {}

    def walk(i, parent):
        world[i] = parent @ _gltf_local(nodes[i])
        for c in nodes[i].get("children", []):
            walk(c, world[i])

    for r in roots:
        walk(r, np.eye(4))

    mats = js.get("materials", [])
    pts, labels, notes = [], [], []
    faces, nverts = [], 0
    tris = 0
    prims = 0
    colour_prims = 0
    for ni, W in world.items():
        n = nodes[ni]
        if "mesh" not in n:
            continue
        mesh = js["meshes"][n["mesh"]]
        mname = mesh.get("name") or n.get("name") or f"mesh {n['mesh']}"
        skin = js["skins"][n["skin"]] if "skin" in n else None
        joint_mats = None
        if skin:
            ibm = (_gltf_accessor(js, bufs, skin["inverseBindMatrices"])
                   if "inverseBindMatrices" in skin else None)
            joint_mats = []
            for k, j in enumerate(skin["joints"]):
                B = ibm[k].reshape(4, 4).T if ibm is not None else np.eye(4)
                joint_mats.append(world.get(j, np.eye(4)) @ B)
            joint_mats = np.array(joint_mats)
        for p in mesh.get("primitives", []):
            prims += 1
            attrs = p.get("attributes", {})
            if any(k.startswith("COLOR_") for k in attrs):
                colour_prims += 1
            mode = p.get("mode", 4)
            cnt = (js["accessors"][p["indices"]]["count"] if "indices" in p
                   else js["accessors"][attrs["POSITION"]]["count"])
            tris += cnt // 3 if mode == 4 else max(cnt - 2, 0) if mode in (5, 6) else 0
            label = mname + (f" / {mats[p['material']].get('name', p['material'])}"
                             if "material" in p and p["material"] < len(mats) else "")
            pos = _gltf_accessor(js, bufs, attrs["POSITION"])
            if pos is None:  # compressed: fall back to the accessor's min/max box
                a = js["accessors"][attrs["POSITION"]]
                lo, hi = a.get("min"), a.get("max")
                if not lo or not hi:
                    notes.append(f"Couldn't read positions of {label}; left out.")
                    continue
                pos = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1])
                                for z in (lo[2], hi[2])], dtype=np.float64)
                notes.append(f"{label} is compressed; its size comes from its bounding box.")
                skin_here = False
            else:
                skin_here = skin is not None and "JOINTS_0" in attrs and "WEIGHTS_0" in attrs
                idx = _gltf_accessor(js, bufs, p["indices"]) if "indices" in p else None
                idx = (idx.ravel().astype(np.int64) if idx is not None
                       else np.arange(len(pos)) if "indices" not in p else None)
                if idx is not None and mode == 4:
                    faces.extend(idx[:len(idx) // 3 * 3].reshape(-1, 3) + nverts)
            ph = np.c_[pos, np.ones(len(pos))]
            if skin_here:
                J = _gltf_accessor(js, bufs, attrs["JOINTS_0"]).astype(int)
                Wt = _gltf_accessor(js, bufs, attrs["WEIGHTS_0"])
                out = np.zeros_like(ph)
                for k in range(4):
                    out += Wt[:, k:k + 1] * np.einsum("nij,nj->ni", joint_mats[J[:, k]], ph)
                wp = out[:, :3]
            elif skin is not None:
                wp = (ph @ joint_mats[0].T)[:, :3]
            else:
                wp = (ph @ W.T)[:, :3]
            # glTF is Y up with the front at +Z; report in Blender's axes.
            pts.append(np.c_[wp[:, 0], -wp[:, 2], wp[:, 1]])
            labels += [label] * len(wp)
            nverts += len(wp)
    if not pts:
        raise RuntimeError("No mesh geometry found in the file.")
    P = np.concatenate(pts)

    lines = [f"{os.path.basename(path)}: {tris:,} triangles in {prims} primitives "
             f"({len(js.get('meshes', []))} meshes, {len(mats)} materials). Measured at the "
             "rest pose, in Blender's axes (Z up, front -Y); the file itself stores Y up."]
    lines += _describe_extent(P, labels, height, 0.0, faces, stats)
    if js.get("skins"):
        lines.append("Skins: " + ", ".join(f"{len(s['joints'])} joints" for s in js["skins"]) + ".")
    anims = js.get("animations", [])
    if anims:
        lines.append("Animations:")
        names = [a.get("name", "(unnamed)") for a in anims]
        for a, name in zip(anims, names):
            ins = [js["accessors"][s["input"]] for s in a.get("samplers", [])]
            t0 = min((x["min"][0] for x in ins if x.get("min")), default=0.0)
            t1 = max((x["max"][0] for x in ins if x.get("max")), default=0.0)
            warn = f" - starts at {t0:.3f} s, not 0" if t0 > 1e-4 else ""
            lines.append(f"- {name!r}: {t0:.3f} to {t1:.3f} s, {len(a.get('channels', []))} "
                         f"channels{warn}")
        dup = sorted({x for x in names if names.count(x) > 1})
        if dup:
            lines.append(f"Duplicate animation names: {', '.join(dup)}.")
        lines += _clip_gaps(js, anims, names)
    else:
        lines.append("Animations: none.")
    imgs = js.get("images", [])
    if imgs:
        desc = []
        for im in imgs:
            sz = _png_size(js, bufs, im)
            desc.append(f"{im.get('name', '?')} ({sz[0]}x{sz[1]} PNG)" if sz
                        else f"{im.get('name', '?')} ({im.get('mimeType', im.get('uri', '?'))})")
        lines.append("Images: " + ", ".join(desc) + ".")
    if any(s.get("magFilter") == 9728 for s in js.get("samplers", [])):
        lines.append("A texture sampler uses nearest-pixel filtering (Closest in Blender).")
    if colour_prims:
        lines.append(f"Vertex colours (COLOR_0) on {colour_prims} of {prims} primitives; game "
                     "engines may multiply them into the base colour.")
    cams = sum(1 for n in nodes if "camera" in n)
    lights = sum(1 for n in nodes if "KHR_lights_punctual" in n.get("extensions", {}))
    if cams or lights:
        lines.append(f"Also contains {cams} cameras and {lights} lights.")
    lines += [f"Note: {x}" for x in notes[:10]]
    return "\n".join(lines)


def _clip_gaps(js, anims, names):
    """Warn about channels some clips key and others don't. A game engine playing a clip
    leaves channels it doesn't key at whatever the previous clip set, so a turret that
    sweeps in idle stays turned in walk."""
    if len(anims) < 2:
        return []
    import re

    nodes = js.get("nodes", [])
    materials = js.get("materials", [])

    def channel_key(target):
        if "node" in target:
            return ("node", target["node"], target.get("path", "?"))
        ptr = target.get("extensions", {}).get("KHR_animation_pointer", {}).get("pointer")
        return ("pointer", ptr, "") if ptr else None

    def describe(key):
        kind, ref, path = key
        if kind == "node":
            name = nodes[ref].get("name", f"node {ref}") if ref < len(nodes) else ref
            return f"{name} {path}"
        m = re.match(r"/materials/(\d+)/(.*)", ref)
        if m and int(m.group(1)) < len(materials):
            return f"material {materials[int(m.group(1))].get('name', m.group(1))} {m.group(2)}"
        return ref

    keyed = [{k for c in a.get("channels", []) if (k := channel_key(c.get("target", {})))}
             for a in anims]
    every = set().union(*keyed)
    out = []
    for name, k in zip(names, keyed):
        miss = sorted(every - k, key=lambda x: (x[0], str(x[1]), x[2]))
        if not miss:
            continue
        desc = ", ".join(describe(x) for x in miss[:6]) + (" ..." if len(miss) > 6 else "")
        fixes = []
        if any(x[0] == "node" for x in miss):
            fixes.append("for objects, export with export_optimize_animation_size=False and "
                         "export_optimize_animation_keep_anim_object=True")
        if any(x[0] == "pointer" for x in miss):
            fixes.append("for material channels, which the exporter drops when they stay "
                         "constant whatever the options, key a change too small to see "
                         "(a 0.5% breath across the clip)")
        out.append(f"{name!r} doesn't key {desc}, which other clips animate. When a game "
                   f"switches to {name!r}, those keep whatever the previous clip left; key them "
                   f"in every clip ({'; '.join(fixes)}).")
    return out


def _measure_scene(args, stats=None, include_hidden=False):
    import numpy as np

    objs, hidden = _target_meshes(args, include_hidden=include_hidden)
    scene = bpy.context.scene
    orig = (scene.frame_current, scene.frame_subframe)
    frame = args.get("frame")
    pts, labels, faces, tris, n = [], [], [], 0, 0
    try:
        if frame is not None:
            scene.frame_set(frame)
        dg = bpy.context.evaluated_depsgraph_get()
        for o in objs:
            p, t, f = _eval_mesh(o.evaluated_get(dg), tris=True, faces=True)
            tris += t
            if len(p):
                pts.append(p)
                labels += [o.name] * len(p)
                faces += [x + n for x in f]
                n += len(p)
    finally:
        scene.frame_set(orig[0], subframe=orig[1])
    if not pts:
        raise RuntimeError("The meshes have no vertices.")
    head = (f"{len(objs)} meshes, {tris:,} triangles (evaluated, modifiers applied), at frame "
            f"{frame if frame is not None else orig[0]}.")
    if hidden:
        head += f" {hidden} hidden meshes left out."
    P = np.concatenate(pts)
    return "\n".join([head] + _describe_extent(P, labels, args.get("height"),
                                                 float(args.get("ground", 0.0)),
                                                 faces, stats))


def _compare_lines(a, b, ref_name):
    """Side-by-side lines for two _extent_stats dicts (a = the model, b = the reference)."""
    sa, sb = a["hi"] - a["lo"], b["hi"] - b["lo"]
    rows = [  # (name, model, reference, compare as a ratio)
        ("Width (X)", float(sa[0]), float(sb[0]), True),
        ("Depth (Y)", float(sa[1]), float(sb[1]), True),
        ("Height (Z)", float(sa[2]), float(sb[2]), True),
        ("Main-body height", a["main_hi"] - a["main_lo"], b["main_hi"] - b["main_lo"], True),
        ("Main body above the lowest point", max(0.0, a["main_lo"] - float(a["lo"][2])),
         max(0.0, b["main_lo"] - float(b["lo"][2])), False),
        ("Thin parts above the main body", max(0.0, float(a["hi"][2]) - a["main_hi"]),
         max(0.0, float(b["hi"][2]) - b["main_hi"]), False),
    ]
    out = [f"Compared with the reference {ref_name} (model vs reference):"]
    for name, x, y, ratio in rows:
        if ratio and y > 1e-6:
            d = f"{(x / y - 1) * 100:+.1f}%"
        else:
            d = f"{'+' if x >= y else '-'}{_len(abs(x - y))}"
        out.append(f"- {name}: {_len(x)} vs {_len(y)} ({d})")
    ratio = lambda s, m: (m["main_hi"] - m["main_lo"]) / max(float(s[0]), float(s[1]), 1e-9)
    out.append(f"- Main-body height / widest side: {ratio(sa, a):.2f} vs {ratio(sb, b):.2f} "
               "(lower is squatter).")
    return out


def _same_offset_as_reference(text, a, b):
    """The model's report with its off-centre warning cut to one line when the reference is
    off the origin in the same way (a tank whose barrel overhangs, like the file it
    replaces). a and b are the stats of the model and the reference."""
    la, lb = a.get("centre_lines"), b.get("centre_lines")
    if not la or not lb:  # either one is centred, or wasn't measured
        return text
    tol = max(0.01, 0.02 * max(a["centre_side"], b["centre_side"]))
    pairs = [(a["centre_box"], b["centre_box"])]
    if (a["centre_feet"] is None) != (b["centre_feet"] is None):
        return text
    if a["centre_feet"] is not None:
        pairs.append((a["centre_feet"], b["centre_feet"]))
    if any(math.hypot(p[0] - q[0], p[1] - q[1]) > tol for p, q in pairs):
        return text
    line = (f"Off the origin in the same way as the reference (within {_len(tol)}): the "
            f"bounding-box centre is {_offset_words(*a['centre_box'])} (reference: "
            f"{_offset_words(*b['centre_box'])})")
    if a["centre_feet"] is not None:
        line += (f"; the points it stands on have their centre {_offset_words(*a['centre_feet'])} "
                 f"(reference: {_offset_words(*b['centre_feet'])})")
    return text.replace("\n".join(la), line + ".")


def measure_model(args):
    path, ref = args.get("path"), args.get("reference")
    mine, theirs = {}, {}
    if path:
        path = os.path.abspath(bpy.path.abspath(path))
        if not os.path.isfile(path):
            raise RuntimeError(f"No file at {path}.")
        text = _measure_gltf(path, args.get("height"), mine)
    else:
        text = _measure_scene(args, mine)
    if not ref:
        return text
    if ref.lower().endswith((".glb", ".gltf")):
        rpath = bpy.path.abspath(ref)
        if not os.path.isfile(rpath):
            raise RuntimeError(f"No reference file at {rpath}.")
        _measure_gltf(rpath, None, theirs)
        rname = os.path.basename(rpath) + " (rest pose)"
    else:
        if ref not in bpy.data.objects:
            raise RuntimeError(f"No reference object named {ref!r}. Pass an object name "
                               "(its hierarchy is measured, hidden or not) or a .glb path.")
        _measure_scene({"root": ref, "frame": args.get("frame")}, theirs, include_hidden=True)
        rname = repr(ref)
    return "\n".join([_same_offset_as_reference(text, mine, theirs)]
                     + _compare_lines(mine, theirs, rname))


def _nla_refresh(tracks):
    """Make the depsgraph notice NLA mute changes made from Python. Setting NlaTrack.mute
    doesn't rebuild its relations (Blender 5.2), so a track that was muted when they were
    built holds its frame-0 pose on every frame once unmuted. Adding and removing a track
    on the same object does rebuild them."""
    seen = set()
    for t in tracks:
        ob = t.id_data
        ad = getattr(ob, "animation_data", None)
        if ad is None or ob in seen:
            continue
        seen.add(ob)
        try:
            ad.nla_tracks.remove(ad.nla_tracks.new())
        except RuntimeError:
            pass  # for example in tweak mode


def _solo_nla(track, drivers):
    """Mute every NLA track not named track. Returns (saved mute states, notes)."""
    saved, notes, found = [], [], False
    for o in bpy.data.objects:
        ad = o.animation_data
        if not ad or not len(ad.nla_tracks):
            continue
        for t in ad.nla_tracks:
            saved.append((t, t.mute))
            t.mute = t.name != track
            found = found or t.name == track
        if ad.action and o in drivers:
            notes.append(f"{o.name} also has an active action ({ad.action.name}), which "
                         "plays on top of its NLA tracks.")
    if not found:
        for t, m in saved:
            t.mute = m
        raise RuntimeError(f"No NLA track named {track!r} on any object.")
    _nla_refresh(t for t, _ in saved)
    return saved, notes


def check_ground_contact(args):
    import numpy as np

    start, end = int(args["start"]), int(args["end"])
    ground = float(args.get("ground", 0.0))
    objs, _ = _target_meshes(args)
    scene = bpy.context.scene
    fps = scene.render.fps / (scene.render.fps_base or 1.0)
    track = args.get("nla_track")
    saved, notes = _solo_nla(track, _with_ancestors(objs)) if track else ([], [])
    orig = (scene.frame_current, scene.frame_subframe)
    frames = list(range(start, end + 1))
    data = []
    try:
        for f in frames:
            scene.frame_set(f)
            dg = bpy.context.evaluated_depsgraph_get()
            data.append(np.concatenate([_eval_mesh(o.evaluated_get(dg))[0] for o in objs]))
    finally:
        for t, m in saved:
            t.mute = m
        _nla_refresh(t for t, _ in saved)
        scene.frame_set(orig[0], subframe=orig[1])

    P0 = data[0]
    frozen = len(data) > 1 and all(
        len(p) == len(P0) and float(np.abs(p - P0).max()) < 1e-6 for p in data[1:])
    H = float(P0[:, 2].max() - P0[:, 2].min())
    tol = float(args["tolerance"]) if args.get("tolerance") is not None else max(0.005, 0.005 * H)
    low = np.array([float(p[:, 2].min()) for p in data]) - ground

    head = f"Ground contact, frames {start}-{end}"
    head += f" (only NLA tracks named {track!r} playing)" if track else ""
    lines = [head + f", ground z = {ground:g}, touching within {_len(tol)}:"]
    if len(frames) <= 32:
        sample = frames
    else:
        sample = sorted({frames[round(i * (len(frames) - 1) / 23)] for i in range(24)})
    lines.append("Lowest point by frame: " + ", ".join(
        f"{f}: {'+' if low[f - start] >= 0 else '-'}{_len(abs(low[f - start]))}" for f in sample))

    if frozen:
        lines.append(
            "- NOT MOVING: the model is identical at every frame, so no animation played in "
            "this range and this is not a check of the clip. Check the track name, that the "
            "clip sits on these frames, and that the action or track is the one playing.")
    sink = [f for f in frames if low[f - start] < -tol]
    hover = [f for f in frames if low[f - start] > tol]
    if sink:
        k = int(low.argmin())
        p = data[k][int(data[k][:, 2].argmin())]
        lines.append(f"- SINKS below the ground at frames {_ranges(sink)}: up to "
                     f"{_len(-float(low[k]))} at frame {frames[k]}, near ({p[0]:.2f}, {p[1]:.2f}).")
    if hover:
        k = int(low.argmax())
        lines.append(f"- NOTHING TOUCHES the ground at frames {_ranges(hover)}: the lowest point "
                     f"is up to {_len(float(low[k]))} above it at frame {frames[k]}.")
    if not sink and not hover:
        lines.append("- Something touches the ground at every frame, and nothing sinks into it.")

    # How the points touching the ground move: tracked by vertex index between frames.
    if all(len(p) == len(P0) for p in data):
        speeds, vecs, step_frames = [], [], []
        for i in range(len(frames) - 1):
            a, b = data[i], data[i + 1]
            on = (np.abs(a[:, 2] - ground) <= tol) & (np.abs(b[:, 2] - ground) <= tol)
            if on.sum() < 3:
                continue
            d = b[on, :2] - a[on, :2]
            v = np.median(d, axis=0) * fps
            vecs.append(v)
            speeds.append(float(np.linalg.norm(v)))
            step_frames.append(frames[i])
        if speeds:
            sp = np.array(speeds)
            mv = np.mean(vecs, axis=0)
            med = float(np.median(sp))
            if frozen:
                pass
            elif med < tol * fps * 0.5:
                lines.append("- Points on the ground stay put while touching it (no sliding).")
            else:
                dirn = "backward (+Y)" if mv[1] > abs(mv[0]) else "forward (-Y)" if -mv[1] > abs(mv[0]) else "sideways"
                lines.append(
                    f"- Points on the ground move {dirn} at a median {med:.2f} m/s while "
                    f"touching it (range {sp.min():.2f} to {sp.max():.2f} m/s over "
                    f"{len(sp)} frame steps). In a cycle made in place this is the walking "
                    f"speed: the game must move the model at about {med:.2f} m/s for the feet "
                    "to stay put, so tell the user this figure. For a cycle that moves "
                    "through the scene, planted points should stay put instead.")
                if sp.max() > 1.5 * med or sp.min() < 0.6 * med:
                    slow = [f for f, s in zip(step_frames, speeds) if s < 0.6 * med]
                    fast = [f for f, s in zip(step_frames, speeds) if s > 1.5 * med]
                    where = "; ".join(x for x in (
                        f"slow from frames {_ranges(slow)}" if slow else "",
                        f"fast from frames {_ranges(fast)}" if fast else "") if x)
                    lines.append(
                        f"- UNEVEN SPEED ({where}): moved at a steady speed, the feet SLIDE "
                        "(forward where the planted foot is slow, backward where it is fast). "
                        "Make the stance-leg swing even, for example with linear "
                        "interpolation on the thigh keys while the foot is down.")
        else:
            lines.append("- No points stay on the ground between two frames in a row.")
    else:
        lines.append("Note: the vertex count changes between frames, so sliding isn't checked.")
    lines += [f"Note: {x}" for x in notes]
    return "\n".join(lines)


MAP_MARGIN_PX = 3  # empty pixels around the model in a map test
MAP_VIEW_GAP_PX = 4  # map colour between the views when several azimuths are rendered

# Every kind of datablock the glTF importer can create (a multi-scene file adds scenes,
# POINTS primitives add point clouds). Kinds this Blender lacks are skipped when the tool
# runs (bpy.data is restricted while the add-on loads, so not here).
_IMPORT_KINDS = ("objects", "meshes", "materials", "images", "armatures", "actions",
                 "collections", "textures", "node_groups", "cameras", "lights", "curves",
                 "scenes", "pointclouds", "worlds")


def _import_gltf_temp(path):
    """Import a .glb/.gltf into the open scene, posed as the file stores it (its node
    transforms, which a game shows before any clip plays). Returns (mesh objects, undo):
    call undo() to remove everything the import added and restore what it changed."""
    scene = bpy.context.scene
    vl = bpy.context.view_layer
    before = {k: set(getattr(bpy.data, k)) for k in _IMPORT_KINDS if hasattr(bpy.data, k)}
    saved = (scene.frame_start, scene.frame_end, scene.frame_current, scene.render.fps,
             scene.render.fps_base, vl.active_layer_collection, vl.objects.active,
             [o for o in vl.objects if o.select_get()])

    def undo():
        for k in before:
            coll = getattr(bpy.data, k)
            new = [x for x in coll if x not in before[k]]
            if new:
                bpy.data.batch_remove(new)
        (scene.frame_start, scene.frame_end, frame, scene.render.fps,
         scene.render.fps_base, alc, act, sel) = saved
        with contextlib.suppress(Exception):
            vl.active_layer_collection = alc
        for o in vl.objects:
            o.select_set(o in sel)
        vl.objects.active = act
        scene.frame_set(frame)

    try:
        # The importer links into the active collection, which may be excluded or hidden
        # from renders; import into the scene's root collection instead (undo restores it).
        vl.active_layer_collection = vl.layer_collection
        # Bones built from the node transforms (not the inverse bind matrices), so a pose
        # reset to identity shows the file's own pose.
        # disable_bone_shape: otherwise the importer adds an icosphere mesh for bone display.
        bpy.ops.import_scene.gltf(filepath=path, guess_original_bind_pose=False,
                                  disable_bone_shape=True, import_select_created_objects=False)
        new_objs = [o for o in bpy.data.objects
                    if o not in before["objects"] and o.name in scene.objects]
        for o in new_objs:
            if o.animation_data:
                o.animation_data_clear()
            keys = getattr(o.data, "shape_keys", None)
            if keys and keys.animation_data:
                # Morph-target clips animate the shape keys, not the object.
                keys.animation_data_clear()
            if o.type == "ARMATURE":
                for pb in o.pose.bones:
                    pb.matrix_basis.identity()
        vl.update()
        meshes = [o for o in new_objs if o.type == "MESH"]
        if not meshes:
            raise RuntimeError(f"{os.path.basename(path)} has no meshes.")
        hidden = [o.name for o in meshes if o.name not in vl.objects or o.hide_render]
        if hidden:
            raise RuntimeError(f"Imported meshes are hidden from renders: {', '.join(hidden[:5])}.")
        return meshes, undo
    except Exception:
        undo()
        raise


def _dilate(mask, steps):
    """Grow a 2-D boolean mask by steps pixels (8-connected)."""
    import numpy as np

    out = mask.copy()
    for _ in range(steps):
        p = np.pad(out, 1)
        grown = np.zeros_like(out)
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                grown |= p[dy:dy + out.shape[0], dx:dx + out.shape[1]]
        out = grown
    return out


def render_map_test(args):
    glb = args.get("path")
    undo_import = None
    if glb:
        glb = os.path.abspath(bpy.path.abspath(glb))
        if not os.path.isfile(glb):
            raise RuntimeError(f"No file at {glb}.")
        objs, undo_import = _import_gltf_temp(glb)
    else:
        objs, _ = _target_meshes(args)
    try:
        return _map_test(args, objs, glb)
    finally:
        if undo_import:
            undo_import()


def _map_test(args, objs, glb):
    import numpy as np
    from mathutils import Matrix, Vector

    scene = bpy.context.scene
    rd = scene.render
    px_h = int(args["px_height"]) if args.get("px_height") else None
    px_w = int(args["px_width"]) if args.get("px_width") else None
    el = math.radians(float(args.get("elevation", 55)))
    az_degs = [float(v) for v in (args.get("azimuths") or [args.get("azimuth", 30)])]
    lighting = args.get("lighting") or "sun"
    outline = int(args.get("outline_px", 1))
    bg_hex = (args.get("background") or "#2a2b32").lstrip("#")
    bg = np.array([int(bg_hex[i:i + 2], 16) / 255 for i in (0, 2, 4)], dtype=np.float32)
    ink = np.array([0.04, 0.04, 0.05], dtype=np.float32)  # the outline colour

    orig_frame = (scene.frame_current, scene.frame_subframe)
    saved = {
        "camera": scene.camera, "res": (rd.resolution_x, rd.resolution_y, rd.resolution_percentage),
        "film": rd.film_transparent, "fmt": (rd.image_settings.file_format, rd.image_settings.color_mode),
        "border": rd.use_border, "pixel_aspect": (rd.pixel_aspect_x, rd.pixel_aspect_y),
        "view": (scene.view_settings.view_transform, scene.view_settings.look),
    }
    hidden = []  # objects whose hide_render this call switched on
    temp = []  # (object, data) created here
    paths = [_temp_png(f"maptest_raw{i}") for i in range(len(az_degs))]
    mask_paths = [_temp_png(f"maptest_mask{i}") for i in range(len(az_degs))]
    out = _temp_png("maptest")
    for p in paths + mask_paths + [out]:
        _remove_file(p)
    glow_mats = _emissive_materials(objs)
    unblack = []  # functions that undo the blacked-out glow materials
    try:
        vt = args.get("view_transform")
        if vt:
            try:
                scene.view_settings.view_transform = vt
            except TypeError as e:  # not one of this Blender's view transforms
                raise RuntimeError(f"Unknown view_transform {vt!r}: {e}")
        if args.get("frame") is not None:
            scene.frame_set(int(args["frame"]))
        dg = bpy.context.evaluated_depsgraph_get()
        P = np.concatenate([_eval_mesh(o.evaluated_get(dg))[0] for o in objs])
        if not len(P):
            raise RuntimeError("The meshes have no vertices.")
        lo, hi = P.min(axis=0), P.max(axis=0)
        H = float(hi[2] - lo[2])
        if H <= 1e-6:
            raise RuntimeError("The model has no height.")
        centre = Vector(((lo + hi) / 2).tolist())
        dist = float(np.linalg.norm(hi - lo)) * 2 + 10

        def direction(a, e):
            return Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)))

        # Each view's camera rotation, position and the model's camera-space x/y extent.
        extents = []
        for az_deg in az_degs:
            d = direction(math.radians(az_deg), el)
            rot = (-d).to_track_quat("-Z", "Y").to_matrix()
            cam_pos = centre + d * dist
            Q = (P - np.array(cam_pos)) @ np.array(rot, dtype=np.float64)  # camera x, y, -depth
            extents.append((rot, cam_pos, float(Q[:, 0].min()), float(Q[:, 0].max()),
                            float(Q[:, 1].min()), float(Q[:, 1].max())))
        screen_w = max(e[3] - e[2] for e in extents)
        if args.get("metres_per_px"):
            mpp = float(args["metres_per_px"])
        elif px_w:
            mpp = screen_w / px_w
        else:
            mpp = H / px_h

        # The tool's own sun, unless the scene's lights were asked for (and there are any).
        scene_lights = [o for o in scene.objects if o.type == "LIGHT" and not o.hide_render]
        sun = None
        if lighting == "sun" or not scene_lights:
            for o in scene_lights:
                o.hide_render = True
                hidden.append(o)
            ld = bpy.data.lights.new("claude_map_test_sun", "SUN")
            ld.energy = 3.0
            sun = bpy.data.objects.new("claude_map_test_sun", ld)
            temp.append((sun, ld))
            scene.collection.objects.link(sun)
            lit = ("the tool's own sun, 40 deg to the left of the camera"
                   + ("" if lighting == "sun" else " (the scene has no light that renders)"))
        else:
            kinds = ", ".join(sorted({o.data.type.lower() for o in scene_lights}))
            lit = (f"the scene's {len(scene_lights)} light{'s' if len(scene_lights) != 1 else ''} "
                   f"({kinds}); a side facing away from them is in shadow")

        keep = set(objs) | {t[0] for t in temp}
        for o in scene.objects:
            if o not in keep and o.type not in {"LIGHT", "CAMERA", "ARMATURE", "EMPTY"} and not o.hide_render:
                o.hide_render = True
                hidden.append(o)

        rd.resolution_percentage = 100
        rd.pixel_aspect_x = rd.pixel_aspect_y = 1
        rd.use_border = False
        rd.film_transparent = True
        rd.image_settings.file_format = "PNG"
        rd.image_settings.color_mode = "RGBA"

        # One orthographic camera per view, looking at the model from (azimuth, elevation),
        # framed on the model's camera-space extent.
        views = []
        for rot, cam_pos, x0, x1, y0, y1 in extents:
            W = int(math.ceil((x1 - x0) / mpp)) + 2 * MAP_MARGIN_PX
            Ht = int(math.ceil((y1 - y0) / mpp)) + 2 * MAP_MARGIN_PX
            views.append((rot, cam_pos + rot @ Vector(((x0 + x1) / 2, (y0 + y1) / 2, 0)), W, Ht))
        total_w = sum(v[2] for v in views) + MAP_VIEW_GAP_PX * (len(views) - 1)
        if max(total_w, max(v[3] for v in views)) > 1024:
            raise RuntimeError(f"The test would be {total_w}x{max(v[3] for v in views)} px; pass "
                               "a smaller px_height or px_width, a larger metres_per_px or fewer "
                               "azimuths.")
        shots = []
        for (rot, cam_pos, W, Ht), az_deg, path in zip(views, az_degs, paths):
            cd = bpy.data.cameras.new("claude_map_test_cam")
            cd.type = "ORTHO"
            cd.ortho_scale = max(W, Ht) * mpp
            cd.sensor_fit = "AUTO"
            cd.clip_start = 0.01
            cd.clip_end = dist * 3
            cam = bpy.data.objects.new("claude_map_test_cam", cd)
            temp.append((cam, cd))
            scene.collection.objects.link(cam)
            cam.matrix_world = Matrix.Translation(cam_pos) @ rot.to_4x4()
            sun_rot = None
            if sun is not None:  # the same light in every view: it turns with the camera
                to_sun = direction(math.radians(az_deg - 40), math.radians(50))
                sun_rot = to_sun.to_track_quat("Z", "Y").to_euler()
                sun.rotation_euler = sun_rot
            scene.camera = cam
            rd.resolution_x, rd.resolution_y = W, Ht
            bpy.ops.render.render(write_still=False)
            bpy.data.images["Render Result"].save_render(path)
            shots.append((cam, sun_rot, W, Ht))
        # The same views again with every emissive material black: the pixels that change
        # are the ones showing a glow, whatever its colour and however the view transform
        # washes it out.
        if glow_mats:
            unblack.extend(_black_out(m) for m in glow_mats)
            for (cam, sun_rot, W, Ht), path in zip(shots, mask_paths):
                if sun_rot is not None:
                    sun.rotation_euler = sun_rot
                scene.camera = cam
                rd.resolution_x, rd.resolution_y = W, Ht
                bpy.ops.render.render(write_still=False)
                bpy.data.images["Render Result"].save_render(path)
    finally:
        for undo in unblack:
            undo()
        scene.camera = saved["camera"]
        rd.resolution_x, rd.resolution_y, rd.resolution_percentage = saved["res"]
        rd.film_transparent = saved["film"]
        rd.image_settings.file_format, rd.image_settings.color_mode = saved["fmt"]
        rd.use_border = saved["border"]
        rd.pixel_aspect_x, rd.pixel_aspect_y = saved["pixel_aspect"]
        view_used = scene.view_settings.view_transform
        if (scene.view_settings.view_transform, scene.view_settings.look) != saved["view"]:
            scene.view_settings.view_transform = saved["view"][0]
            with contextlib.suppress(TypeError):
                scene.view_settings.look = saved["view"][1]
        for o in hidden:
            o.hide_render = False
        for o, data in temp:
            bpy.data.objects.remove(o)
            if isinstance(data, bpy.types.Camera):
                bpy.data.cameras.remove(data)
            else:
                bpy.data.lights.remove(data)
        scene.frame_set(orig_frame[0], subframe=orig_frame[1])

    comps, per_view, colorspace = [], [], None
    def read(path):
        if not os.path.isfile(path):
            raise RuntimeError("The render wrote no image.")
        img = bpy.data.images.load(path, check_existing=False)
        try:
            w, h = img.size
            ch = img.channels
            space = img.colorspace_settings.name
            px = np.empty(w * h * ch, dtype=np.float32)
            img.pixels.foreach_get(px)
        finally:
            bpy.data.images.remove(img)
        _remove_file(path)
        return px.reshape(h, w, ch), w, h, ch, space

    for path, mask_path, az_deg in zip(paths, mask_paths, az_degs):
        px, w, h, ch, colorspace = read(path)
        rgb = px[:, :, :3]
        a = px[:, :, 3] if ch == 4 else np.ones((h, w), dtype=np.float32)
        solid = a > 0.5
        under = np.broadcast_to(bg, rgb.shape).copy()
        if outline:
            ring = _dilate(solid, outline) & ~solid
            under[ring] = ink
        comp = rgb * a[..., None] + under * (1 - a[..., None])
        comps.append(comp)
        ys, xs = np.nonzero(solid)
        if len(xs):
            size = (f"the model covers {int(xs.max() - xs.min() + 1)} x "
                    f"{int(ys.max() - ys.min() + 1)} px (wide x tall) of the {w}x{h} image")
        else:
            size = "nothing rendered: the model is not visible from this camera"
        glow = None  # no emissive material
        if glow_mats:
            dark = read(mask_path)[0]
            glow = solid & (np.abs(rgb - dark[:, :, :3]).max(axis=2) > 0.04)
        per_view.append((az_deg, size, _glow_pixels(comp, solid, glow)))

    # The views side by side on the map colour, centred vertically.
    h = max(c.shape[0] for c in comps)
    cols = []
    for i, c in enumerate(comps):
        if i:
            cols.append(np.broadcast_to(bg, (h, MAP_VIEW_GAP_PX, 3)))
        pad = h - c.shape[0]
        if pad:
            below = np.broadcast_to(bg, (pad // 2, c.shape[1], 3))
            above = np.broadcast_to(bg, (pad - pad // 2, c.shape[1], 3))
            c = np.concatenate([below, c, above], axis=0)
        cols.append(c)
    strip = np.concatenate(cols, axis=1).astype(np.float32)
    w = strip.shape[1]
    flat = np.concatenate([strip, np.ones((h, w, 1), dtype=np.float32)], axis=2)
    res = bpy.data.images.new("claude_map_test", w, h, alpha=True)
    try:
        res.colorspace_settings.name = colorspace
        res.pixels.foreach_set(flat.ravel())
        res.filepath_raw = out
        res.file_format = "PNG"
        res.save()
    finally:
        bpy.data.images.remove(res)

    az_words = (f"azimuth {az_degs[0]:g} deg (0 = front)" if len(az_degs) == 1 else
                f"{len(az_degs)} views side by side, left to right at azimuth "
                + ", ".join(f"{v:g}" for v in az_degs) + " deg (0 = front)")
    if args.get("metres_per_px"):
        scale = "metres_per_px"
    elif px_w:
        scale = (f"model width on screen {_len(screen_w)} / {px_w} px"
                 + (", the widest view" if len(az_degs) > 1 else ""))
    else:
        scale = f"model height {_len(H)} / {px_h} px"
    note = (f"Map test: 1 px = {_len(mpp)} ({scale}), "
            f"orthographic, {math.degrees(el):g} deg above the horizon, {az_words}, "
            + (f"{outline} px dark outline" if outline else "no outline")
            + f", {MAP_MARGIN_PX} px margin, lit by {lit}, view transform {view_used}"
            + ("" if args.get("view_transform") else " (the scene's; pass view_transform to "
               "match the game's tonemapper)")
            + (". In a view from above the model looks taller in pixels than px_height, "
               "because its depth shows too." if px_h and not args.get("metres_per_px") else "."))
    if glb:
        note = (f"{os.path.basename(glb)}, in the pose the file stores (no clip playing). "
                + note)
    if len(per_view) == 1:
        _, size, glow = per_view[0]
        note += f" {size[0].upper()}{size[1:]}. {glow}"
    else:
        note += "".join(f"\n- Azimuth {v:g}: {size}. {glow}" for v, size, glow in per_view) + "\n"
    blocks = _png_blocks(out, out, "Map test render", stats=True)
    blocks[-1]["text"] = note + " " + blocks[-1]["text"]
    return blocks


def _emissive_materials(objs):
    """The materials on these objects that emit light: a Principled BSDF or Emission node
    with a strength above zero (or driven by other nodes)."""
    out = []
    for o in objs:
        for slot in o.material_slots:
            m = slot.material
            if m is None or m in out or not m.node_tree:
                continue
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    s_in, c_in = n.inputs.get("Emission Strength"), n.inputs.get("Emission Color")
                elif n.type == "EMISSION":
                    s_in, c_in = n.inputs.get("Strength"), n.inputs.get("Color")
                else:
                    continue
                if s_in is None or not (s_in.is_linked or s_in.default_value > 1e-4):
                    continue
                if c_in is not None and not c_in.is_linked and max(c_in.default_value[:3]) <= 1e-4:
                    continue
                out.append(m)
                break
    return out


def _black_out(mat):
    """Make a material render black and unlit. Returns a function that puts it back."""
    nt = mat.node_tree
    outs = [n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"]
    black = nt.nodes.new("ShaderNodeEmission")
    black.inputs["Color"].default_value = (0, 0, 0, 1)
    black.inputs["Strength"].default_value = 0.0
    was = []
    for n in outs:
        surf = n.inputs["Surface"]
        was.append((surf, surf.links[0].from_socket if surf.is_linked else None))
        nt.links.new(black.outputs[0], surf)

    def undo():
        for surf, src in was:
            if src is not None:
                nt.links.new(src, surf)
            else:
                for link in list(surf.links):
                    nt.links.remove(link)
        nt.nodes.remove(black)
    return undo


def _glow_pixels(comp, solid, glow=None):
    """Sentences counting a map test's glow pixels (glow: the mask of pixels that show an
    emissive material, or None when the model has none) and its bright, saturated pixels
    of any material by hue. comp is h x w x 3 sRGB 0-1."""
    import numpy as np

    rgb = comp[solid]
    if not len(rgb):
        return ""
    if glow is None:
        first = "Glow pixels: none, the model has no material with emission."
    else:
        n = int(glow.sum())
        if not n:
            first = ("Glow pixels (showing a material with emission): none, so no glow "
                     "shows from here at this size.")
        else:
            mean = comp[glow].mean(axis=0)
            first = (f"Glow pixels (showing a material with emission): {n} of {len(rgb)} "
                     f"model pixels, average colour {_hex(mean)} ({_hsv_words(mean)})."
                     + (" One or two pixels don't read at this size." if n <= 2 else ""))
    mx, mn = rgb.max(axis=1), rgb.min(axis=1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    bright = (mx >= 0.6) & (sat >= 0.4)
    if not bright.any():
        return first + " Bright, saturated pixels of any material (value 60%+, saturation 40%+): none."
    counts = {}
    for c in rgb[bright]:
        hue, s_, _ = _hsv(c)
        name = _hue_name(hue, s_)
        counts[name] = counts.get(name, 0) + 1
    parts = ", ".join(f"{n} {name}" for name, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return (first + " Bright, saturated pixels of any material, paint as well as glow (value "
            f"60%+, saturation 40%+), by hue: {parts}.")


HANDLERS = {
    "get_scene_summary": get_scene_summary,
    "get_object_details": get_object_details,
    "execute_python": execute_python,
    "capture_viewport": capture_viewport,
    "view_image": view_image,
    "check_animation_loop": check_animation_loop,
    "check_detached_parts": check_detached_parts,
    "measure_model": measure_model,
    "check_ground_contact": check_ground_contact,
    "render_map_test": render_map_test,
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
