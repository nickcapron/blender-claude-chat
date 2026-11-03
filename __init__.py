bl_info = {
    "name": "Claude Chat",
    "author": "crender",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Claude",
    "description": "Chat with Claude inside Blender and let it inspect and edit your scene",
    "category": "Interface",
}

import os
import sys

# Bundled dependencies (anthropic SDK), installed with Blender's own Python.
_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from . import ui  # noqa: E402


def register():
    ui.register()


def unregister():
    ui.unregister()
