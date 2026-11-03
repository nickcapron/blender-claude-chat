"""Install the add-on's Python dependencies into ./vendor using Blender's own Python.

Run with Blender (so the packages match Blender's Python version):

    blender --background --python install_deps.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")

# Inside Blender, sys.executable is Blender's bundled Python interpreter.
python = sys.executable
subprocess.check_call([python, "-m", "ensurepip", "--upgrade"])
subprocess.check_call(
    [python, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "--target", VENDOR, "anthropic"]
)
print(f"\nInstalled dependencies into {VENDOR}")
