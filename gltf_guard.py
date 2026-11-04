"""Keep glTF exports from changing the scene.

Blender's glTF exporter (seen in 5.2) saves each animated object's matrix_world before
baking its NLA tracks or actions and restores it by assigning matrix_world afterwards.
For a child whose parent is animated too, Blender rebuilds the child's local transform
from a stale parent matrix, so the error lands in channels that have no keys and nothing
overwrites it. It grows with every export: three exports tilted an animated child empty
by about 18 degrees, which then wobbled when it spun and was baked into the next .glb.

The exporter calls glTF2_pre_export_callback and glTF2_post_export_callback on every
enabled add-on that defines them (crender's __init__ re-exports these). We snapshot every
object's local transform before an export and put it back afterwards.
"""

import bpy

_saved = None


def _snapshot():
    out = []
    for o in bpy.data.objects:
        if o.library is not None:  # linked objects can't be edited (or changed by export)
            continue
        out.append((o, o.location.copy(), o.rotation_mode, o.rotation_euler.copy(),
                    o.rotation_quaternion.copy(), tuple(o.rotation_axis_angle), o.scale.copy()))
    return out


def _restore(saved):
    changed = []
    for o, loc, mode, eul, quat, axis_angle, scale in saved:
        try:
            if (o.location != loc or o.rotation_mode != mode or o.rotation_euler != eul
                    or o.rotation_quaternion != quat or tuple(o.rotation_axis_angle) != axis_angle
                    or o.scale != scale):
                changed.append(o.name)
                o.location = loc
                o.rotation_mode = mode
                o.rotation_euler = eul
                o.rotation_quaternion = quat
                o.rotation_axis_angle = axis_angle
                o.scale = scale
        except ReferenceError:  # object removed during the export
            continue
    return changed


def glTF2_pre_export_callback(export_settings):
    global _saved
    try:
        _saved = _snapshot()
    except Exception as e:  # never break an export
        _saved = None
        print(f"crender: could not snapshot transforms before glTF export: {e}")


def glTF2_post_export_callback(export_settings):
    global _saved
    saved, _saved = _saved, None
    if not saved:
        return
    try:
        changed = _restore(saved)
    except Exception as e:
        print(f"crender: could not restore transforms after glTF export: {e}")
        return
    if changed:
        names = ", ".join(changed[:10]) + (f" and {len(changed) - 10} more" if len(changed) > 10 else "")
        print(f"crender: the glTF exporter changed the transforms of {names}; restored them.")
