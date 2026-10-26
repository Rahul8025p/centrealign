"""
The agent loop.

There is NO fixed plan here. Each turn the model sees the goal, its working memory and the history of
tool results, and chooses the next tool call (or finishes). The loop only does the plumbing:

    observe  -> the model reads the latest tool result
    decide   -> the model picks the next tool call
    act      -> the Toolbox runs it (every write passes the Guard first)
    repeat   -> until `finish` is accepted, the user stops it, or the step budget runs out

Terminal states (memory.status): completed, failed, needs_input, declined, already_submitted,
already_paid, discrepancy.
"""
import datetime
import json
import os
import time
import uuid

from .guard import Guard
from .llm import LLMClient, LLMError
from .memory import AgentMemory
from .prompts import build_system_prompt
from .tools.api_tool import ApiTool
from .tools.browser_tool import BrowserTool
from .tools.file_tool import FileTool
from .tools.toolbox import TOOL_SPECS, HaltRun, Toolbox
from .tools.verifier_tool import VerifierTool

FINISH_STATUSES = {"completed", "already_submitted", "already_paid", "discrepancy", "declined", "needs_input", "failed"}
MAX_TOOL_RESULT_CHARS = 4500
DEFAULT_CONTEXT_CHARS = 40000


class TaskOrchestrator:
    def __init__(self, base_url: str = "http://localhost:5000", headless: bool = True,
                 approval_threshold: float = 100000, llm: LLMClient = None, max_steps: int = 30):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.max_steps = max_steps
        self.llm = llm or LLMClient.from_env()
        self.file_tool = FileTool()
        self.api_tool = ApiTool(base_url=self.base_url)
        self.verifier_tool = VerifierTool(base_url=self.base_url)
        self.guard = Guard(self.api_tool, self.verifier_tool, approval_threshold)
        self.browser_tool = None
        self._emit = lambda *_a, **_k: None
        self._ask_user = None
        self._decision = {}

    # ------------------------------------------------------------------ plumbing
    def _get_browser(self):
        if self.browser_tool is None:
            self.browser_tool = BrowserTool(headless=self.headless)
        return self.browser_tool

    def _close_browser(self):
        if self.browser_tool:
            self.browser_tool.close()
            self.browser_tool = None

    def _ask(self, memory, kind: str, question: str, options=None, details=None):
        """Emit a question and block for the human's answer. Returns the response dict, or None."""
        memory.needs_human, memory.human_question = True, question
        self._emit("needs_input", {"kind": kind, "question": question, "options": options or [],
                                   "details": details or {}, "interactive": self._ask_user is not None})
        resp = None
        if self._ask_user:
            try:
                resp = self._ask_user({"kind": kind, "question": question, "options": options or []})
            except Exception:
                resp = None
        memory.human_response = resp
        memory.needs_human = resp is None
        return resp

    def _end(self, memory, status: str, summary: str):
        memory.status = status
        memory.is_completed = status == "completed"
        payload = {"status": status, "summary": summary, "message": summary,
                   "evidence": memory.evidence, "entities": memory.discovered_entities}
        self._emit("completed" if status == "completed" else "halted", payload)
        result = memory.to_dict()
        result["summary"] = summary
        return result

    # --------------------------------------------------------------- entry point
    def run_task(self, prompt: str, task_id: str = None, on_event=None, ask_user=None):
        """
        Run one task. `on_event(dict)` receives progress events. `ask_user(question_dict)` may block and
        returns {"answer": str} | {"approved": bool} | None; with no callback the run stops at the first question.
        """
        task_id = task_id or f"task_{uuid.uuid4().hex[:8]}"
        memory = AgentMemory(task_id=task_id, prompt=prompt)

        def emit(event_type, data):
            payload = {"type": event_type, "task_id": task_id, "timestamp": time.strftime("%H:%M:%S"), **data}
            if on_event:
                on_event(payload)
            return payload

        self._emit, self._ask_user = emit, ask_user
        try:
            return self._loop(memory)
        except Exception as e:  # last-resort safety net: report, never crash the server thread
            emit("error", {"message": f"Execution error: {e}"})
            return self._end(memory, "failed", f"The run stopped on an unexpected error: {e}")
        finally:
            self._close_browser()

    # ------------------------------------------------------------------ the loop
    def _loop(self, memory: AgentMemory):
        emit = self._emit
        emit("status", {"message": f"Starting autonomous execution for goal: '{memory.prompt}'"})
        ready, problem = self.llm.check() if hasattr(self.llm, "check") else (True, "")
        if not ready:
            emit("error", {"message": problem})
            return self._end(memory, "failed", problem)
        # leave room for the system prompt and tool schemas inside the model's context window
        budget = max(8000, getattr(self.llm, "context_chars", DEFAULT_CONTEXT_CHARS) - 12000)

        self.guard.start(memory)
        toolbox = Toolbox(memory, self.base_url, self.file_tool, self.api_tool, self.verifier_tool, self.guard,
                          self._get_browser,
                          lambda kind, q, opts=None, details=None: self._ask(memory, kind, q, opts, details), emit)
        today = datetime.date.today().isoformat()
        messages = [{"role": "system", "content": build_system_prompt(self.base_url, today)},
                    {"role": "user", "content": memory.prompt}]
        failures, nudges = {}, 0

        try:
            for step in range(1, self.max_steps + 1):
                self._compact(messages, budget)
                emit("thinking", {"step": step})
                started = time.time()
                try:
                    # The system prompt + tools + history form a prefix that does not change between turns, so
                    # Ollama can reuse its cache of it. Only the small working-memory note at the end is new.
                    note = memory.working_memory_text()
                    turn = messages + ([{"role": "system", "content": "WORKING MEMORY (maintained by the system):\n" + note}]
                                       if note else [])
                    reply = self.llm.chat(turn, TOOL_SPECS)
                    self._decision = {"model": getattr(self.llm, "model", "?"), "seconds": round(time.time() - started, 1)}
                except LLMError as e:
                    emit("error", {"message": str(e)})
                    return self._end(memory, "failed", str(e))

                content, calls = reply.get("content"), reply.get("tool_calls") or []
                if content and calls:
                    emit("thought", {"thought": content})

                if not calls and not (content or "").strip():  # empty reply (happens with small models)
                    nudges += 1
                    if nudges > 3:
                        return self._end(memory, "failed", "The model kept returning empty replies, so the run stopped.")
                    messages.append({"role": "user", "content": "Your reply was empty. Call a tool, or call finish with the outcome."})
                    continue

                if not calls:  # plain-text reply
                    if not memory.history or nudges >= 2:  # a direct answer, or the model won't call finish
                        done, rejection = self._conclude(memory, "completed", content or "", None)
                        if done:
                            return done
                        messages += [{"role": "assistant", "content": content or ""},
                                     {"role": "user", "content": rejection}]
                        continue
                    nudges += 1
                    messages += [{"role": "assistant", "content": content or ""},
                                 {"role": "user", "content": "If the task is done, call the finish tool with the outcome. "
                                                             "Otherwise continue with tools."}]
                    continue

                messages.append({"role": "assistant", "content": content or "", "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["raw_arguments"]}} for c in calls]})

                for call in calls:
                    if call["name"] == "finish":
                        done, feedback = self._handle_finish(memory, call)
                        if done:
                            return done               # accepted: the run is over
                        messages.append(self._tool_message(call["id"], feedback))
                        continue
                    result = self._call_tool(toolbox, memory, step, call, failures, content)
                    messages.append(self._tool_message(call["id"], result))

            return self._end(memory, "failed",
                             f"The agent used its full budget of {self.max_steps} steps without finishing, so it stopped.")
        except HaltRun as h:
            return self._end(memory, h.status, h.message)

    # --------------------------------------------------------------- tool calls
    def _call_tool(self, toolbox, memory, step, call, failures, thought):
        name, args = call["name"], call["arguments"]
        decision = getattr(self, "_decision", {})
        self._emit("tool_call", {"step": step, "tool": name, "args": self._display_args(args), **decision})
        if os.environ.get("AGENT_DEBUG"):
            print(f"[agent] step {step}: {decision.get('model')} chose {name}({self._display_args(args)}) "
                  f"after {decision.get('seconds')}s", flush=True)
        result = toolbox.execute(name, args)
        ok = result.get("ok", True) is not False
        blocked = bool(result.get("blocked_by_guard"))

        if not ok and not blocked:  # stuck detector: the same call failing again and again
            key = (name, json.dumps(args, sort_keys=True, default=str))
            failures[key] = failures.get(key, 0) + 1
            if failures[key] >= 3:
                result["stuck"] = ("This exact call has now failed 3 times. Change your approach, use another tool, "
                                   "ask the user, or finish with status 'failed'.")
            if failures[key] >= 5:
                raise HaltRun("failed", "The agent got stuck repeating an action that kept failing, so it stopped.")

        screenshot = result.pop("screenshot", None)
        obs = self._short(name, result, ok, blocked)
        memory.record_step(thought or "", f"{name}({self._display_args(args)})", name, obs,
                           "success" if ok else "failed", screenshot)
        self._emit("tool_result", {"step": step, "tool": name, "ok": ok, "blocked": blocked,
                                   "observation": obs, "screenshot": screenshot})
        return result

    def _handle_finish(self, memory, call):
        args = call["arguments"]
        status, summary = args.get("status"), str(args.get("summary", "")).strip()
        if status not in FINISH_STATUSES:
            return None, {"ok": False, "error": f"status must be one of {sorted(FINISH_STATUSES)}"}
        done, rejection = self._conclude(memory, status, summary, args.get("confirmation_id"))
        return done, {"ok": False, "blocked_by_guard": True, "error": rejection}

    def _conclude(self, memory, status, summary, confirmation_id):
        """
        Accept the model's ending only if the guard agrees (a success claim must be independently verified).
        Returns (result, "") when the run is over, or (None, reason) when the model must keep working.
        """
        ok, message = self.guard.review_finish(memory, status, summary, confirmation_id)
        if ok:
            return self._end(memory, status, summary), ""
        memory.finish_rejections += 1
        self._emit("tool_result", {"step": len(memory.history) + 1, "tool": "finish", "ok": False, "blocked": True,
                                   "observation": f"⛔ Completion rejected: {message}", "screenshot": None})
        if memory.finish_rejections >= 2:
            return self._end(memory, "failed", f"The agent could not substantiate its result. {message}"), ""
        return None, message

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _tool_message(call_id: str, result: dict) -> dict:
        text = json.dumps(result, default=str, ensure_ascii=False)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = text[:MAX_TOOL_RESULT_CHARS] + " …[truncated]"
        return {"role": "tool", "tool_call_id": call_id, "content": text}

    @staticmethod
    def _compact(messages, budget: int = 90000):
        """Keep the context bounded: shrink old tool outputs once the conversation gets long."""
        if sum(len(m.get("content") or "") for m in messages) <= budget:
            return
        for m in messages[1:-8]:
            if m.get("role") == "tool" and len(m["content"]) > 500:
                m["content"] = m["content"][:400] + " …[older output truncated]"

    @staticmethod
    def _display_args(args: dict) -> dict:
        return {k: (v if not isinstance(v, str) or len(v) <= 160 else v[:160] + "…") for k, v in args.items()}

    @staticmethod
    def _short(name, result, ok, blocked) -> str:
        if blocked:
            return "⛔ Safety guard: " + str(result.get("message") or result.get("error"))
        if not ok:
            return "Error: " + str(result.get("error") or result.get("message") or "failed")[:300]
        if name == "erp_request":
            return f"HTTP {result.get('status')} - {json.dumps(result.get('data'), default=str)[:220]}"
        if name in ("browser_open", "browser_snapshot", "browser_click"):
            page = result.get("page") or {}
            banner = f" Banner: {page['alerts'][0][:140]}" if page.get("alerts") else ""
            return f"Page {page.get('url')} - {len(page.get('elements', []))} controls.{banner}"
        if name == "read_file":
            return f"Read {result.get('path')} ({len(result.get('content', ''))} chars)"
        if name == "list_files":
            return f"{result.get('count')} file(s) listed"
        if name == "verify_payment":
            return str(result.get("detail"))[:260]
        return json.dumps(result, default=str, ensure_ascii=False)[:240]
