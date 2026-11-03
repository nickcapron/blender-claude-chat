# crender

![crender: Claude, right inside Blender](assets/brand/crender-banner.svg)

**Chat with Claude inside [Blender](https://www.blender.org). Ask in plain language and
Claude builds, changes and explains things in your scene, right in the 3D Viewport.**

crender adds a chat panel to Blender's 3D Viewport. Claude can look at your scene, write
and run Blender Python to make changes (after you approve it), and take a screenshot of
the viewport to check its own work. It works with your own **Claude account**; no API key
needed.

> "Add a low-poly tree next to the cube."
>
> "Make the selected object look like brushed gold."
>
> "Set up three-point lighting and point the camera at the character."
>
> "Why is my render black?"
>
> "Scatter 50 rocks of different sizes on the ground plane."
>
> "Explain what the modifiers on this object do."

Claude shows you its code and waits for **Run** before anything changes, and every run is
one undo step, so **Ctrl+Z undoes anything Claude did**.

## What Claude can do

| Area | What it means |
| --- | --- |
| **See your scene** | Objects, transforms, collections, selection, frame range, render settings |
| **Inspect objects** | Mesh stats, modifiers and their settings, material node setups, lights, cameras, animation, custom properties |
| **Make changes** | Anything Blender Python can do: modeling, materials, lighting, cameras, modifiers, animation, scene setup |
| **Check its work** | Takes a screenshot of the 3D Viewport and looks at it |
| **Know what you mean** | Every message includes your mode, selection and frame, so "make this red" works |

## Install

You need **Blender 4.2 or newer** (opened at least once) and a paid **Claude** plan (Pro,
Max, Team or Enterprise; the free plan doesn't include Claude Code), or an
[API key](#using-an-api-key-instead).

### Windows: one command

Open **PowerShell** (Start menu → type `powershell` → Enter; not *Run as administrator*,
so crender installs for your own account), paste this whole line and press Enter:

```powershell
irm https://raw.githubusercontent.com/nickcapron/blender-claude-chat/main/install.ps1 | iex
```

Copy it in one piece: `irm` on its own asks for a `Uri:`, and without `| iex` at the end
PowerShell only prints the installer instead of running it. When it's running you'll see
**crender installer**, then the Blender versions it found.

It:

- finds every Blender 4.2 or newer on your PC and installs crender for each one;
- turns the add-on on for you (if Blender is open, it asks you to close it first);
- offers to install **Claude Code** with Anthropic's official installer, if you don't have it.

Then sign in to Claude Code once (open a new terminal, run `claude`, and follow the browser
prompts) and open Blender: the chat opens by itself. Run the same command again to update.

The installer never asks for a Claude password or key; signing in happens in Claude's own
app. To see what it does first, [read install.ps1](install.ps1).

### macOS, Linux, or by hand

1. Install Claude Code and sign in. In **Terminal**:
   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   ```
   then open a new terminal, run `claude`, and follow the prompts to sign in. (On Windows,
   the PowerShell command is `irm https://claude.ai/install.ps1 | iex`.)
2. **[Download crender.zip](https://github.com/nickcapron/blender-claude-chat/releases/latest/download/crender.zip)**.
   Don't unzip it.
3. In Blender, open **Edit → Preferences → Add-ons**, click the **⌄** arrow at the top
   right, choose **Install from Disk…**, and pick `crender.zip`.
4. Make sure **crender: Claude Chat** has a tick next to it. The chat opens by itself the
   first time.

To update, install the new zip the same way.

## Using it

- Click the **Claude** button in the bar at the top of the 3D Viewport (next to
  View · Select · Add · Object). Click it again to hide the chat.
- Type in the box at the bottom and press **Enter**, or click one of the example prompts.
- When Claude wants to change something, its code appears with **Run** and **Reject**.
  To steer it, type what you want instead in the message box, then click **Reject**.
- **Stop** cancels a reply. **New chat** starts over.
- Press **F7** over the viewport to ask a quick question in a pop-up.
- The overlay in the bottom-left of the viewport shows what Claude is doing and its latest
  reply, so you can keep working with the sidebar closed.

Tip: **Auto-run code** skips the approval step. Turn it on only when you trust what you're
asking for; Ctrl+Z still undoes each run.

## Sharing a chat or reporting a bug

Click **Copy chat** under the message box (or the copy icon at the top of the panel).
This copies the whole conversation as text, including the code Claude ran, what it
printed, any errors, and your Blender, crender and Claude Code versions. Paste it into a
message, a GitHub issue, or a chat with Claude when something goes wrong.

Your API key is never included. The copy does include your `.blend` file's name.

## Settings

In **Edit → Preferences → Add-ons → crender: Claude Chat**:

- **Connect with**: **Claude account** (default) or **API key**.
- **Model**: Default (your Claude Code default), Claude Opus 5.5, Sonnet 5.5 or Haiku 5.5.
- **Effort**: how much Claude thinks before acting. Medium is a good default; higher is
  slower and uses more of your plan.
- **Messages shown**, **Send on Enter**, and the **Claude button** in the viewport header.

### Using an API key instead

Choose **API key** under **Connect with**, then install the Anthropic library into the
add-on (once, from a terminal):

```bash
blender --background --python "<add-ons folder>/claude_chat/install_deps.py"
```

Then paste your key from the [Claude Console](https://console.anthropic.com/) into the
preferences, or set the `ANTHROPIC_API_KEY` environment variable. A key typed into
preferences is stored in plain text in your Blender preferences file. API usage is billed
per token.

## Security notes

- Claude's code runs inside your Blender with the same access Blender has, including your
  files. That's why it asks before running anything. Read the code (**View all** shows
  it in full) before you click **Run**.
- With your Claude account, crender runs your own Claude Code with its built-in tools
  (shell, file editing, web) turned off. Claude can only use crender's Blender tools, and
  your global Claude Code hooks and plugins aren't loaded.
- crender talks to Claude Code over a local connection on your computer only
  (`127.0.0.1`), protected by a random key that changes every time Blender starts.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| No **Claude** button | Check the add-on is ticked in Preferences. If the viewport's menus (View, Select…) are hidden, right-click the header and turn **Show Menus** back on. You can also press **N** over the viewport and click the **Claude** tab. |
| "Claude Code isn't installed" | Run the [installer](#windows-one-command) again (it offers to install Claude Code), or install it [by hand](#macos-linux-or-by-hand); then restart Blender. If it's installed somewhere unusual, set **Claude Code path** in preferences. |
| "Claude Code isn't logged in" | Open a terminal, run `claude`, sign in, and try again. |
| Claude's code fails | Claude usually reads the error and fixes it. If not, click **Copy chat** and share it. |
| Something else | [Open an issue](https://github.com/nickcapron/blender-claude-chat/issues) and paste a **Copy chat** export. |

## How it works

| File | Role |
| --- | --- |
| `ui.py` | Preferences, the chat panel, header button, viewport overlay, and a main-thread timer |
| `session.py` | Conversation state, tool dispatch, chat export, and the API-key backend |
| `claude_code.py` | Claude-account backend: runs `claude -p` headless and streams its output |
| `bridge.py` | Local, token-protected socket that serves Blender tools to Claude Code |
| `mcp_bridge.py` | Tiny MCP server (standard library only) that Claude Code launches to reach `bridge.py` |
| `blender_tools.py` | The tools Claude can call, run on Blender's main thread |
| `install_deps.py` | Installs the `anthropic` library into `vendor/` (API-key mode only) |
| `install.ps1` | The Windows one-command installer |

Blender's Python API isn't thread-safe, so nothing outside Blender's main thread touches the
scene. When Claude calls a tool, the request is queued and a timer on the main thread runs
it (or waits for your approval) and sends back the result. With your Claude account, each
message runs Claude Code in headless mode and resumes the same session, so the conversation
carries on.

## Development

Clone the repo into your add-ons folder as `claude_chat` (or link it there):

- Windows: `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\claude_chat`
- macOS: `~/Library/Application Support/Blender/<version>/scripts/addons/claude_chat`
- Linux: `~/.config/blender/<version>/scripts/addons/claude_chat`

`python build_release.py` builds `dist/crender.zip`, and `python assets/make_brand.py`
regenerates the logo and banner (needs Pillow).

## License

[MIT](LICENSE)
