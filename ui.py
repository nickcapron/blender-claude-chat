"""Blender UI: preferences, sidebar chat panel, viewport overlay, operators, main-thread timer."""

import queue
import time

import blf
import bpy
import gpu
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty
from gpu_extras.batch import batch_for_shader

from . import blender_tools, bridge, claude_code
from .session import SESSION

ADDON_ID = __package__
TRANSCRIPT_TEXT = "Claude Chat Transcript"
PENDING_TEXT = "Claude Pending Code"
OVERLAY_LINGER = 20.0  # seconds the last reply stays on the overlay

_draw_handle = None
_addon_keymaps = []
_suppress_prompt_update = False
_overlay_visible = False


def prefs():
    return bpy.context.preferences.addons[ADDON_ID].preferences


# ---------------------------------------------------------------------------
# Preferences & properties
# ---------------------------------------------------------------------------


class ClaudeChatPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    backend: EnumProperty(
        name="Connect with",
        items=[
            ("CLI", "Claude account", "Use your Claude subscription through your Claude Code install (claude login)"),
            ("API", "API key", "Use an Anthropic API key (billed per token)"),
        ],
        default="CLI",
    )
    claude_path: StringProperty(
        name="Claude Code path",
        description="Path to the claude executable. Leave empty to find it automatically",
        subtype="FILE_PATH",
    )
    api_key: StringProperty(
        name="API Key",
        description="Anthropic API key. Leave empty to use the ANTHROPIC_API_KEY environment variable",
        subtype="PASSWORD",
    )
    model: EnumProperty(
        name="Model",
        items=[
            ("default", "Default", "Claude account: your Claude Code default model. API key: Claude Opus 5.5"),
            ("claude-opus-5-5", "Claude Opus 5.5", "Most capable for building and scripting"),
            ("claude-sonnet-5-5", "Claude Sonnet 5.5", "Faster and cheaper"),
            ("claude-haiku-5-5", "Claude Haiku 5.5", "Fastest and cheapest"),
        ],
        default="default",
    )
    effort: EnumProperty(
        name="Effort",
        description="How much Claude thinks before acting. Higher is slower and costs more",
        items=[
            ("low", "Low", "Quick answers and simple edits"),
            ("medium", "Medium", "Balanced (default)"),
            ("high", "High", "Complex builds and tricky scripts"),
            ("xhigh", "Extra High", "Long, multi-step work"),
            ("max", "Max", "Hardest problems"),
        ],
        default="medium",
    )
    messages_shown: IntProperty(name="Messages shown in sidebar", default=6, min=2, max=60)
    show_header_button: BoolProperty(
        name="Show Claude button in the 3D Viewport header",
        default=True,
        update=lambda self, ctx: _redraw_all(),
    )
    welcomed: BoolProperty(default=False, options={"HIDDEN"})  # chat opened once on first enable
    send_on_enter: BoolProperty(
        name="Send on Enter",
        description="Send the message when you confirm the text field",
        default=True,
    )

    def draw(self, context):
        col = self.layout.column()
        col.row().prop(self, "backend", expand=True)
        box = col.box()
        if self.backend == "CLI":
            found = claude_code.find_claude(self.claude_path)
            box.prop(self, "claude_path")
            if found:
                box.label(text=f"Using {found}", icon="CHECKMARK")
            else:
                box.label(text="Claude Code not found. Install it, or set the path above.", icon="ERROR")
            box.label(text="Log in once by running `claude` in a terminal with your Claude account.", icon="INFO")
        else:
            box.prop(self, "api_key")
            box.label(text="Stored in your Blender preferences file. Leave empty to use ANTHROPIC_API_KEY.", icon="INFO")
        col.separator()
        row = col.row()
        row.prop(self, "model")
        row.prop(self, "effort")
        col.prop(self, "messages_shown")
        col.prop(self, "send_on_enter")
        col.prop(self, "show_header_button")
        col.separator()
        col.label(text="Open the chat with the Claude button at the top of the 3D Viewport.", icon="INFO")
        col.label(text="Shortcut: F7 in the 3D Viewport opens a quick prompt.", icon="EVENT_F7")


def _prompt_updated(self, context):
    if _suppress_prompt_update or not prefs().send_on_enter:
        return
    if self.prompt.strip() and not SESSION.busy:
        bpy.ops.claude.send()


class ClaudeChatProps(bpy.types.PropertyGroup):
    prompt: StringProperty(name="Message", description="Message to Claude", update=_prompt_updated)
    auto_run: BoolProperty(
        name="Auto-run code",
        description="Run Claude's Python without asking first (each run is still undoable)",
        default=False,
    )
    show_overlay: BoolProperty(
        name="Viewport overlay",
        description="Show Claude's status and latest reply over the 3D Viewport",
        default=True,
        update=lambda self, ctx: _redraw_all(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redraw_all():
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in {"VIEW_3D", "TEXT_EDITOR"}:
                area.tag_redraw()


def _wrap(text, max_px, size):
    """Word-wrap text to a pixel width, measured with Blender's UI font."""
    blf.size(0, size)
    measure = lambda s: blf.dimensions(0, s)[0]  # noqa: E731
    lines = []
    for raw in text.replace("\t", "    ").split("\n"):
        if not raw.strip():
            lines.append("")
            continue
        indent = raw[: len(raw) - len(raw.lstrip())]
        line = ""
        for word in raw.split():
            candidate = f"{line} {word}" if line else indent + word
            if measure(candidate) <= max_px:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = indent + word
            # Hard-break words longer than a whole line.
            while measure(line) > max_px and len(line) > len(indent) + 1:
                cut = len(line) - 1
                while cut > len(indent) + 1 and measure(line[:cut]) > max_px:
                    cut -= 1
                lines.append(line[:cut])
                line = indent + line[cut:]
        lines.append(line)
    return lines


def _ui_text_size(context):
    style = context.preferences.ui_styles[0].widget
    pts = getattr(style, "points", None) or getattr(style, "size", 11)
    # system.ui_scale includes the monitor DPI factor; view.ui_scale does not.
    return pts * context.preferences.system.ui_scale


def _write_text_block(name, body, show=True):
    txt = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    txt.clear()
    txt.write(body)
    if show:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "TEXT_EDITOR":
                    area.spaces.active.text = txt
                    area.tag_redraw()
                    return True
    return False


def _build_cfg():
    p = prefs()
    model = p.model
    if p.backend == "API" and model == "default":
        model = "claude-opus-5-5"
    return {
        "backend": p.backend,
        "claude_path": bpy.path.abspath(p.claude_path) if p.claude_path else "",
        "api_key": p.api_key.strip(),
        "model": model,
        "effort": p.effort,
        "version": bpy.app.version_string,
        "tools": blender_tools.TOOL_DEFS,
    }


def _run_call(call):
    content, is_error = blender_tools.run(call.name, call.args)
    call.finish(content, is_error)
    SESSION.mark_dirty()


# ---------------------------------------------------------------------------
# Main-thread timer: runs tool calls queued by the worker
# ---------------------------------------------------------------------------


def _tick():
    global _overlay_visible
    s = SESSION
    if s.pending is None:
        try:
            call = s.calls.get_nowait()
        except queue.Empty:
            call = None
        if call is not None:
            wm = bpy.context.window_manager
            if s.cancel.is_set():
                call.finish("Cancelled by the user.", True)
            elif call.name == "execute_python" and not wm.claude_chat.auto_run:
                s.pending = call
                s._set_status("Waiting for your approval to run code")
                _write_text_block(PENDING_TEXT, call.args["code"], show=False)
            else:
                _run_call(call)

    # Fade the overlay out a while after the last reply.
    visible_now = s.busy or s.pending is not None or (time.time() - s.finished_at) < OVERLAY_LINGER
    if s.consume_dirty() or visible_now != _overlay_visible:
        _overlay_visible = visible_now
        _redraw_all()
    return 0.05 if (s.busy or not s.calls.empty()) else 0.25


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


class CLAUDE_OT_send(bpy.types.Operator):
    bl_idname = "claude.send"
    bl_label = "Send"
    bl_description = "Send your message to Claude"

    text: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        global _suppress_prompt_update
        props = context.window_manager.claude_chat
        msg = (self.text or props.prompt).strip()
        if not msg:
            return {"CANCELLED"}
        if SESSION.busy:
            self.report({"WARNING"}, "Claude is still working. Stop it first or wait.")
            return {"CANCELLED"}
        SESSION.send(msg, blender_tools.context_snapshot(), _build_cfg())
        _suppress_prompt_update = True
        try:
            props.prompt = ""
        finally:
            _suppress_prompt_update = False
        _redraw_all()
        return {"FINISHED"}


class CLAUDE_OT_quick_ask(bpy.types.Operator):
    bl_idname = "claude.quick_ask"
    bl_label = "Ask Claude"
    bl_description = "Pop up a prompt and send it to Claude"

    text: StringProperty(name="", options={"SKIP_SAVE"})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=500)

    def draw(self, context):
        self.layout.prop(self, "text", text="")
        if SESSION.busy:
            self.layout.label(text="Claude is busy; this will be ignored.", icon="ERROR")

    def execute(self, context):
        if self.text.strip():
            bpy.ops.claude.send(text=self.text)
        return {"FINISHED"}


class CLAUDE_OT_stop(bpy.types.Operator):
    bl_idname = "claude.stop"
    bl_label = "Stop"
    bl_description = "Stop Claude's current response"

    def execute(self, context):
        SESSION.stop()
        if SESSION.pending is not None:
            call, SESSION.pending = SESSION.pending, None
            call.finish("Cancelled by the user.", True)
        return {"FINISHED"}


class CLAUDE_OT_clear(bpy.types.Operator):
    bl_idname = "claude.clear"
    bl_label = "New Chat"
    bl_description = "Clear the conversation and start fresh"

    def execute(self, context):
        if not SESSION.clear():
            self.report({"WARNING"}, "Stop Claude before clearing the chat.")
            return {"CANCELLED"}
        blender_tools.reset_namespace()
        _redraw_all()
        return {"FINISHED"}


class CLAUDE_OT_approve(bpy.types.Operator):
    bl_idname = "claude.approve"
    bl_label = "Run"
    bl_description = "Run the code Claude proposed (undoable with Ctrl+Z)"

    always: BoolProperty(default=False, options={"SKIP_SAVE"})

    def execute(self, context):
        call, SESSION.pending = SESSION.pending, None
        if call is None:
            return {"CANCELLED"}
        if self.always:
            context.window_manager.claude_chat.auto_run = True
        _run_call(call)
        return {"FINISHED"}


class CLAUDE_OT_reject(bpy.types.Operator):
    bl_idname = "claude.reject"
    bl_label = "Reject"
    bl_description = "Don't run this code. Anything typed in the message box is sent to Claude as the reason"

    def execute(self, context):
        global _suppress_prompt_update
        call, SESSION.pending = SESSION.pending, None
        if call is None:
            return {"CANCELLED"}
        props = context.window_manager.claude_chat
        reason = props.prompt.strip()
        msg = "The user rejected this code; it was not run."
        if reason:
            msg += f" Their feedback: {reason}"
            _suppress_prompt_update = True
            try:
                props.prompt = ""
            finally:
                _suppress_prompt_update = False
        call.finish(msg, True)
        SESSION.mark_dirty()
        return {"FINISHED"}


_cli_version_cache = {}


def _claude_code_version(exe):
    if not exe:
        return "not found"
    if exe not in _cli_version_cache:
        import subprocess
        import sys

        try:
            out = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            _cli_version_cache[exe] = (out.stdout or out.stderr).strip().splitlines()[0][:80]
        except Exception as e:
            _cli_version_cache[exe] = f"unknown ({e.__class__.__name__})"
    return _cli_version_cache[exe]


def _environment(context):
    import os
    import platform
    import sys

    p = prefs()
    addon = sys.modules.get(ADDON_ID)
    version = ".".join(map(str, getattr(addon, "bl_info", {}).get("version", ("?",))))
    env = [
        ("crender", version),
        ("Blender", bpy.app.version_string),
        ("OS", platform.platform()),
        ("Connect with", "Claude account (Claude Code)" if p.backend == "CLI" else "API key"),
    ]
    if p.backend == "CLI":
        exe = claude_code.find_claude(bpy.path.abspath(p.claude_path) if p.claude_path else "")
        env.append(("Claude Code", _claude_code_version(exe)))
    env += [
        ("Model", "Claude Code default" if _build_cfg()["model"] == "default" else _build_cfg()["model"]),
        ("Effort", p.effort),
        ("Auto-run code", "on" if context.window_manager.claude_chat.auto_run else "off"),
        ("File", os.path.basename(bpy.data.filepath) or "(unsaved)"),
    ]
    return env


class CLAUDE_OT_copy_chat(bpy.types.Operator):
    bl_idname = "claude.copy_chat"
    bl_label = "Copy Chat"
    bl_description = (
        "Copy the whole chat, including the code Claude ran and its output, so you can paste it "
        "into a message or bug report"
    )

    def execute(self, context):
        display, _, _ = SESSION.snapshot()
        if not display:
            self.report({"INFO"}, "Nothing to copy yet.")
            return {"CANCELLED"}
        context.window_manager.clipboard = SESSION.export_markdown(_environment(context))
        self.report({"INFO"}, "Chat copied. Paste it anywhere with Ctrl+V.")
        return {"FINISHED"}


class CLAUDE_OT_show_text(bpy.types.Operator):
    bl_idname = "claude.show_text"
    bl_label = "Open in Text Editor"
    bl_description = "Open the full chat transcript or the pending code in a Text Editor"

    what: EnumProperty(items=[("TRANSCRIPT", "Transcript", ""), ("PENDING", "Pending code", "")])

    def execute(self, context):
        if self.what == "PENDING":
            if SESSION.pending is None:
                return {"CANCELLED"}
            name, body = PENDING_TEXT, SESSION.pending.args["code"]
        else:
            name, body = TRANSCRIPT_TEXT, SESSION.transcript()
        if not _write_text_block(name, body):
            self.report({"INFO"}, f"Saved to text '{name}'. Open a Text Editor to read it.")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Opening the chat: header button, first-run, F-key
# ---------------------------------------------------------------------------


def _find_view3d(context):
    if context.area and context.area.type == "VIEW_3D":
        return context.area
    win = context.window or (context.window_manager.windows[0] if context.window_manager.windows else None)
    if win:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                return area
    return None


def _sidebar(area):
    return next((r for r in area.regions if r.type == "UI"), None)


def _select_claude_tab(area, tries=20):
    """Switch the sidebar to the Claude tab. A sidebar that was just opened has no tabs until it
    has drawn once, so retry briefly."""
    try:
        region = _sidebar(area)
        region.active_panel_category = "Claude"
        area.tag_redraw()
        return
    except ReferenceError:
        return  # area went away
    except Exception:
        pass
    if tries > 0:
        bpy.app.timers.register(lambda: _select_claude_tab(area, tries - 1), first_interval=0.05)


def _chat_is_open(area):
    try:
        region = _sidebar(area)
        return area.spaces.active.show_region_ui and region and region.active_panel_category == "Claude"
    except Exception:
        return False


def open_chat(area):
    area.spaces.active.show_region_ui = True
    area.tag_redraw()
    _select_claude_tab(area)


class CLAUDE_OT_open_chat(bpy.types.Operator):
    bl_idname = "claude.open_chat"
    bl_label = "Claude"
    bl_description = "Open the Claude chat in the sidebar (click again to hide it)"

    toggle: BoolProperty(default=True, options={"SKIP_SAVE"})

    def execute(self, context):
        area = _find_view3d(context)
        if area is None:
            self.report({"WARNING"}, "Open a 3D Viewport to chat with Claude.")
            return {"CANCELLED"}
        if self.toggle and _chat_is_open(area):
            area.spaces.active.show_region_ui = False
        else:
            open_chat(area)
        return {"FINISHED"}


def _draw_header_button(self, context):
    try:
        if not prefs().show_header_button:
            return
    except KeyError:
        return
    area = context.area
    is_open = bool(area) and _chat_is_open(area)
    label = "Claude"
    if SESSION.pending is not None:
        label = "Claude: approve code"
    elif SESSION.busy:
        label = "Claude: working..."
    row = self.layout.row(align=True)
    row.alert = SESSION.pending is not None
    row.operator("claude.open_chat", text=label, icon="LIGHT", depress=is_open)


def _first_run():
    try:
        p = prefs()
    except KeyError:
        return None
    if p.welcomed or bpy.app.background:
        return None
    area = _find_view3d(bpy.context)
    if area is None:
        return 1.0  # no viewport yet (e.g. still on the splash); try again shortly
    open_chat(area)
    p.welcomed = True
    return None


# ---------------------------------------------------------------------------
# Sidebar panel
# ---------------------------------------------------------------------------

EXAMPLES = [
    ("What's in my scene?", "What's in my scene? Give me a short overview."),
    ("Add a low-poly tree", "Add a simple low-poly tree next to the existing objects."),
    ("Make the cube red and shiny", "Give the selected object a glossy red material."),
    ("Set up nice lighting", "Set up a simple three-point lighting rig for the scene."),
]

_setup_cache = {"t": 0.0, "msg": None}


def _setup_problem():
    """A short message if chatting can't work yet, else None. Cached; called on every redraw."""
    now = time.time()
    if now - _setup_cache["t"] < 5:
        return _setup_cache["msg"]
    p = prefs()
    msg = None
    if p.backend == "CLI":
        if not claude_code.find_claude(bpy.path.abspath(p.claude_path) if p.claude_path else ""):
            msg = (
                "Claude Code isn't installed (or wasn't found). Install it from claude.com/claude-code, "
                "run `claude` once in a terminal to log in, then come back here."
            )
    else:
        import importlib.util
        import os

        if importlib.util.find_spec("anthropic") is None:
            msg = "API-key mode needs setup: run install_deps.py (see the README), or switch to 'Claude account' in the add-on preferences."
        elif not p.api_key.strip() and not os.environ.get("ANTHROPIC_API_KEY"):
            msg = "Add your API key in the add-on preferences, or switch to 'Claude account'."
    _setup_cache.update(t=now, msg=msg)
    return msg


class CLAUDE_OT_open_prefs(bpy.types.Operator):
    bl_idname = "claude.open_prefs"
    bl_label = "Open Settings"
    bl_description = "Open the Claude Chat add-on preferences"

    def execute(self, context):
        bpy.ops.screen.userpref_show(section="ADDONS")
        context.preferences.active_section = "ADDONS"
        context.window_manager.addon_search = "Claude Chat"
        return {"FINISHED"}

ROLE_ICONS = {"user": "USER", "assistant": "LIGHT", "tool": "TOOL_SETTINGS", "error": "ERROR", "info": "INFO"}


class VIEW3D_PT_claude_chat(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Claude"
    bl_label = "Claude"

    def draw_header_preset(self, context):
        row = self.layout.row(align=True)
        row.operator("claude.copy_chat", text="", icon="COPYDOWN")
        row.operator("claude.show_text", text="", icon="TEXT").what = "TRANSCRIPT"
        row.operator("claude.clear", text="", icon="FILE_NEW")

    def draw(self, context):
        layout = self.layout
        p = prefs()
        props = context.window_manager.claude_chat
        display, status, busy = SESSION.snapshot()

        scale = context.preferences.system.ui_scale
        text_size = _ui_text_size(context)
        # Region width minus panel margins, box padding and the leading icon, with slack
        # because Blender's label font metrics differ slightly from blf's.
        wrap_px = max(80, (context.region.width - 80 * scale) * 0.92)

        problem = _setup_problem()
        if problem:
            box = layout.box()
            box.alert = True
            col = box.column(align=True)
            for i, line in enumerate(_wrap(problem, wrap_px, text_size)):
                col.label(text=line, icon="ERROR" if i == 0 else "BLANK1")
            box.operator("claude.open_prefs", icon="PREFERENCES")

        if not display:
            box = layout.box()
            col = box.column(align=True)
            intro = (
                "Hi! I can build, change and explain things in your scene. Type below and press "
                "Enter, or try one of these:"
            )
            for i, line in enumerate(_wrap(intro, wrap_px, text_size)):
                col.label(text=line, icon="LIGHT" if i == 0 else "BLANK1")
            col.separator()
            for label, prompt in EXAMPLES:
                op = col.operator("claude.send", text=label, icon="RIGHTARROW_THIN")
                op.text = prompt
            col.separator()
            for i, line in enumerate(
                _wrap("I'll ask before running any code, and Ctrl+Z undoes my changes.", wrap_px, text_size)
            ):
                col.label(text=line, icon="INFO" if i == 0 else "BLANK1")

        shown = display[-p.messages_shown:]
        if len(display) > len(shown):
            layout.label(text=f"({len(display) - len(shown)} earlier messages in transcript)", icon="THREE_DOTS")
        for entry in shown:
            role = entry["role"]
            text = entry["text"] or ("..." if busy else "")
            if role in {"tool", "info"}:
                col = layout.column(align=True)
                col.scale_y = 0.8
                lines = _wrap(text, wrap_px, text_size)[:4]
                for i, line in enumerate(lines):
                    col.label(text=line, icon=ROLE_ICONS[role] if i == 0 else "BLANK1")
                continue
            box = layout.box()
            col = box.column(align=True)
            col.alert = role == "error"
            lines = _wrap(text, wrap_px, text_size)
            max_lines = 40
            if len(lines) > max_lines:
                col.label(text=f"... ({len(lines) - max_lines} lines above, see transcript)")
                lines = lines[-max_lines:]
            for i, line in enumerate(lines):
                col.label(text=line, icon=ROLE_ICONS.get(role, "DOT") if i == 0 else "BLANK1")

        pending = SESSION.pending
        if pending is not None:
            box = layout.box()
            box.alert = True
            col = box.column(align=True)
            col.label(text="Claude wants to run code:", icon="CONSOLE")
            for line in _wrap(pending.args.get("summary", ""), wrap_px, text_size):
                col.label(text=line)
            code_box = box.box()
            ccol = code_box.column(align=True)
            ccol.scale_y = 0.75
            code_lines = pending.args["code"].split("\n")
            for line in code_lines[:14]:
                ccol.label(text=line.replace("\t", "    ") or " ")
            if len(code_lines) > 14:
                ccol.label(text=f"... {len(code_lines) - 14} more lines")
            row = box.row(align=True)
            row.scale_y = 1.3
            row.operator("claude.approve", text="Run", icon="CHECKMARK")
            row.operator("claude.reject", text="Reject", icon="X")
            row = box.row(align=True)
            row.operator("claude.show_text", text="View all", icon="TEXT").what = "PENDING"
            row.operator("claude.approve", text="Always run").always = True
            box.label(text="Tip: type a reason below, then Reject.", icon="INFO")

        if busy:
            row = layout.row()
            row.label(text=status or "Working...", icon="SORTTIME")
            row.operator("claude.stop", text="Stop", icon="CANCEL")

        row = layout.split(factor=0.75, align=True)
        row.scale_y = 1.5
        row.prop(props, "prompt", text="", placeholder="Ask Claude anything...")
        sub = row.row(align=True)
        sub.enabled = not busy
        sub.operator("claude.send", text="Send")

        row = layout.row(align=True)
        row.prop(props, "auto_run", toggle=True, icon="SCRIPT")
        row.prop(props, "show_overlay", text="Overlay", toggle=True, icon="OVERLAY")

        if display:
            row = layout.row(align=True)
            row.operator("claude.copy_chat", text="Copy chat", icon="COPYDOWN")
            row.operator("claude.clear", text="New chat", icon="FILE_NEW")

        u = SESSION.usage
        if u["input"] or u["output"]:
            col = layout.column()
            col.scale_y = 0.7
            col.enabled = False
            col.label(
                text=f"{'Claude account' if p.backend == 'CLI' else p.model}  "
                f"in {u['input'] + u['cache_read'] + u['cache_write']:,} "
                f"(cached {u['cache_read']:,})  out {u['output']:,}"
            )


# ---------------------------------------------------------------------------
# Viewport overlay
# ---------------------------------------------------------------------------


def _draw_overlay():
    ctx = bpy.context
    try:
        props = ctx.window_manager.claude_chat
    except AttributeError:
        return
    if not props.show_overlay:
        return
    s = SESSION
    display, status, busy = s.snapshot()
    pending = s.pending is not None
    recent = (time.time() - s.finished_at) < OVERLAY_LINGER
    if not (busy or pending or (recent and display)):
        return

    region = ctx.region
    scale = ctx.preferences.system.ui_scale
    font = 0
    size = 13 * scale
    blf.size(font, size)
    max_w = min(region.width * 0.45, 640 * scale)

    header = "Claude"
    if pending:
        header = "Claude wants to run code - click the Claude button at the top to review it"
    elif busy:
        header = f"Claude: {status or 'working...'}"
    lines = [(header, (1.0, 0.75, 0.45, 1.0) if pending else (0.85, 0.8, 1.0, 1.0))]

    last = next((e for e in reversed(display) if e["role"] in {"assistant", "error"}), None)
    if last and last["text"] and not pending:
        body = _wrap(last["text"], max_w, size)
        blf.size(font, size)
        if len(body) > 5:
            body = ["..."] + body[-5:]
        color = (1.0, 0.55, 0.55, 1.0) if last["role"] == "error" else (0.95, 0.95, 0.95, 1.0)
        lines += [(b, color) for b in body]

    pad = 8 * scale
    line_h = size * 1.45
    w = max(blf.dimensions(font, t)[0] for t, _ in lines) + 2 * pad
    h = len(lines) * line_h + 2 * pad - (line_h - size)
    x = 20 * scale
    y = 20 * scale

    verts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "TRIS", {"pos": verts}, indices=[(0, 1, 2), (0, 2, 3)])
    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", (0.05, 0.05, 0.08, 0.72))
    batch.draw(shader)
    gpu.state.blend_set("NONE")

    for i, (t, color) in enumerate(lines):
        blf.color(font, *color)
        blf.position(font, x + pad, y + h - pad - size - i * line_h, 0)
        blf.draw(font, t)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

CLASSES = (
    ClaudeChatPreferences,
    ClaudeChatProps,
    CLAUDE_OT_send,
    CLAUDE_OT_quick_ask,
    CLAUDE_OT_stop,
    CLAUDE_OT_clear,
    CLAUDE_OT_approve,
    CLAUDE_OT_reject,
    CLAUDE_OT_show_text,
    CLAUDE_OT_open_chat,
    CLAUDE_OT_open_prefs,
    CLAUDE_OT_copy_chat,
    VIEW3D_PT_claude_chat,
)


def register():
    global _draw_handle
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.claude_chat = PointerProperty(type=ClaudeChatProps)
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_overlay, (), "WINDOW", "POST_PIXEL")
    bpy.app.timers.register(_tick, persistent=True)
    bridge.start()
    bpy.types.VIEW3D_MT_editor_menus.append(_draw_header_button)
    bpy.app.timers.register(_first_run, first_interval=0.5)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="3D View", space_type="VIEW_3D")
        kmi = km.keymap_items.new("claude.quick_ask", "F7", "PRESS")
        _addon_keymaps.append((km, kmi))


def unregister():
    global _draw_handle
    SESSION.stop()
    if SESSION.pending is not None:
        SESSION.pending.finish("Add-on was disabled.", True)
        SESSION.pending = None
    bridge.stop()
    bpy.types.VIEW3D_MT_editor_menus.remove(_draw_header_button)
    if bpy.app.timers.is_registered(_first_run):
        bpy.app.timers.unregister(_first_run)
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        _draw_handle = None
    for km, kmi in _addon_keymaps:
        km.keymap_items.remove(kmi)
    _addon_keymaps.clear()
    del bpy.types.WindowManager.claude_chat
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
