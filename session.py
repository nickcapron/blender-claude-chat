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
- view_image shows you an image file, an image datablock (such as a baked texture) or the \
last render. Look at renders and bakes you make before calling the work done.
- check_animation_loop compares a looping clip's last frame with its first in the evaluated \
scene and checks the motion across the seam. Run it on every looping clip before calling a \
loop seamless; matching first and last key values doesn't prove it.

Working in Blender:
- Prefer the data API (bpy.data, object and node properties) over bpy.ops where practical. \
Operators depend on context; code runs with the 3D Viewport as context.
- Write code for the Blender {version} Python API. If something errors, read the traceback \
and fix it rather than repeating the same call.
- Keep changes focused on what was asked. Don't delete, rename or overwrite existing work \
unless the user asked for it; if a request is ambiguous and would destroy work, ask first.
- Name new objects and materials meaningfully, and put larger builds in their own collection.
- To use another .blend while working in the open one, append or link from it \
(bpy.data.libraries.load) rather than opening it, which would replace the user's scene.

Animation:
- Actions are layered in Blender 5: `action.fcurves` no longer exists. F-curves live in \
`action.layers[i].strips[j].channelbags[k].fcurves`, or `strip.channelbag(slot)` for one slot.
- Objects rotate and scale around their origin. Moving mesh data inside an animated \
hierarchy (for example to recentre a model) also moves it off the pivot it turns around, so \
recentre by moving objects or empties and keep each rotating part on its own axis.
- For a looping clip, give the F-curves a Cycles modifier so auto handles wrap around the \
loop; otherwise the first and last keys get flat handles and the motion pauses at the seam.

Finishing a task:
- When the task comes from a written spec or checklist, go back through it before you say \
you're done, and tell the user briefly about anything you skipped, couldn't do, or did \
differently, and why.
- Don't change settings the task didn't mention (texture filtering, export options and \
so on) without telling the user.
- Only report what you checked. If you didn't verify something, say so instead of \
describing it as done.

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
        self.cli_session_id = None  # Claude Code session to resume (account mode)
        self.proc = None  # running Claude Code process (account mode)

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
            self.cli_session_id = None
            self.display.clear()
            self.status = ""
            self.usage = {k: 0 for k in self.usage}
            self._dirty = True
        return True

    def export_markdown(self, environment):
        """The whole chat, including every tool call's input and output, as Markdown that reads
        well when pasted into a chat or an issue. environment: list of (label, value)."""
        with self.lock:
            entries = [dict(e) for e in self.display]
            pending = self.pending
            usage = dict(self.usage)
        out = ["## crender chat export", ""]
        out += [f"- **{k}:** {v}" for k, v in environment]
        if usage["input"] or usage["output"]:
            out.append(
                f"- **Tokens:** in {usage['input'] + usage['cache_read'] + usage['cache_write']:,} "
                f"(cached {usage['cache_read']:,}), out {usage['output']:,}"
            )
        out.append("")
        for e in entries:
            role, text = e["role"], e["text"]
            if role == "user":
                out += ["### You", "", e.get("full", text), ""]
            elif role == "assistant":
                out += ["### Claude", "", text, ""]
            elif role == "tool":
                d = e.get("detail")
                if not d:
                    out += [f"> **Tool:** {text}", ""]
                    continue
                status = "error" if d["is_error"] else "ok"
                out.append(f"**Tool `{d['name']}`** ({status})")
                args = d["input"] if isinstance(d["input"], dict) else {"input": d["input"]}
                code = args.get("code")
                rest = {k: v for k, v in args.items() if k != "code"}
                if rest:
                    out.append(f"- input: `{json.dumps(rest, default=str)}`")
                if code:
                    out += ["", "```python", code.rstrip(), "```"]
                output = d["output"].rstrip()
                try:  # compact JSON (scene summaries) so pasted chats stay readable
                    output = json.dumps(json.loads(output), separators=(",", ":"))
                    if len(output) > 2500:
                        output = output[:2500] + " ...(truncated)"
                except ValueError:
                    pass
                out += ["", "<details><summary>output</summary>", "", "```", output, "```", "", "</details>", ""]
            elif role == "error":
                out += [f"> **Error:** {text}", ""]
            else:
                out += [f"> {text}", ""]
        if pending is not None:
            out += ["**Waiting for approval:** " + pending.args.get("summary", ""), "",
                    "```python", pending.args.get("code", "").rstrip(), "```", ""]
        return "\n".join(out).rstrip() + "\n"

    def transcript(self):
        with self.lock:
            labels = {"user": "You", "assistant": "Claude", "tool": "  [tool]", "error": "  [error]", "info": "  [info]"}
            return "\n\n".join(f"{labels.get(e['role'], e['role'])}: {e['text']}" for e in self.display)

    # -- public API -------------------------------------------------------
    def send(self, user_text, context_line, cfg, display_text=None):
        """Start a turn. cfg: backend, api_key, claude_path, model, effort, version, tools.
        display_text: a shorter version for the sidebar (e.g. for a long pasted prompt)."""
        if self.busy:
            return False
        self.cancel.clear()
        with self.lock:
            self.busy = True
            self.status = "Sending..."
            self._dirty = True
        entry = self._add("user", display_text or user_text)
        if display_text:
            entry["full"] = user_text
        prompt = f"<blender_context>{context_line}</blender_context>\n\n{user_text}"
        cfg = dict(cfg, system=SYSTEM_PROMPT.format(version=cfg["version"]))
        if cfg.get("backend") == "API":
            self.messages.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
            target, args = self._run, (cfg,)
        else:
            target, args = self._run_cli, (prompt, cfg)
        self._thread = threading.Thread(target=target, args=args, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self.cancel.set()
        self._set_status("Stopping...")
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    # -- worker -----------------------------------------------------------
    def _finish(self):
        with self.lock:
            self.busy = False
            self.status = ""
            self.finished_at = time.time()
            self._dirty = True

    def _run_cli(self, prompt, cfg):
        from . import claude_code

        try:
            claude_code.run_turn(self, prompt, cfg)
        except Exception:
            self._add("error", "Unexpected error:\n" + traceback.format_exc(limit=4))
        finally:
            self._finish()

    def _run(self, cfg):
        try:
            import anthropic  # only needed in API-key mode (installed by install_deps.py)
        except ImportError:
            self._add(
                "error",
                "API-key mode needs the anthropic package. Run:\n"
                "blender --background --python <add-on folder>/install_deps.py\n"
                "or switch to 'Claude account' in the add-on preferences.",
            )
            self._finish()
            return
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
            self._finish()

    def _request_kwargs(self, cfg):
        model = cfg["model"]
        kwargs = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=cfg["system"],
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
        if truncated:
            content, is_error = "Tool input was cut off by the output length limit; not run. Use smaller steps.", True
            self._add("tool", f"-> {block.name}  [x] {content}")
        else:
            content, is_error = self.execute_tool(block.name, block.input)
        r = {"type": "tool_result", "tool_use_id": block.id, "content": content}
        if is_error:
            r["is_error"] = True
        return r

    def execute_tool(self, name, args):
        """Validate, show in the chat, and run a tool on Blender's main thread (waiting for
        approval if needed). Called from a worker or bridge thread. Returns (content, is_error)."""
        from . import blender_tools  # validate() doesn't touch bpy state

        label = name
        if name == "execute_python" and isinstance(args, dict):
            label = f"run code: {args.get('summary') or '...'}"
        elif name == "get_object_details" and isinstance(args, dict):
            label = f"inspect {args.get('name')!r}"
        elif name == "check_animation_loop" and isinstance(args, dict):
            label = f"check loop {args.get('start')}-{args.get('end')}"
            if args.get("nla_track"):
                label += f" ({args['nla_track']})"
        tool_entry = self._add("tool", f"-> {label}")

        def done(content, is_error):
            mark = "x" if is_error else "ok"
            if isinstance(content, str):
                full = content
            else:  # list of blocks, e.g. a screenshot
                full = "\n".join(
                    b.get("text", "") if b.get("type") == "text" else f"[{b.get('type')}]" for b in content
                )
            preview = " ".join(full.split())[:160]
            with self.lock:
                tool_entry["detail"] = {"name": name, "input": args, "output": full[:6000], "is_error": is_error}
            self._append_text(tool_entry, f"  [{mark}] {preview}")
            return content, is_error

        if self.cancel.is_set():
            return done("Cancelled by the user.", True)
        err = blender_tools.validate(name, args)
        if err:
            return done(f"INVALID_INPUT: {err} Input was: {json.dumps(args, default=str)[:500]}", True)

        self._set_status(f"Running {name}...")
        call = ToolCall(None, name, args)
        self.calls.put(call)
        call.done.wait()  # the main thread always finishes calls (stop() rejects pending ones)
        self._set_status("Thinking...")
        return done(call.content, call.is_error)


SESSION = ChatSession()
