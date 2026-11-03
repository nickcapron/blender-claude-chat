"""Localhost socket that mcp_bridge.py (run by Claude Code) uses to call Blender tools."""

import json
import secrets
import socketserver
import threading

from . import blender_tools

TOKEN = secrets.token_hex(16)
_server = None


def _to_mcp(content):
    """Convert tool output (str or Anthropic-style blocks) to MCP content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out = []
    for block in content:
        if block.get("type") == "image":
            src = block["source"]
            out.append({"type": "image", "data": src["data"], "mimeType": src["media_type"]})
        elif block.get("type") == "text":
            out.append({"type": "text", "text": block["text"]})
    return out


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self._serve()
        except (ConnectionError, OSError):
            pass  # Claude Code exited (turn finished or was stopped)

    def _serve(self):
        from .session import SESSION

        for line in self.rfile:
            try:
                req = json.loads(line)
            except ValueError:
                return
            if not secrets.compare_digest(str(req.get("token", "")), TOKEN):
                return
            if req.get("op") == "list":
                tools = [
                    {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
                    for t in blender_tools.TOOL_DEFS
                ]
                resp = {"tools": tools}
            elif req.get("op") == "call":
                if not SESSION.busy:
                    content, is_error = "No chat turn is active in Blender.", True
                else:
                    content, is_error = SESSION.execute_tool(req.get("name"), req.get("arguments") or {})
                resp = {"content": _to_mcp(content), "isError": is_error}
            else:
                resp = {"content": [{"type": "text", "text": "Unknown op"}], "isError": True}
            self.wfile.write(json.dumps(resp).encode("utf-8") + b"\n")
            self.wfile.flush()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def start():
    global _server
    if _server is None:
        _server = _Server(("127.0.0.1", 0), _Handler)
        threading.Thread(target=_server.serve_forever, daemon=True).start()
    return _server.server_address[1]


def port():
    return _server.server_address[1] if _server else start()


def stop():
    global _server
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
