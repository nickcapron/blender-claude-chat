"""Chat session: conversation state plus a worker thread that talks to the Claude API.

bpy is not thread-safe, so the worker never touches Blender. When Claude calls a tool,
the worker queues a ToolCall and waits; a timer on Blender's main thread runs it
(see ui.py) and hands back the result.
"""

import json
import queue
import threading
import time
import traceback

import anthropic

MAX_TOOL_ROUNDS = 40
MAX_TOKENS = 64000

SYSTEM_PROMPT = """\
You are Claude, working inside Blender {version} as a chat assistant docked in the 3D \
Viewport sidebar. The user is working on their scene alongside you, and you can inspect \
and change the open scene through tools.

Tools:
- get_scene_summary and get_object_details show you the scene. Look before you change \
things, so your code uses real object, material and collection names.
- execute_python runs bpy code in the live session. The user may have to approve each run. \
Every run is a separate undo step, so the user can revert with Ctrl+Z. Variables persist \
between runs. Use print() or assign to `result` to see values.
- capture_viewport returns a screenshot of the 3D Viewport. Use it to check visual results \
such as layout, materials or lighting, when looking would tell you something the data \
doesn't.

Working in Blender:
- Prefer the data API (bpy.data, object and node properties) over bpy.ops where practical. \
Operators depend on context; code runs with the 3D Viewport as context.
- Write code for the Blender {version} Python API. If something errors, read the traceback \
and fix it rather than repeating the same call.
- Keep changes focused on what was asked. Don't delete, rename or overwrite existing work \
unless the user asked for it; if a request is ambiguous and would destroy work, ask first.
- Name new objects and materials meaningfully, and put larger builds in their own collection.

Each user message starts with a <blender_context> line describing the current mode, \
selection and frame at the moment they hit send. "This", "it" or "the selected object" \
usually refers to that selection.

Replies appear in a narrow sidebar that shows plain text (no markdown rendering). Keep \
them short: say what you did or found in a few sentences. Don't paste code into the chat \
unless the user asks to see it; run it instead.\
"""


class ToolCall:
    def __init__(self, tool_use_id, name, args):
        self.id = tool_use_id
        self.name = name
        self.args = args
        self.content = None
        self.is_error = False
        self.done = threading.Event()

    def finish(self, content, is_error=False):
        self.content = content
        self.is_error = is_error
        self.done.set()


class ChatSession:
    def __init__(self):
        self.lock = threading.RLock()
        self.messages = []  # API history; append-only
        self.display = []  # [{"role": user|assistant|tool|error|info, "text": str}]
        self.busy = False
        self.status = ""
        self.finished_at = 0.0
        self.cancel = threading.Event()
        self.calls = queue.Queue()  # ToolCall objects for the main thread
        self.pending = None  # ToolCall awaiting user approval
        self.usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._dirty = True
        self._thread = None

    # -- display helpers (thread-safe) ------------------------------------
    def _add(self, role, text):
        with self.lock:
            entry = {"role": role, "text": text}
            self.display.append(entry)
            self._dirty = True
            return entry

    def _append_text(self, entry, text):
        with self.lock:
            entry["text"] += text
            self._dirty = True

    def _set_status(self, text):
        with self.lock:
            self.status = text
            self._dirty = True

    def mark_dirty(self):
        with self.lock:
            self._dirty = True

    def consume_dirty(self):
        with self.lock:
            d, self._dirty = self._dirty, False
            return d

    def snapshot(self):
        with self.lock:
            return [dict(e) for e in self.display], self.status, self.busy

    def clear(self):
        if self.busy:
            return False
        with self.lock:
            self.messages.clear()
            self.display.clear()
            self.status = ""
            self.usage = {k: 0 for k in self.usage}
            self._dirty = True
        return True

    def transcript(self):
        with self.lock:
            labels = {"user": "You", "assistant": "Claude", "tool": "  [tool]", "error": "  [error]", "info": "  [info]"}
            return "\n\n".join(f"{labels.get(e['role'], e['role'])}: {e['text']}" for e in self.display)

    # -- public API -------------------------------------------------------
    def send(self, user_text, context_line, cfg):
        """Start a turn. cfg: api_key, model, effort, version."""
        if self.busy:
            return False
        self.cancel.clear()
        with self.lock:
            self.busy = True
            self.status = "Sending..."
            self._dirty = True
        self._add("user", user_text)
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"<blender_context>{context_line}</blender_context>\n\n{user_text}",
                    }
                ],
            }
        )
        self._thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self.cancel.set()
        self._set_status("Stopping...")

    # -- worker -----------------------------------------------------------
    def _run(self, cfg):
        try:
            client = anthropic.Anthropic(api_key=cfg.get("api_key") or None, max_retries=3)
            self._loop(client, cfg)
        except anthropic.AuthenticationError:
            self._add(
                "error",
                "Authentication failed. Add your API key in Edit > Preferences > Add-ons > "
                "Claude Chat, or set the ANTHROPIC_API_KEY environment variable.",
            )
        except anthropic.PermissionDeniedError as e:
            self._add("error", f"Permission denied: {e.message}")
        except anthropic.NotFoundError as e:
            self._add("error", f"Not found (check the model setting): {e.message}")
        except anthropic.RateLimitError:
            self._add("error", "Rate limited by the API. Wait a moment and try again.")
        except anthropic.BadRequestError as e:
            self._add("error", f"Bad request: {e.message}")
        except anthropic.APIStatusError as e:
            self._add("error", f"API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            self._add("error", "Couldn't reach the Claude API. Check your internet connection.")
        except anthropic.AnthropicError as e:
            # e.g. no credentials could be resolved
            self._add("error", f"{e}\nSet an API key in the add-on preferences.")
        except Exception:
            self._add("error", "Unexpected error:\n" + traceback.format_exc(limit=4))
        finally:
            with self.lock:
                self.busy = False
                self.status = ""
                self.finished_at = time.time()
                self._dirty = True

    def _request_kwargs(self, cfg):
        model = cfg["model"]
        kwargs = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT.format(version=cfg["version"]),
            tools=cfg["tools"],
            messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": cfg["effort"]},
            cache_control={"type": "ephemeral"},
        )
        # Server-side refusal fallback; Haiku has no fallback target.
        if not model.startswith("claude-haiku"):
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _stream_once(self, client, cfg):
        """Stream one model turn. Returns the final message, or None if cancelled."""
        entry = None
        need_sep = False
        with client.beta.messages.stream(**self._request_kwargs(cfg)) as stream:
            for event in stream:
                if self.cancel.is_set():
                    return None
                t = event.type
                if t == "content_block_start":
                    kind = event.content_block.type
                    if kind == "thinking":
                        self._set_status("Thinking...")
                    elif kind == "tool_use":
                        self._set_status(f"Preparing {event.content_block.name}...")
                    elif kind == "text":
                        self._set_status("Writing...")
                        if entry is None:
                            entry = self._add("assistant", "")
                        elif entry["text"]:
                            need_sep = True
                elif t == "text":
                    if entry is None:
                        entry = self._add("assistant", "")
                    if need_sep:
                        self._append_text(entry, "\n\n")
                        need_sep = False
                    self._append_text(entry, event.text)
                elif t == "thinking":
                    snippet = " ".join(event.snapshot.split())[-70:]
                    if snippet:
                        self._set_status("Thinking: " + snippet)
            message = stream.get_final_message()
        u = message.usage
        with self.lock:
            self.usage["input"] += u.input_tokens or 0
            self.usage["output"] += u.output_tokens or 0
            self.usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            self.usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        if message.model and message.model != cfg["model"]:
            self._add("info", f"(answered by fallback model {message.model})")
        return message

    def _loop(self, client, cfg):
        json_retries = 0
        for _ in range(MAX_TOOL_ROUNDS):
            if self.cancel.is_set():
                self._add("info", "Stopped.")
                return
            try:
                message = self._stream_once(client, cfg)
                json_retries = 0
            except ValueError:
                # Tool input JSON the SDK couldn't parse at all; re-issue the turn.
                json_retries += 1
                if json_retries > 2:
                    raise
                continue
            if message is None:
                self._add("info", "Stopped.")
                return

            if message.stop_reason == "refusal":
                # Discard the partial turn; the user can rephrase and continue.
                cat = getattr(message.stop_details, "category", None) if message.stop_details else None
                self._add("error", "Claude declined this request" + (f" ({cat})." if cat else "."))
                return

            self.messages.append({"role": "assistant", "content": message.content})

            if message.stop_reason == "pause_turn":
                continue

            tool_uses = [b for b in message.content if b.type == "tool_use"]
            if not tool_uses:
                if message.stop_reason == "max_tokens":
                    self._add("info", "(reply hit the length limit)")
                return

            results = []
            for block in tool_uses:
                results.append(self._run_tool(block, truncated=message.stop_reason == "max_tokens"))
            self.messages.append({"role": "user", "content": results})
            if self.cancel.is_set():
                self._add("info", "Stopped.")
                return
        self._add("info", f"Stopped after {MAX_TOOL_ROUNDS} tool rounds. Say 'continue' to keep going.")

    def _run_tool(self, block, truncated):
        from . import blender_tools  # validate() doesn't touch bpy state

        args = block.input
        label = block.name
        if block.name == "execute_python" and isinstance(args, dict):
            label = f"run code: {args.get('summary') or '...'}"
        elif block.name == "get_object_details" and isinstance(args, dict):
            label = f"inspect {args.get('name')!r}"
        tool_entry = self._add("tool", f"-> {label}")

        def result(content, is_error):
            mark = "x" if is_error else "ok"
            preview = content if isinstance(content, str) else "image"
            preview = " ".join(preview.split())[:160]
            self._append_text(tool_entry, f"  [{mark}] {preview}")
            r = {"type": "tool_result", "tool_use_id": block.id, "content": content}
            if is_error:
                r["is_error"] = True
            return r

        if truncated:
            return result("Tool input was cut off by the output length limit; not run. Use smaller steps.", True)
        if self.cancel.is_set():
            return result("Cancelled by the user.", True)
        err = blender_tools.validate(block.name, args)
        if err:
            return result(f"INVALID_INPUT: {err} Input was: {json.dumps(args, default=str)[:500]}", True)

        self._set_status(f"Running {block.name}...")
        call = ToolCall(block.id, block.name, args)
        self.calls.put(call)
        while not call.done.wait(0.1):
            pass  # the main thread always finishes calls (stop() rejects pending ones)
        return result(call.content, call.is_error)
