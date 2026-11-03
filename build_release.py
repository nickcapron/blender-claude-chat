"""Build the installable add-on zip (dist/crender.zip) for a GitHub release.

    python build_release.py

The zip holds a single `claude_chat/` folder, which is what Blender's
Preferences > Add-ons > Install from Disk expects. Dev-only files are left out.
"""

import ast
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_FILES = [
    "__init__.py",
    "ui.py",
    "session.py",
    "blender_tools.py",
    "claude_code.py",
    "bridge.py",
    "mcp_bridge.py",
    "install_deps.py",
    "LICENSE",
    "README.md",
    "assets/brand/crender-icon.png",
]


def version():
    with open(os.path.join(HERE, "__init__.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "bl_info":
            return ".".join(map(str, ast.literal_eval(node.value)["version"]))
    raise SystemExit("bl_info not found")


def main():
    os.makedirs(os.path.join(HERE, "dist"), exist_ok=True)
    out = os.path.join(HERE, "dist", "crender.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ADDON_FILES:
            z.write(os.path.join(HERE, name), f"claude_chat/{name}")
    print(f"{out}  (version {version()})")


if __name__ == "__main__":
    main()
