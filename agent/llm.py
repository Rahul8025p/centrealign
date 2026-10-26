"""
LLM client for a LOCAL model served by Ollama (https://ollama.com). No cloud, no API key.

The model is the agent's brain: it receives the conversation plus the tool catalogue and returns
text or tool calls. Nothing here is task-specific.

Configuration (environment variables, all optional):
  OLLAMA_HOST     where Ollama listens          (default http://localhost:11434)
  OLLAMA_MODEL    model to use                  (default qwen2.5:7b - must support tool calling)
  OLLAMA_NUM_CTX  context window in tokens      (default 16384; Ollama's own default of ~4k would
                                                 silently truncate the agent's prompt)
  OLLAMA_TIMEOUT  seconds per model call        (default 300; local models can be slow)
  OLLAMA_KEEP_ALIVE how long the model stays loaded (default 30m; Ollama's own default is 5m)

Local models are weaker than hosted ones at tool calling, so this client is deliberately forgiving:
it accepts arguments as a dict or a JSON string, strips <think> blocks, and recovers a tool call that
a small model wrote as JSON text instead of using the tool-call channel.
"""
import json
import os
import re
import time

import requests

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"


class LLMError(Exception):
    """The model could not be reached or returned something unusable (after retries)."""


class LLMClient:
    def __init__(self, host: str = None, model: str = None, num_ctx: int = None, timeout: int = None,
                 max_retries: int = 2, sleep=time.sleep):
        self.host = self._normalise_host(host or os.getenv("OLLAMA_HOST") or DEFAULT_HOST)
        self.model = model or os.getenv("OLLAMA_MODEL") or DEFAULT_MODEL
        self.num_ctx = int(num_ctx or os.getenv("OLLAMA_NUM_CTX") or 16384)
        self.timeout = int(timeout or os.getenv("OLLAMA_TIMEOUT") or 300)
        self.keep_alive = os.getenv("OLLAMA_KEEP_ALIVE") or "30m"
        self.max_retries, self._sleep = max_retries, sleep
        self.configured = True  # nothing to configure for a local model; use check() to see if it is usable

    @classmethod
    def from_env(cls):
        return cls()

    @staticmethod
    def _normalise_host(host: str) -> str:
        host = host.strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        return host.replace("//0.0.0.0", "//localhost")

    @property
    def context_chars(self) -> int:
        """Rough size of the conversation (in characters) that fits the context window."""
        return int(self.num_ctx * 2.5)

    # ------------------------------------------------------------------ readiness
    def check(self):
        """(ok, message): is Ollama running and is the model installed?"""
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=4)
            r.raise_for_status()
            names = [m.get("name") or m.get("model") or "" for m in r.json().get("models", [])]
        except (requests.RequestException, ValueError):
            return False, (f"Ollama is not reachable at {self.host}. Install it from https://ollama.com, "
                           f"start it (`ollama serve`), then run: ollama pull {self.model}")
        wanted = {self.model, self.model + ":latest"} if ":" not in self.model else {self.model}
        if not wanted & set(names):
            have = ", ".join(names) or "none"
            return False, f"The model '{self.model}' is not installed. Run: ollama pull {self.model}  (installed: {have})"
        return True, ""

    # ------------------------------------------------------------------ one model turn
    def chat(self, messages: list, tools: list) -> dict:
        """
        Returns {"content": str|None, "tool_calls": [{"id","name","arguments","raw_arguments"}]}.
        Connection problems, timeouts and 5xx are retried with backoff; anything else raises LLMError.
        """
        payload = {"model": self.model, "messages": self._to_ollama(messages), "tools": tools, "stream": False,
                   "keep_alive": self.keep_alive, "options": {"temperature": 0, "num_ctx": self.num_ctx}}
        last = None
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = LLMError(f"Could not reach Ollama at {self.host}: {e}")
            else:
                if r.status_code == 404:
                    raise LLMError(f"Ollama does not have the model '{self.model}'. Run: ollama pull {self.model}")
                if r.status_code == 400 and "tool" in r.text.lower():
                    raise LLMError(f"The model '{self.model}' does not support tool calling. "
                                   "Try qwen2.5:7b, llama3.1:8b or mistral-nemo.")
                if r.status_code >= 500:
                    last = LLMError(f"Ollama returned HTTP {r.status_code}: {r.text[:200]}")
                elif not r.ok:
                    raise LLMError(f"Ollama returned HTTP {r.status_code}: {r.text[:200]}")
                else:
                    try:
                        return self._normalise(r.json().get("message") or {}, tools)
                    except ValueError as e:
                        raise LLMError(f"Ollama sent an unreadable response: {e}")
            if attempt < self.max_retries:
                self._sleep(min(2 ** attempt, 8))
        raise last

    # ------------------------------------------------------------------ format translation
    @staticmethod
    def _to_ollama(messages: list) -> list:
        """The agent keeps OpenAI-style history; Ollama wants dict arguments and tool names on tool results."""
        names = {}
        out = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    fn = c["function"]
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    names[c["id"]] = fn["name"]
                    calls.append({"function": {"name": fn["name"], "arguments": args if isinstance(args, dict) else {}}})
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            elif m["role"] == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": names.get(m.get("tool_call_id"), "")})
            else:
                out.append({"role": m["role"], "content": m.get("content") or ""})
        return out

    @staticmethod
    def _normalise(message: dict, tools: list) -> dict:
        content = message.get("content") or ""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
        calls = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except ValueError:
                    args = {"_parse_error": args}
            if not isinstance(args, dict):
                args = {"_parse_error": str(args)}
            calls.append({"name": fn.get("name", ""), "arguments": args})
        if not calls and content:  # small models sometimes write the call as text
            calls, content = LLMClient._inline_calls(content, {t["function"]["name"] for t in tools})
        for i, c in enumerate(calls):
            c["id"] = f"call_{int(time.time() * 1000) % 10**8}_{i}"
            c["raw_arguments"] = json.dumps(c["arguments"], ensure_ascii=False)
        return {"content": content or None, "tool_calls": calls}

    @staticmethod
    def _inline_calls(content: str, known: set):
        """Recover {"name": ..., "arguments": {...}} written as plain text (optionally fenced)."""
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return [], content
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            return [], content
        found = []
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("name") in known:
                args = item.get("arguments", item.get("parameters", {}))
                if isinstance(args, dict):
                    found.append({"name": item["name"], "arguments": args})
        return (found, "") if found else ([], content)
