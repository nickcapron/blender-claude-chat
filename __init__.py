bl_info = {
    "name": "crender: Claude Chat",
    "author": "Nick Capron",
    "version": (0, 3, 1),
    "blender": (4, 2, 0),
    "location": "3D Viewport > header > Claude button",
    "description": "Chat with Claude inside Blender and let it build, inspect and fix your scene",
    "doc_url": "https://github.com/nickcapron/blender-claude-chat",
    "tracker_url": "https://github.com/nickcapron/blender-claude-chat/issues",
    "category": "Interface",
}

import os
import sys

# Bundled dependencies (anthropic SDK), installed with Blender's own Python.
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from . import ui  # noqa: E402

# The glTF exporter looks these up on enabled add-ons; see gltf_guard.py.
from .gltf_guard import glTF2_post_export_callback, glTF2_pre_export_callback  # noqa: E402,F401


def register():
    ui.register()


def unregister():
    ui.unregister()
