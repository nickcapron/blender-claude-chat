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

Requires Blender 4.2 or newer (tested on 5.2), plus one of:

- **Your Claude account** (default): [Claude Code](https://claude.com/claude-code) installed and
  logged in with your Claude subscription. No API key needed.
- **An [Anthropic API key](https://console.anthropic.com/)**, billed per token.

1. Clone this repo into your Blender add-ons folder, or clone it anywhere and link it there.
   The folder must be named `claude_chat`:
   - Windows: `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\claude_chat`
   - macOS: `~/Library/Application Support/Blender/<version>/scripts/addons/claude_chat`
   - Linux: `~/.config/blender/<version>/scripts/addons/claude_chat`
2. In Blender: **Edit → Preferences → Add-ons**, search for **Claude Chat**, and enable it.
3. Choose how to connect in the add-on preferences:
   - **Claude account**: run `claude` once in a terminal and log in. The add-on finds Claude
     Code automatically; set its path in preferences if it can't.
   - **API key**: first install the `anthropic` SDK into `vendor/` using Blender's own Python,
     then enter your key (or set `ANTHROPIC_API_KEY`). A key entered in preferences is stored
     in plain text in your Blender preferences file.
     ```
     blender --background --python claude_chat/install_deps.py
     ```

## Settings

In the add-on preferences:

- **Connect with**: Claude account (default) or API key.
- **Model**: Default (your Claude Code default, or Claude Opus 5.5 with an API key), Opus 5.5,
  Sonnet 5.5, or Haiku 5.5.
- **Effort**: how much Claude thinks before acting (Medium by default).
- **Messages shown**: how many messages the sidebar displays. The full transcript is always
  available from the 📄 button in the panel header.
- **Send on Enter**

## How it works

| File | Role |
| --- | --- |
| `ui.py` | Preferences, sidebar panel, viewport overlay, operators, and a main-thread timer |
| `session.py` | Conversation state, tool dispatch, and the API-key backend |
| `claude_code.py` | Claude-account backend: runs `claude -p` headless and streams its output |
| `bridge.py` | Token-protected localhost socket that serves Blender tools to Claude Code |
| `mcp_bridge.py` | Tiny MCP server (stdlib only) that Claude Code launches to reach `bridge.py` |
| `blender_tools.py` | Tool definitions and implementations (run on Blender's main thread) |
| `install_deps.py` | Installs the `anthropic` SDK into `vendor/` (API-key mode only) |

`bpy` isn't thread-safe, so nothing outside Blender's main thread touches the scene. When Claude
calls a tool, the request is queued and a timer on the main thread runs it (or waits for your
approval) and hands back the result.

In Claude-account mode, each message runs your own Claude Code in headless mode
(`claude -p`), resuming the same session so the conversation continues. Claude Code runs with
its built-in tools (shell, file editing, web) turned off and only the Blender tools available,
so everything it does goes through the approval flow above.

## Limitations

- Chat history is kept in memory and cleared when Blender closes.
- Claude's Python runs with full access to your Blender session and file system. Review code
  before approving, and only turn on **Auto-run code** if you trust what you're asking for.

## License

[MIT](LICENSE)
