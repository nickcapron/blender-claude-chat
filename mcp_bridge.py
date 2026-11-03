"""Minimal MCP stdio server that forwards Blender tool calls to the Claude Chat add-on.

Claude Code launches this script (with Blender's bundled Python) when the add-on runs it in
"Claude account" mode. It speaks MCP JSON-RPC on stdin/stdout and relays tools/list and
tools/call to the add-on over a token-protected localhost socket. Standard library only.
"""

import json
import os
import socket
import sys

PORT = int(os.environ.get("BLENDER_BRIDGE_PORT", "0"))
TOKEN = os.environ.get("BLENDER_BRIDGE_TOKEN", "")

_conn = None


def _blender(request):
    global _conn
    request = dict(request, token=TOKEN)
    for attempt in range(2):
        try:
            if _conn is None:
                sock = socket.create_connection(("127.0.0.1", PORT), timeout=10)
                sock.settimeout(None)  # tool calls can wait on user approval
                _conn = (sock, sock.makefile("rb"))
            sock, rfile = _conn
            sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
            line = rfile.readline()
            if not line:
                raise ConnectionError("Blender closed the connection")
            return json.loads(line)
        except (OSError, ConnectionError, ValueError):
            _conn = None
            if attempt:
                raise


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(msg_id, result):
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _error(msg_id, code, message):
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        msg_id = msg.get("id")
        method = msg.get("method")
        if msg_id is None:  # notification
            continue
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                _result(
                    msg_id,
                    {
                        "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "blender", "version": "0.2.0"},
                    },
                )
            elif method == "ping":
                _result(msg_id, {})
            elif method == "tools/list":
                _result(msg_id, {"tools": _blender({"op": "list"})["tools"]})
            elif method == "tools/call":
                resp = _blender(
                    {"op": "call", "name": params.get("name"), "arguments": params.get("arguments") or {}}
                )
                _result(msg_id, {"content": resp["content"], "isError": bool(resp.get("isError"))})
            else:
                _error(msg_id, -32601, f"Method not found: {method}")
        except Exception as e:  # Blender unreachable, etc.
            if method == "tools/call":
                _result(msg_id, {"content": [{"type": "text", "text": f"Blender is not reachable: {e}"}], "isError": True})
            else:
                _error(msg_id, -32603, f"Blender is not reachable: {e}")


if __name__ == "__main__":
    main()
