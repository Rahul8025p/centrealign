"""
Toolbox: the generic tools the model can choose from, plus their JSON schemas.

Nothing in here knows about a specific vendor, invoice or task. The tools are:
  thinking   update_plan, remember
  reading    list_files, read_file, search_files, erp_request (GET), browser_open, browser_snapshot, verify_payment
  acting     erp_request (POST ...), browser_fill, browser_select, browser_click
  talking    ask_user, finish

Every side effect passes through the Guard first, whichever tool (browser or API) the model picks.
"""
import json
import time
from urllib.parse import urlparse, urljoin

import requests

from ..guard import Decision


class HaltRun(Exception):
    """Stop the whole run safely (no answer from the user, or the user rejected a change)."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


TOOL_SPECS = [
    _fn("update_plan",
        "Write or revise your plan: a short ordered list of steps with a status each. Call it first, then again "
        "whenever a step finishes or reality forces a change.",
        {"steps": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done", "skipped"]}},
            "required": ["text", "status"]}}}, ["steps"]),
    _fn("remember", "Store a fact you discovered (e.g. invoice_id, amount, due_date) so it stays in front of you.",
        {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
    _fn("list_files", "List files in the document workspace (vendor invoice documents are under 'invoices').",
        {"subdir": {"type": "string", "description": "optional sub-folder, e.g. 'invoices'"}}),
    _fn("read_file", "Read a file from the workspace. Invoice documents also come back with parsed fields.",
        {"path": {"type": "string", "description": "path as returned by list_files"}}, ["path"]),
    _fn("search_files", "Case-insensitive text search across all workspace files.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("erp_request",
        "Call the ERP's HTTP API (method defaults to GET). GET /api returns a catalogue of endpoints. Writes "
        "(POST/PUT/PATCH/DELETE) are checked by the safety guard and may be blocked or need human approval.",
        {"method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
         "path": {"type": "string", "description": "must start with /api, e.g. /api/invoices"},
         "query": {"type": "object", "description": "optional query-string parameters"},
         "body": {"type": "object", "description": "optional JSON body"}}, ["path"]),
    _fn("browser_open", "Open an ERP page in the browser (e.g. '/billing') and return what is on it.",
        {"url": {"type": "string"}}, ["url"]),
    _fn("browser_snapshot",
        "Look at the current page: visible text, alert banners and every control with its selector, label and value."),
    _fn("browser_fill", "Type a value into an input using a selector from the latest snapshot.",
        {"selector": {"type": "string"}, "value": {"type": "string"}}, ["selector", "value"]),
    _fn("browser_select", "Choose an option in a dropdown (by value or visible label).",
        {"selector": {"type": "string"}, "value": {"type": "string"}}, ["selector", "value"]),
    _fn("browser_click",
        "Click an element. Clicking a form's submit button is a side effect and is checked by the safety guard. "
        "Returns the page afterwards so you can read banners and errors.",
        {"selector": {"type": "string"}}, ["selector"]),
    _fn("verify_payment",
        "Independently check the audit registry for an invoice's payment record (read-only). Use it to confirm a "
        "submission you made, or to answer 'has X been paid/submitted?'.",
        {"invoice_id": {"type": "string"}}, ["invoice_id"]),
    _fn("ask_user",
        "Ask the human a question when you cannot safely proceed (ambiguous or unknown target, missing information). "
        "Offer concrete options when you can.",
        {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, ["question"]),
    _fn("finish",
        "End the task. Give the outcome and a concise summary with key figures and evidence. Success claims are "
        "independently verified by the system before they are accepted.",
        {"status": {"type": "string", "enum": ["completed", "already_submitted", "already_paid", "discrepancy",
                                                "declined", "needs_input", "failed"]},
         "summary": {"type": "string"},
         "confirmation_id": {"type": "string", "description": "only if you completed a payment this run"}},
        ["status", "summary"]),
]


class Toolbox:
    def __init__(self, run, base_url, file_tool, api_tool, verifier, guard,
                 browser_provider, ask, emit):
        self.run, self.base_url = run, base_url.rstrip("/")
        self.files, self.api, self.verifier, self.guard = file_tool, api_tool, verifier, guard
        self.browser_provider, self.ask, self.emit = browser_provider, ask, emit

    # ------------------------------------------------------------------ dispatch
    def execute(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"t_{name}", None)
        if handler is None:
            return {"ok": False, "error": f"Unknown tool '{name}'. Available: {[s['function']['name'] for s in TOOL_SPECS]}"}
        if "_parse_error" in args:
            return {"ok": False, "error": "Your tool arguments were not valid JSON. Call the tool again with a valid JSON object."}
        args = self._coerce(name, args)
        try:
            return handler(**args)
        except HaltRun:
            raise
        except TypeError as e:
            return {"ok": False, "error": f"Bad arguments for {name}: {e}"}
        except Exception as e:  # a tool must never crash the run
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def _coerce(name, args):
        """Small models often send arrays/objects as JSON text; accept that."""
        spec = next((t["function"]["parameters"]["properties"] for t in TOOL_SPECS if t["function"]["name"] == name), {})
        fixed = dict(args)
        for k, v in args.items():
            if isinstance(v, str) and spec.get(k, {}).get("type") in ("array", "object") and v.strip()[:1] in "[{":
                try:
                    fixed[k] = json.loads(v)
                except ValueError:
                    pass
        return fixed

    # ------------------------------------------------------------------ thinking
    def t_update_plan(self, steps):
        plan = self.run.set_plan(steps)
        self.emit("plan", {"plan": plan})
        return {"ok": True, "steps": len(plan)}

    def t_remember(self, key, value):
        self.run.set_entity(str(key), value)
        return {"ok": True}

    # ------------------------------------------------------------------ files
    def t_list_files(self, subdir=""):
        return self.files.list_files(subdir)

    def t_read_file(self, path):
        res = self.files.read_file(path)
        if res.get("ok") and res.get("parsed_invoice"):
            self.guard.note_document(self.run, res["parsed_invoice"])
        return res

    def t_search_files(self, query):
        return self.files.search(query)

    # ------------------------------------------------------------------ ERP API
    def _http(self, method, path, query, body):
        last = None
        for attempt in range(3):  # retry only network-level failures
            try:
                return requests.request(method, self.base_url + path, params=query or None,
                                        json=body if method != "GET" else None, timeout=8)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                time.sleep(0.4 * (attempt + 1))
        raise last

    def _approval(self, decision: Decision):
        resp = self.ask("approval", decision.message, [], decision.details)
        if resp is None:
            raise HaltRun("needs_input", "There was no answer to the approval request, so nothing was changed.")
        if resp.get("approved") is True:
            self.run.approvals[decision.key] = True
            return
        self.run.approvals[decision.key] = False
        raise HaltRun("declined", "You rejected this change, so nothing was submitted.")

    def t_erp_request(self, path, method=None, query=None, body=None):
        if method is None:
            if body:  # never guess that a request with a body is a write: make the model say so
                return {"ok": False, "error": "Specify the method (e.g. POST) for a request that has a body."}
            method = "GET"
        method = str(method).upper()
        path = str(path)
        if not path.startswith("/api"):
            return {"ok": False, "error": "Only the ERP's /api/... endpoints can be called here. Use browser_open for UI pages."}
        if method != "GET":
            decision = self.guard.review_http_write(self.run, method, path, body or {})
            if decision.action == Decision.BLOCK:
                return decision.to_tool_result()
            if decision.action == Decision.APPROVE:
                self._approval(decision)
        try:
            resp = self._http(method, path, query, body)
        except Exception as e:
            return {"ok": False, "error": f"The ERP could not be reached: {e}"}
        if method != "GET":
            self.guard.record_http_write(self.run, method, path, body or {}, resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            data = resp.text[:1500]
        return {"ok": resp.ok, "status": resp.status_code, "data": data}

    def t_verify_payment(self, invoice_id):
        mine = [w for w in self.run.writes if w["kind"] == "payment" and w["invoice_id"] == str(invoice_id).upper()]
        r = self.verifier.verify_outcome(str(invoice_id), expected_confirmation_id=mine[-1]["confirmation_id"] if mine else None)
        out = {"ok": True, "verified": bool(r.get("verified")), "detail": r.get("summary") or r.get("reason")}
        for k in ("confirmation_id", "amount", "status", "timestamp", "audit_log_record", "vendor_invoice_record"):
            if r.get(k) is not None:
                out[k] = r[k]
        return out

    # ------------------------------------------------------------------ talking to the human
    def t_ask_user(self, question, options=None):
        resp = self.ask("clarification", str(question), list(options or []), {})
        answer = str((resp or {}).get("answer", "")).strip()
        if not answer:
            raise HaltRun("needs_input", "There was no answer to my question, so I stopped without changing anything.")
        self.run.user_texts.append(answer)   # the user's words ground later actions (see Guard._grounded)
        return {"ok": True, "answer": answer}

    # ------------------------------------------------------------------ browser
    def _browser(self):
        return self.browser_provider()

    def _absolute(self, url):
        full = urljoin(self.base_url + "/", str(url))
        if urlparse(full).netloc != urlparse(self.base_url).netloc:
            return None
        return full

    @staticmethod
    def _brief(snap: dict) -> dict:
        els = []
        for e in (snap.get("elements") or [])[:60]:
            els.append({k: v for k, v in e.items() if v not in (None, "", False)})
        return {"url": snap.get("url"), "title": snap.get("title"), "alerts": snap.get("alerts") or [],
                "text": (snap.get("text") or "")[:1200], "elements": els}

    def _page_after(self, b, ok: bool, **extra):
        snap = b.snapshot()
        out = {"ok": ok, **extra}
        if snap.get("success"):
            out["page"] = self._brief(snap)
            out["screenshot"] = snap.get("screenshot")
        else:
            out["page_error"] = snap.get("error")
        return out

    def t_browser_open(self, url):
        full = self._absolute(url)
        if not full:
            return {"ok": False, "error": "The browser may only open pages of the company ERP."}
        b = self._browser()
        res = b.navigate(full)
        if not res.get("success"):  # one retry for a flaky page load
            time.sleep(0.8)
            res = b.navigate(full)
        if not res.get("success"):
            return {"ok": False, "error": res.get("error", "Navigation failed")}
        return self._page_after(b, True)

    def t_browser_snapshot(self):
        snap = self._browser().snapshot()
        if not snap.get("success"):
            return {"ok": False, "error": snap.get("error", "Could not read the page. Open a page first with browser_open.")}
        return {"ok": True, "page": self._brief(snap), "screenshot": snap.get("screenshot")}

    def t_browser_fill(self, selector, value):
        b = self._browser()
        res = b.fill(selector, value)
        if res.get("success"):
            return {"ok": True, "filled": selector}
        return self._page_after(b, False, error=res.get("error"),
                                hint="The selector may be wrong. Pick one from this fresh snapshot.")

    def t_browser_select(self, selector, value):
        b = self._browser()
        res = b.select_option(selector, value)
        if res.get("success"):
            return {"ok": True, "selected": value}
        return self._page_after(b, False, error=res.get("error"),
                                hint="Check the option values/labels in this fresh snapshot.")

    def t_browser_click(self, selector):
        b = self._browser()
        submit = b.is_submit(selector)
        path, values = urlparse(b.current_url()).path, {}
        if submit:
            values = b.form_values(selector)
            decision = self.guard.review_browser_submit(self.run, path, values)
            if decision.action == Decision.BLOCK:
                return decision.to_tool_result()
            if decision.action == Decision.APPROVE:
                self._approval(decision)
        res = b.click(selector)
        if not res.get("success"):
            return self._page_after(b, False, error=res.get("error"),
                                    hint="Nothing was clicked. Re-read the page and try again, or use another route.")
        if submit:
            self.guard.record_browser_submit(self.run, path, values)
        return self._page_after(b, True, clicked=selector)
