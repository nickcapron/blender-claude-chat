# Claude Chat for Blender

Chat with Claude inside Blender. Claude can inspect your scene, run Python in your live
session (after you approve it), and screenshot the viewport to check its own work.

## Features

- **Sidebar chat**: 3D Viewport → press `N` → **Claude** tab. Type and press Enter; replies stream in.
- **Viewport overlay**: Claude's status and latest reply in the bottom-left of the viewport.
- **Quick prompt**: press `F7` in the 3D Viewport.
- **Scene tools**: scene summary, per-object details (modifiers, materials, nodes, mesh stats),
  Python execution, and viewport screenshots.
- **Selection-aware**: each message includes your current mode, selection and frame, so
  "make this red" works.
- **Safe by default**: Claude's code is shown for approval before it runs (**Run** / **Reject** /
  **Always run**). Type a reason before pressing Reject to send Claude feedback. Each run is a
  single undo step, so `Ctrl+Z` reverts it.

## Install

Requires Blender 4.2 or newer (tested on 5.2) and an [Anthropic API key](https://console.anthropic.com/).

1. Clone this repo into your Blender add-ons folder, or clone it anywhere and link it there.
   The folder must be named `claude_chat`:
   - Windows: `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\claude_chat`
   - macOS: `~/Library/Application Support/Blender/<version>/scripts/addons/claude_chat`
   - Linux: `~/.config/blender/<version>/scripts/addons/claude_chat`
2. Install the dependencies (the `anthropic` SDK) into `vendor/` using Blender's own Python:
   ```
   blender --background --python claude_chat/install_deps.py
   ```
3. In Blender: **Edit → Preferences → Add-ons**, search for **Claude Chat**, and enable it.
4. Enter your API key in the add-on preferences, or leave it empty and set the
   `ANTHROPIC_API_KEY` environment variable. A key entered in preferences is stored in
   plain text in your Blender preferences file.

## Settings

In the add-on preferences:

- **Model**: Claude Opus 5.5 (default), Sonnet 5.5, or Haiku 5.5.
- **Effort**: how much Claude thinks before acting (Medium by default).
- **Messages shown**: how many messages the sidebar displays. The full transcript is always
  available from the 📄 button in the panel header.
- **Send on Enter**

## How it works

| File | Role |
| --- | --- |
| `ui.py` | Preferences, sidebar panel, viewport overlay, operators, and a main-thread timer |
| `session.py` | Conversation state and a background worker that streams from the Claude API |
| `blender_tools.py` | Tool definitions and implementations (run on Blender's main thread) |
| `install_deps.py` | Installs the `anthropic` SDK into `vendor/` |

`bpy` isn't thread-safe, so the API worker never touches Blender directly. When Claude calls a
tool, the worker queues it and a timer on the main thread runs it (or waits for your approval)
and hands back the result.

## Limitations

- Chat history is kept in memory and cleared when Blender closes.
- Claude's Python runs with full access to your Blender session and file system. Review code
  before approving, and only turn on **Auto-run code** if you trust what you're asking for.
