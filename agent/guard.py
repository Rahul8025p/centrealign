"""
Guard: the deterministic safety layer around the model's tools.

The model decides WHAT to do; the guard decides whether a side effect is allowed to happen.
It sits on every write path (browser form submit and HTTP write alike), so switching channel
can never bypass a rule. It does four jobs:

  1. review   - before a write: allow it, block it (with a reason the model can act on),
                or pause for human approval
  2. record   - after a write: note what REALLY changed by diffing the audit registry,
                instead of trusting what the model or the page said
  3. verify   - independently re-check every recorded write
  4. finish   - refuse a "completed" claim that is unverified or cites fabricated confirmation IDs

Rules that exist today (payments):
  unknown invoice | already submitted | already submitted earlier in THIS run | already paid |
  amount / vendor / due-date mismatch (form vs ERP vs the invoice document the agent read) |
  amount >= approval threshold | payee not named by the user -> needs approval.
Any write the guard has no policy for is never silently allowed: it needs human approval.
"""
import re
from .vendors import exact_match, find_exact_vendors

CONF_RE = re.compile(r"APX-CONF-\d+-\w+")
NOTE_PATH_RE = re.compile(r"^/api/invoices/([^/]+)/notes$", re.IGNORECASE)


def inr(value) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def to_float(value):
    try:
        return float(str(value).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


class Decision:
    ALLOW, BLOCK, APPROVE = "allow", "block", "approve"

    def __init__(self, action, code="", message="", finish_with=None, details=None, key=None):
        self.action, self.code, self.message = action, code, message
        self.finish_with, self.details, self.key = finish_with, details or {}, key

    @classmethod
    def allow(cls):
        return cls(cls.ALLOW)

    @classmethod
    def block(cls, code, message, finish_with=None, **details):
        return cls(cls.BLOCK, code, message, finish_with, details)

    @classmethod
    def approve(cls, message, key, **details):
        return cls(cls.APPROVE, "needs_approval", message, None, details, key)

    def to_tool_result(self):
        r = {"ok": False, "blocked_by_guard": True, "code": self.code, "message": self.message}
        if self.finish_with:
            r["finish_with"] = self.finish_with
        if self.details:
            r["details"] = self.details
        return r


class Guard:
    def __init__(self, api, verifier, approval_threshold: float = 100000):
        self.api, self.verifier, self.approval_threshold = api, verifier, approval_threshold

    # ------------------------------------------------------------------ setup
    def start(self, run):
        """Remember which confirmations already exist, so anything new is provably this run's."""
        self._ensure_baseline(run)

    def _registry(self):
        res = self.api.get_submissions()
        if isinstance(res, dict) and res.get("success") is not False:
            return res.get("submissions") or []
        return None

    def _ensure_baseline(self, run) -> bool:
        if run.baseline_ok:
            return True
        subs = self._registry()
        if subs is None:
            return False
        run.known_submission_ids = {s.get("confirmation_id") for s in subs if s.get("confirmation_id")}
        run.baseline_ok = True
        return True

    def note_document(self, run, parsed: dict):
        """Called when the agent reads an invoice document; kept for the cross-check."""
        if parsed and parsed.get("invoice_id"):
            run.docs_seen[parsed["invoice_id"].upper()] = parsed

    # ------------------------------------------------------------ classification
    @staticmethod
    def classify(channel: str, method: str, path: str, values: dict) -> str:
        path = (path or "").split("?")[0].rstrip("/") or "/"
        if channel == "http":
            if method == "POST" and path == "/api/billing/submit":
                return "payment"
            if method == "POST" and NOTE_PATH_RE.match(path):
                return "note"
        elif path == "/billing" and {"invoice_id", "amount"} <= set(values or {}):
            return "payment"
        return "unknown"

    # ----------------------------------------------------------------- review
    def review_http_write(self, run, method: str, path: str, body: dict) -> Decision:
        body = body or {}
        kind = self.classify("http", method, path, body)
        if kind == "payment":
            return self._review_payment(run, body)
        if kind == "note":
            return self._review_note(run, NOTE_PATH_RE.match(path.split("?")[0].rstrip("/")).group(1), body)
        return self._review_unknown(run, f"{method} {path}")

    def review_browser_submit(self, run, page_path: str, values: dict) -> Decision:
        if self.classify("browser", "POST", page_path, values) == "payment":
            return self._review_payment(run, values)
        return self._review_unknown(run, f"form on {page_path or 'the current page'}")

    def _review_unknown(self, run, what: str) -> Decision:
        key = f"unknown:{what}"
        if run.approvals.get(key):
            return Decision.allow()
        if run.approvals.get(key) is False:
            return Decision.block("declined", "The user rejected this change.", finish_with="declined")
        return Decision.approve(
            f"The agent wants to make a change ({what}) that no safety policy covers. Approve it?",
            key, reasons=["No safety policy exists for this kind of change"], what=what)

    def _review_note(self, run, invoice_id: str, body: dict) -> Decision:
        if not str(body.get("note", "")).strip():
            return Decision.block("incomplete", "A note needs non-empty text in the 'note' field.")
        res = self.api.get_invoice_details(invoice_id)
        if not res.get("success"):
            return Decision.block("unknown_invoice", f"Invoice {invoice_id} does not exist in the ERP.")
        return Decision.allow()

    def _grounded(self, run, invoice_id: str, company: str) -> bool:
        """Did the USER name this invoice or vendor (in the task or in an answer)? Prevents silent substitution."""
        text = "\n".join(run.user_texts)
        if invoice_id.upper() in {m.upper() for m in re.findall(r"INV-\d+", text, re.IGNORECASE)}:
            return True
        return bool(company) and bool(find_exact_vendors(text, [company]))

    def _review_payment(self, run, v: dict) -> Decision:
        inv_id = str(v.get("invoice_id", "")).strip().upper()
        amount = to_float(v.get("amount"))
        company = str(v.get("company", "") or "").strip()
        due = str(v.get("due_date", "") or "").strip()
        if not inv_id or amount is None:
            return Decision.block("incomplete", "A payment needs an invoice id and a numeric amount.")
        if not self._ensure_baseline(run):
            return Decision.block("erp_unreachable", "The ERP could not be reached to run safety checks; try again shortly.")

        res = self.api.get_invoice_details(inv_id)
        if not res.get("success"):
            if "API request failed" in str(res.get("error", "")):
                return Decision.block("erp_unreachable", "The ERP could not be reached to run safety checks; try again shortly.")
            known = [i.get("id") for i in (self.api.get_invoices().get("invoices") or [])]
            return Decision.block("unknown_invoice", f"Invoice {inv_id} does not exist in the ERP. Known invoices: {known}.")
        inv = res["invoice"]

        # a payment this run already caused (e.g. the click worked but the page banner was missed)
        self._sync(run, "unknown")
        mine = [w for w in run.writes if w["kind"] == "payment" and w["invoice_id"] == inv_id]
        if mine:
            return Decision.block(
                "already_done_by_this_run",
                f"{inv_id} was already submitted earlier in this run (confirmation {mine[-1]['confirmation_id']}). "
                "Do NOT submit again - verify with verify_payment and finish.",
                confirmation_id=mine[-1]["confirmation_id"])

        subs = self._registry() or []
        prior = [s for s in subs if str(s.get("invoice_id", "")).upper() == inv_id]
        if prior:
            return Decision.block(
                "already_submitted",
                f"{inv_id} was already submitted on {prior[-1].get('timestamp')} "
                f"(confirmation {prior[-1].get('confirmation_id')}). A second payment would be a duplicate.",
                finish_with="already_submitted", existing_confirmation_id=prior[-1].get("confirmation_id"))
        if str(inv.get("status", "")).lower() == "paid":
            return Decision.block("already_paid", f"{inv_id} is already marked as paid in the ERP.", finish_with="already_paid")

        payable = to_float(inv.get("total", inv.get("amount")))
        problems = []
        if payable is not None and abs(amount - payable) > 1.0:
            problems.append(f"amount {inr(amount)} differs from the ERP payable total {inr(payable)}")
        if company and not exact_match(company, inv.get("company", "")):
            problems.append(f"vendor '{company}' differs from the ERP vendor '{inv.get('company')}'")
        if due and inv.get("due_date") and due != inv["due_date"]:
            problems.append(f"due date {due} differs from the ERP due date {inv['due_date']}")
        doc = run.docs_seen.get(inv_id)
        if doc:
            if doc.get("amount") is not None and payable is not None and abs(doc["amount"] - payable) > 1.0:
                problems.append(f"the invoice document says {inr(doc['amount'])} but the ERP says {inr(payable)}")
            if doc.get("due_date") and due and doc["due_date"] != due:
                problems.append(f"due date {due} differs from the invoice document ({doc['due_date']})")
        if problems:
            return Decision.block("discrepancy", f"Not submitted - {'; '.join(problems)}. Report this or ask the user.",
                                  finish_with="discrepancy", problems=problems)

        reasons = []
        if amount >= self.approval_threshold:
            reasons.append(f"amount {inr(amount)} is at or above the {inr(self.approval_threshold)} approval limit")
        if not self._grounded(run, inv_id, inv.get("company", "")):
            reasons.append(f"you did not name {inv_id} or {inv.get('company')} in your request")
        if reasons:
            if run.approvals.get(inv_id) is False:
                return Decision.block("declined", "The user rejected this payment.", finish_with="declined")
            if not run.approvals.get(inv_id):
                run.reviewed[inv_id] = {"amount": amount, "company": company, "due_date": due}
                return Decision.approve(
                    f"Approve paying {inr(amount)} to {inv.get('company')} for {inv_id}? Reason: {'; '.join(reasons)}.",
                    inv_id, reasons=reasons, invoice_id=inv_id, amount=amount, vendor=inv.get("company"))
        run.reviewed[inv_id] = {"amount": amount, "company": company, "due_date": due}
        return Decision.allow()

    # ----------------------------------------------------------------- record
    def _sync(self, run, via: str):
        """Diff the audit registry: anything new since the run started was caused by this run."""
        if not self._ensure_baseline(run):
            return
        for s in self._registry() or []:
            cid = s.get("confirmation_id")
            if cid and cid not in run.known_submission_ids:
                inv_id = str(s.get("invoice_id", "")).upper()
                intent = run.reviewed.get(inv_id, {})
                run.known_submission_ids.add(cid)
                run.writes.append({"kind": "payment", "invoice_id": inv_id, "confirmation_id": cid,
                                   "amount": s.get("amount"), "company": s.get("company"),
                                   "due_date": s.get("due_date"), "via": via,
                                   "intended_amount": intent.get("amount")})
                run.record_evidence("confirmation_id", cid)
                run.record_evidence("submitted_via", via)
                for k, val in (("invoice_id", inv_id), ("company", s.get("company")),
                               ("amount", s.get("amount")), ("due_date", s.get("due_date"))):
                    run.set_entity(k, val)

    def record_http_write(self, run, method: str, path: str, body: dict, status_code: int):
        kind = self.classify("http", method, path, body or {})
        if kind == "payment":
            self._sync(run, "api")
        elif 200 <= status_code < 300:
            if kind == "note":
                inv = NOTE_PATH_RE.match(path.split("?")[0].rstrip("/")).group(1).upper()
                run.writes.append({"kind": "note", "invoice_id": inv, "note": str((body or {}).get("note", "")), "via": "api"})
            else:
                run.writes.append({"kind": "unknown", "what": f"{method} {path}", "via": "api"})

    def record_browser_submit(self, run, page_path: str, values: dict):
        if self.classify("browser", "POST", page_path, values) == "payment":
            self._sync(run, "browser")
        else:
            run.writes.append({"kind": "unknown", "what": f"form on {page_path}", "via": "browser"})

    # ----------------------------------------------------------------- verify
    def verify_run(self, run) -> dict:
        checks, unverified, ok = [], [], True
        for w in run.writes:
            if w["kind"] == "payment":
                r = self.verifier.verify_outcome(
                    w["invoice_id"], expected_amount=w.get("intended_amount", w["amount"]),
                    expected_vendor=w.get("company"), expected_confirmation_id=w["confirmation_id"],
                    expected_due_date=w.get("due_date"))
                good = bool(r.get("verified"))
                checks.append({"write": f"payment {w['invoice_id']}", "ok": good, "detail": r.get("summary") or r.get("reason")})
                if good:
                    keep = {k: r[k] for k in ("verified", "confirmation_id", "amount", "bound_to_this_run",
                                              "two_way_reconciled", "evidence", "timestamp") if k in r}
                    run.record_evidence("verification_result", keep)
                ok = ok and good
            elif w["kind"] == "note":
                res = self.api.get_invoice_details(w["invoice_id"])
                notes = (res.get("invoice") or {}).get("notes") or []
                texts = [n.get("text", "") if isinstance(n, dict) else str(n) for n in notes]
                good = any(w["note"] in t for t in texts)
                checks.append({"write": f"note on {w['invoice_id']}", "ok": good,
                               "detail": "note found on the invoice" if good else "note not found when read back"})
                ok = ok and good
            else:
                unverified.append(w)
        if unverified:
            run.record_evidence("unverified_writes", unverified)
        return {"ok": ok, "checks": checks, "unverified": unverified}

    # ----------------------------------------------------------------- finish
    def review_finish(self, run, status: str, summary: str, confirmation_id: str = None):
        """Returns (ok, message). Only a 'completed' claim is checked."""
        if status != "completed":
            return True, ""
        subs = self._registry()
        if subs is not None:
            real = {s.get("confirmation_id") for s in subs}
            cited = set(CONF_RE.findall(summary or "")) | ({confirmation_id} if confirmation_id else set())
            fake = sorted(c for c in cited if c not in real)
            if fake:
                return False, (f"Confirmation ID(s) {fake} do not exist in the audit registry. Never invent evidence - "
                               "correct the summary using real tool output.")
            self._sync(run, "unknown")
        v = self.verify_run(run)
        if not v["ok"]:
            bad = [c["detail"] for c in v["checks"] if not c["ok"]]
            return False, f"Independent verification failed: {'; '.join(map(str, bad))}. You cannot report success; investigate or finish with status 'failed'."
        return True, ""
