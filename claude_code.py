"""Run a chat turn through the user's own Claude Code install (`claude -p`), which uses their
Claude account login. Blender tools reach Claude Code through mcp_bridge.py."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from . import blender_tools, bridge

HERE = os.path.dirname(os.path.abspath(__file__))
WORK_DIR = os.path.join(tempfile.gettempdir(), "blender_claude_chat")
TOOL_NAMES = [f"mcp__blender__{t['name']}" for t in blender_tools.TOOL_DEFS]


def find_claude(override=""):
    """Locate the Claude Code executable. Prefers a real binary over npm's .cmd shim."""
    if override and os.path.isfile(override):
        return override
    candidates = []
    found = shutil.which("claude")
    if found:
        base = os.path.dirname(found)
        if sys.platform == "win32":
            candidates.append(os.path.join(base, "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"))
        if sys.platform != "win32" or found.lower().endswith(".exe"):
            candidates.append(found)
    home = os.path.expanduser("~")
    exe = "claude.exe" if sys.platform == "win32" else "claude"
    candidates += [
        os.path.join(home, ".local", "bin", exe),
        os.path.join(home, ".claude", "local", exe),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return found  # may be a .cmd shim; still works because no argument needs quoting


def _python_exe():
    # Inside Blender, sys.executable is Blender's bundled Python interpreter.
    return sys.executable


def _write_config_files(system_prompt):
    os.makedirs(WORK_DIR, exist_ok=True)
    mcp_path = os.path.join(WORK_DIR, "mcp.json")
    config = {
        "mcpServers": {
            "blender": {
                "type": "stdio",
                "command": _python_exe(),
                "args": ["-I", os.path.join(HERE, "mcp_bridge.py")],
                "env": {"BLENDER_BRIDGE_PORT": str(bridge.port()), "BLENDER_BRIDGE_TOKEN": bridge.TOKEN},
            }
        }
    }
    with open(mcp_path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    prompt_path = os.path.join(WORK_DIR, "system_prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(system_prompt)
    return mcp_path, prompt_path


def build_command(exe, cfg, session_id):
    mcp_path, prompt_path = _write_config_files(cfg["system"])
    cmd = [
        exe,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--mcp-config", mcp_path,
        "--strict-mcp-config",
        "--tools", "",  # no built-in tools (shell, file edits, web); only Blender's
        "--allowedTools", ",".join(TOOL_NAMES),
        "--permission-mode", "dontAsk",
        "--setting-sources", "project,local",  # skip the user's global hooks/plugins
        "--system-prompt-file", prompt_path,
    ]
    if cfg.get("model") and cfg["model"] != "default":
        cmd += ["--model", cfg["model"]]
    if cfg.get("effort"):
        cmd += ["--effort", cfg["effort"]]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


def child_env():
    env = dict(os.environ)
    # Use the Claude account login, not an API key that happens to be in the environment.
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    env["MCP_TOOL_TIMEOUT"] = str(24 * 3600 * 1000)  # approvals can take a while
    env.setdefault("MCP_TIMEOUT", "30000")
    return env


LOGIN_HINT = (
    "Claude Code isn't logged in. Open a terminal, run `claude`, and log in with your "
    "Claude account (or type /login), then try again."
)


def run_turn(session, prompt, cfg):
    """Stream one turn. Runs on the session's worker thread."""
    exe = find_claude(cfg.get("claude_path", ""))
    if not exe:
        session._add(
            "error",
            "Couldn't find Claude Code. Install it (see claude.com/claude-code), or set its path "
            "in the add-on preferences.",
        )
        return

    cmd = build_command(exe, cfg, session.cli_session_id)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        cmd,
        cwd=WORK_DIR,
        env=child_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    session.proc = proc
    stderr_lines = []
    err_reader = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
    err_reader.start()
    proc.stdin.write(prompt)
    proc.stdin.close()

    entry = None
    saw_text_delta = False
    got_result = False
    try:
        for line in proc.stdout:
            if session.cancel.is_set():
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                session.cli_session_id = ev.get("session_id") or session.cli_session_id
                session._set_status("Thinking...")
            elif t == "stream_event":
                e = ev.get("event", {})
                et = e.get("type")
                if et == "message_start":
                    saw_text_delta = False
                    entry = None  # each model message gets its own bubble, in order with tool calls
                elif et == "content_block_start":
                    kind = e.get("content_block", {}).get("type")
                    if kind == "thinking":
                        session._set_status("Thinking...")
                    elif kind == "tool_use":
                        entry = None  # text after a tool call starts a new bubble
                        name = e["content_block"].get("name", "").replace("mcp__blender__", "")
                        session._set_status(f"Preparing {name}...")
                    elif kind == "text":
                        session._set_status("Writing...")
                        if entry is not None and entry["text"]:
                            session._append_text(entry, "\n\n")
                elif et == "content_block_delta":
                    d = e.get("delta", {})
                    if d.get("type") == "text_delta":
                        saw_text_delta = True
                        if entry is None:
                            entry = session._add("assistant", "")
                        session._append_text(entry, d.get("text", ""))
                    elif d.get("type") == "thinking_delta":
                        snippet = " ".join(d.get("thinking", "").split())[-70:]
                        if snippet:
                            session._set_status("Thinking: " + snippet)
            elif t == "assistant" and not saw_text_delta:
                # No partial events for this message; show its text directly.
                entry = None
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        if entry is None:
                            entry = session._add("assistant", "")
                        elif entry["text"]:
                            session._append_text(entry, "\n\n")
                        session._append_text(entry, block["text"])
            elif t == "result":
                got_result = True
                session.cli_session_id = ev.get("session_id") or session.cli_session_id
                usage = ev.get("usage") or {}
                with session.lock:
                    session.usage["input"] += usage.get("input_tokens", 0) or 0
                    session.usage["output"] += usage.get("output_tokens", 0) or 0
                    session.usage["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
                    session.usage["cache_write"] += usage.get("cache_creation_input_tokens", 0) or 0
                if ev.get("is_error"):
                    text = str(ev.get("result") or ev.get("subtype") or "Claude Code reported an error.")
                    low = text.lower()
                    if "login" in low or "log in" in low or "api key" in low or "auth" in low:
                        text = LOGIN_HINT + f"\n({text.strip()[:200]})"
                    session._add("error", text)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        session.proc = None
        err_reader.join(timeout=2)

    if session.cancel.is_set():
        session._add("info", "Stopped.")
    elif not got_result:
        err = "".join(stderr_lines).strip()
        low = err.lower()
        if "login" in low or "auth" in low:
            session._add("error", LOGIN_HINT)
        else:
            session._add("error", "Claude Code exited unexpectedly." + (f"\n{err[-600:]}" if err else ""))
