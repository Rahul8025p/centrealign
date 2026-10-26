"""
Tests for the tool-calling agent.

What is real:   the agent loop, the toolbox, the guard, the verifier, the Flask ERP (on temporary data),
                and - in RealBrowserTests - a real headless Chromium driven through Playwright.
What is faked:  the LLM. `Script` stands in for the model: each turn is a function that reads the
                tool results so far and chooses the next call, the way a model would. These tests
                therefore prove the loop / tools / guard / verification, NOT the quality of any real model.

Run:  python -m unittest tests.test_agent -v
"""
import itertools
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
logging.getLogger("werkzeug").setLevel(logging.ERROR)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests
from werkzeug.serving import make_server

import app.server as srv
from agent.llm import LLMClient, LLMError
from agent.orchestrator import TaskOrchestrator
from agent.prompts import SYSTEM_PROMPT
from agent.tools.file_tool import FileTool
from agent.tools.toolbox import TOOL_SPECS

# --------------------------------------------------------------------------- stand-in model
_ids = itertools.count(1)


def tc(name, **args):
    return {"id": f"call_{next(_ids)}", "name": name, "arguments": args, "raw_arguments": json.dumps(args)}


class Script:
    """A stand-in LLM. Each turn is a value or a function(messages) -> value; a value is a tool call,
    a list of tool calls, a plain string (text reply) or (text, [tool calls])."""
    configured, model = True, "scripted"

    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def chat(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        if not self.turns:
            raise AssertionError("The script ran out of turns: the agent asked the model for more than expected.")
        turn = self.turns.pop(0)
        out = turn(messages) if callable(turn) else turn
        content = None
        if isinstance(out, tuple):
            content, out = out
        if isinstance(out, str):
            return {"content": out, "tool_calls": []}
        if isinstance(out, dict):
            out = [out]
        return {"content": content, "tool_calls": out}


def results(messages):
    out = []
    for m in messages:
        if m["role"] == "tool":
            try:
                out.append(json.loads(m["content"]))
            except ValueError:
                out.append({"raw": m["content"]})
    return out


def last(messages):
    return results(messages)[-1]


def selector(messages, label_part, key="label"):
    """Find a control's selector from the most recent page snapshot, by its visible label (discovery, not hard-coding)."""
    for r in reversed(results(messages)):
        for e in (r.get("page") or {}).get("elements", []):
            if e.get("tag") in ("input", "select", "textarea") and label_part.lower() in str(e.get(key, "")).lower():
                return e["selector"]            # form fields only (a model would not type into a nav link)
    raise AssertionError(f"no form field labelled '{label_part}' in any snapshot")


def submit_selector(messages):
    for r in reversed(results(messages)):
        for e in (r.get("page") or {}).get("elements", []):
            if e.get("submits_form"):
                return e["selector"]
    raise AssertionError("no submit button in any snapshot")


# ----------------------------------------------------------------------------- fake browser
class FakeBrowser:
    """Mimics the /billing page (including the case where the success banner never shows)."""
    def __init__(self, base_url, hide_banner=False, fail_click=False):
        self.base_url, self.hide_banner, self.fail_click = base_url, hide_banner, fail_click
        self.url, self.fields, self.banner = "", {}, None

    def navigate(self, url):
        self.url, self.fields, self.banner = url, {}, None
        return {"success": True, "screenshot": None}

    def current_url(self):
        return self.url

    def snapshot(self, max_text=1500):
        if "/billing" not in self.url:
            return {"success": True, "url": self.url, "title": "Page", "text": "", "alerts": [], "elements": [], "screenshot": None}
        spec = [("#input-invoice-id", "input", "Invoice Number / ID *"), ("#input-company", "input", "Vendor / Company Legal Name *"),
                ("#input-amount", "input", "Payment Amount (INR ₹) *"), ("#input-due-date", "input", "Due Date (YYYY-MM-DD) *"),
                ("#select-category", "select", "Expense Category")]
        els = [{"selector": s, "tag": t, "label": l, "value": self.fields.get(s, "")} for s, t, l in spec]
        els.append({"selector": "#btn-submit-billing", "tag": "button", "label": "Submit for Disbursement →", "submits_form": True})
        alerts = [] if (self.banner is None or self.hide_banner) else [self.banner]
        return {"success": True, "url": self.url, "title": "Billing", "text": "", "alerts": alerts, "elements": els, "screenshot": None}

    def fill(self, sel, value):
        self.fields[sel] = str(value)
        return {"success": True}

    def select_option(self, sel, value):
        self.fields[sel] = value
        return {"success": True}

    def is_submit(self, sel):
        return sel == "#btn-submit-billing"

    def form_values(self, sel):
        m = {"invoice_id": "#input-invoice-id", "company": "#input-company", "amount": "#input-amount",
             "due_date": "#input-due-date", "category": "#select-category"}
        return {k: self.fields.get(v, "") for k, v in m.items()}

    def click(self, sel, fallback_text=None):
        if self.fail_click:
            return {"success": False, "error": f"Element {sel} not found or not visible."}
        f = self.form_values(sel)
        r = requests.post(f"{self.base_url}/api/billing/submit", json=f)
        d = r.json()
        self.banner = (f"Payment Submission Confirmed! Confirmation ID: {d['confirmation_id']}" if r.ok and d.get("success")
                       else f"Submission Failed {d.get('error')}")
        return {"success": True, "screenshot": None}

    def capture_screenshot(self, tag="shot"):
        return None

    def close(self):
        pass


# --------------------------------------------------------------------------------- base
class ERPCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        srv.INVOICES_FILE = os.path.join(cls.tmp, "invoices.json")
        srv.SUBMISSIONS_FILE = os.path.join(cls.tmp, "submissions.json")
        cls.httpd = make_server("127.0.0.1", 0, srv.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        requests.post(f"{self.base}/api/reset")

    def submissions(self):
        return requests.get(f"{self.base}/api/submissions").json()["submissions"]

    def invoice(self, inv_id):
        return requests.get(f"{self.base}/api/invoices/{inv_id}").json()["invoice"]

    def run_agent(self, prompt, script, answers=None, browser=None, ask=True, threshold=None, max_steps=30):
        kwargs = {"approval_threshold": threshold} if threshold is not None else {}
        orch = TaskOrchestrator(base_url=self.base, llm=script, max_steps=max_steps, **kwargs)
        orch.browser_tool = browser or FakeBrowser(self.base)
        orch._get_browser = lambda: orch.browser_tool
        events, questions, queue_ = [], [], list(answers or [])

        def ask_user(q):
            questions.append(q)
            return queue_.pop(0) if queue_ else None

        result = orch.run_task(prompt, on_event=events.append, ask_user=ask_user if ask else None)
        return result, events, questions

    # ----- reusable "model" behaviours --------------------------------------------------------------
    def pay_turns(self, vendor_query, amount=None, via="browser", finish=True):
        """Turns for a model that finds an invoice, reads its document, pays it and verifies."""
        ctx = {}

        def pick(m):
            ctx["inv"] = last(m)["data"]["invoices"][0]
            return tc("search_files", query=ctx["inv"]["id"])

        def read(m):
            return tc("read_file", path=last(m)["matching_files"][0])

        def fill_all(m):
            inv = ctx["inv"]
            amt = amount if amount is not None else inv["total"]
            return [tc("browser_fill", selector=selector(m, "Invoice Number"), value=inv["id"]),
                    tc("browser_fill", selector=selector(m, "Vendor"), value=inv["company"]),
                    tc("browser_fill", selector=selector(m, "Payment Amount"), value=str(amt)),
                    tc("browser_fill", selector=selector(m, "Due Date"), value=inv["due_date"])]

        def post_api(m):
            inv = ctx["inv"]
            return tc("erp_request", method="POST", path="/api/billing/submit",
                      body={"invoice_id": inv["id"], "company": inv["company"],
                            "amount": amount if amount is not None else inv["total"], "due_date": inv["due_date"]})

        turns = [tc("update_plan", steps=[{"text": "Find the invoice", "status": "in_progress"},
                                          {"text": "Pay it", "status": "pending"}]),
                 tc("erp_request", method="GET", path="/api/invoices", query={"query": vendor_query}),
                 pick, read]
        if via == "browser":
            turns += [tc("browser_open", url="/billing"), fill_all,
                      lambda m: tc("browser_click", selector=submit_selector(m))]
        else:
            turns += [post_api]
        turns += [lambda m: tc("verify_payment", invoice_id=ctx["inv"]["id"])]
        if finish:
            turns += [lambda m: tc("finish", status="completed", confirmation_id=last(m).get("confirmation_id"),
                                   summary=f"Paid {ctx['inv']['id']} ({last(m).get('confirmation_id')}).")]
        return turns, ctx


# =================================================================================== tests
class GeneralisationTests(ERPCase):
    def test_nothing_task_specific_is_baked_into_the_agent(self):
        text = (SYSTEM_PROMPT + json.dumps(TOOL_SPECS)).lower()
        for needle in ("techcorp", "megaretail", "globalinc", "startupxyz", "acme", "inv-10", "99,710"):
            self.assertNotIn(needle, text)

    def test_unrelated_question_is_answered_without_touching_any_system(self):
        script = Script("Paris is the capital of France.")
        result, events, _ = self.run_agent("What is the capital of France?", script)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["history"], [])            # no tool was used
        self.assertEqual(self.submissions(), [])
        self.assertNotIn("techcorp", result["summary"].lower())

    def test_unrelated_question_via_finish_tool(self):
        script = Script(tc("finish", status="completed", summary="That is outside what I do here: I operate the company's invoice systems."))
        result, _, _ = self.run_agent("what's the weather like?", script)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.submissions(), [])

    def test_read_only_analysis_uses_data_not_a_script(self):
        def count(m):
            invs = last(m)["data"]["invoices"]
            return tc("finish", status="completed", summary=f"{len(invs)} invoices are pending.")
        script = Script(tc("update_plan", steps=["Fetch pending invoices", "Count them"]),
                        tc("erp_request", method="GET", path="/api/invoices", query={"status": "pending"}), count)
        result, _, _ = self.run_agent("How many invoices are pending?", script)
        self.assertEqual(result["status"], "completed")
        self.assertIn("3 invoices", result["summary"])      # INV-1042, 1043, 1045
        self.assertEqual(result["writes"], [])
        self.assertEqual(self.submissions(), [])

    def test_a_different_write_task_runs_through_the_same_loop(self):
        note = "Vendor asked for a 5 day extension."
        script = Script(
            tc("update_plan", steps=["Add the note", "Confirm it is stored"]),
            tc("erp_request", method="POST", path="/api/invoices/INV-1043/notes", body={"note": note}),
            tc("erp_request", method="GET", path="/api/invoices/INV-1043"),
            tc("finish", status="completed", summary="Added the note to INV-1043 and read it back."))
        result, events, questions = self.run_agent("Add a note to INV-1043: " + note, script)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(questions, [])                     # a low-risk write needs no approval
        self.assertEqual(result["writes"][0]["kind"], "note")
        self.assertEqual(self.invoice("INV-1043")["notes"][0]["text"], note)
        self.assertEqual(self.submissions(), [])            # and no payment was made

    def test_note_that_is_not_really_stored_fails_independent_verification(self):
        script = Script(
            tc("erp_request", method="POST", path="/api/invoices/INV-1043/notes", body={"note": "A"}),
            lambda m: (requests.post(f"{self.base}/api/reset"), tc("finish", status="completed", summary="Done."))[1],
            tc("finish", status="completed", summary="Done."))
        result, _, _ = self.run_agent("Add note A to INV-1043", script)
        self.assertEqual(result["status"], "failed")
        self.assertIn("verification", result["summary"].lower())


class TransparencyTests(ERPCase):
    def test_every_tool_call_says_which_model_chose_it_and_how_long_it_took(self):
        script = Script(tc("list_files"), tc("finish", status="completed", summary="Listed."))
        _, events, _ = self.run_agent("list the files", script)
        calls = [e for e in events if e["type"] == "tool_call"]
        self.assertTrue(calls)
        for e in calls:
            self.assertEqual(e["model"], "scripted")
            self.assertIn("seconds", e)
        self.assertEqual([e["type"] for e in events].count("thinking"), 2)   # one model call per decision


class ServedPageTests(ERPCase):
    def test_the_agent_page_is_the_new_ui_and_is_never_cached(self):
        r = requests.get(self.base + "/agent")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["X-Agent-Build"], "tool-calling")
        self.assertIn("NEW tool-calling agent", r.text)
        for old_text in ("Decomposed Action Plan", "Preset Enterprise Scenarios", "ReAct Loop", "TechCorp Retainer"):
            self.assertNotIn(old_text, r.text)


class PaymentTests(ERPCase):
    def test_pay_via_browser_with_discovered_selectors(self):
        turns, _ = self.pay_turns("GlobalInc")
        result, events, questions = self.run_agent("Pay the GlobalInc invoice.", Script(*turns))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(questions, [])
        subs = self.submissions()
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["invoice_id"], "INV-1043")
        self.assertEqual(subs[0]["amount"], 37760)
        self.assertEqual(result["evidence"]["confirmation_id"], subs[0]["confirmation_id"])
        self.assertEqual(result["writes"][0]["via"], "browser")
        self.assertTrue(result["evidence"]["verification_result"]["verified"])
        self.assertTrue(result["evidence"]["verification_result"]["bound_to_this_run"])
        self.assertTrue(any(e["type"] == "plan" for e in events))

    def test_pay_via_api_goes_through_the_same_guard(self):
        turns, _ = self.pay_turns("GlobalInc", via="api")
        result, _, _ = self.run_agent("Pay the GlobalInc invoice.", Script(*turns))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["writes"][0]["via"], "api")
        self.assertEqual(len(self.submissions()), 1)

    def test_duplicate_payment_is_blocked(self):
        turns, _ = self.pay_turns("GlobalInc")
        self.run_agent("Pay the GlobalInc invoice.", Script(*turns))

        ctx = {}
        def pick(m):
            ctx["inv"] = last(m)["data"]["invoices"][0]
            return tc("browser_open", url="/billing")
        def fill(m):
            i = ctx["inv"]
            return [tc("browser_fill", selector=selector(m, "Invoice Number"), value=i["id"]),
                    tc("browser_fill", selector=selector(m, "Payment Amount"), value=str(i["total"]))]
        def after_block(m):
            r = last(m)
            self.assertTrue(r["blocked_by_guard"])
            self.assertEqual(r["finish_with"], "already_submitted")
            return tc("finish", status=r["finish_with"], summary=r["message"])
        script = Script(tc("erp_request", method="GET", path="/api/invoices", query={"query": "GlobalInc"}), pick, fill,
                        lambda m: tc("browser_click", selector=submit_selector(m)), after_block)
        result, _, _ = self.run_agent("Pay the GlobalInc invoice.", script)
        self.assertEqual(result["status"], "already_submitted")
        self.assertEqual(len(self.submissions()), 1)

    def test_already_paid_invoice_is_not_paid_again(self):
        def pay(m):
            i = last(m)["data"]["invoices"][0]
            return tc("erp_request", method="POST", path="/api/billing/submit",
                      body={"invoice_id": i["id"], "company": i["company"], "amount": i["total"], "due_date": i["due_date"]})
        script = Script(tc("erp_request", method="GET", path="/api/invoices", query={"query": "FinanceFirst"}), pay,
                        lambda m: tc("finish", status=last(m)["finish_with"], summary=last(m)["message"]))
        result, _, _ = self.run_agent("Pay the FinanceFirst invoice", script)
        self.assertEqual(result["status"], "already_paid")
        self.assertEqual(self.submissions(), [])

    def test_wrong_amount_is_blocked_and_the_model_can_correct_itself(self):
        # the model first types the pre-tax subtotal; the guard blocks; the model re-reads, corrects, succeeds
        ctx = {}
        def pick(m):
            ctx["inv"] = last(m)["data"]["invoices"][0]
            return tc("browser_open", url="/billing")
        def fill_wrong(m):
            i = ctx["inv"]
            return [tc("browser_fill", selector=selector(m, "Invoice Number"), value=i["id"]),
                    tc("browser_fill", selector=selector(m, "Vendor"), value=i["company"]),
                    tc("browser_fill", selector=selector(m, "Payment Amount"), value=str(i["amount"])),   # subtotal!
                    tc("browser_fill", selector=selector(m, "Due Date"), value=i["due_date"])]
        def blocked_then_fix(m):
            r = last(m) if False else [x for x in results(m) if x.get("blocked_by_guard")][-1]
            self.assertEqual(r["code"], "discrepancy")
            return tc("browser_fill", selector=selector(m, "Payment Amount"), value=str(ctx["inv"]["total"]))
        script = Script(
            tc("erp_request", method="GET", path="/api/invoices", query={"query": "GlobalInc"}), pick, fill_wrong,
            lambda m: tc("browser_click", selector=submit_selector(m)),
            blocked_then_fix,
            lambda m: tc("browser_click", selector=submit_selector(m)),
            lambda m: tc("finish", status="completed", summary="Paid after correcting the amount.",
                         confirmation_id=self.submissions()[0]["confirmation_id"]))
        result, events, _ = self.run_agent("Pay the GlobalInc invoice.", script)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.submissions()), 1)
        self.assertEqual(self.submissions()[0]["amount"], 37760)
        self.assertTrue(any(e.get("blocked") for e in events if e["type"] == "tool_result"))

    def test_invoice_document_that_disagrees_with_the_erp_is_blocked(self):
        # ERP total is changed behind the document's back; the model reads the document (₹37,760) but types the ERP value
        inv = self.invoice("INV-1043")
        with open(srv.INVOICES_FILE) as fh:
            data = json.load(fh)
        for i in data:
            if i["id"] == "INV-1043":
                i["total"] = 35000
        with open(srv.INVOICES_FILE, "w") as fh:
            json.dump(data, fh)
        turns, _ = self.pay_turns("GlobalInc", finish=False)
        script = Script(*turns[:-1], lambda m: tc("finish", status="discrepancy", summary="Stopped: " + last(m)["message"]))
        # turns[:-1] drops the verify step; the click is blocked, so go straight to finish
        result, _, _ = self.run_agent("Pay the GlobalInc invoice.", script)
        self.assertEqual(result["status"], "discrepancy")
        self.assertEqual(self.submissions(), [])

    def test_click_fails_so_the_model_switches_to_the_api(self):
        ctx = {}
        def pick(m):
            ctx["inv"] = last(m)["data"]["invoices"][0]
            return tc("browser_open", url="/billing")
        def fill(m):
            i = ctx["inv"]
            return [tc("browser_fill", selector=selector(m, "Invoice Number"), value=i["id"]),
                    tc("browser_fill", selector=selector(m, "Payment Amount"), value=str(i["total"]))]
        def fallback(m):
            self.assertFalse(last(m)["ok"])           # the click failed and said so
            i = ctx["inv"]
            return tc("erp_request", method="POST", path="/api/billing/submit",
                      body={"invoice_id": i["id"], "company": i["company"], "amount": i["total"], "due_date": i["due_date"]})
        script = Script(tc("erp_request", method="GET", path="/api/invoices", query={"query": "GlobalInc"}), pick, fill,
                        lambda m: tc("browser_click", selector=submit_selector(m)), fallback,
                        lambda m: tc("verify_payment", invoice_id="INV-1043"),
                        lambda m: tc("finish", status="completed", confirmation_id=last(m)["confirmation_id"], summary="Paid via the API after the UI failed."))
        result, _, _ = self.run_agent("Pay the GlobalInc invoice.", script, browser=FakeBrowser(self.base, fail_click=True))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["writes"][0]["via"], "api")
        self.assertEqual(len(self.submissions()), 1)

    def test_missed_banner_cannot_cause_a_double_payment(self):
        # the click works server-side but the banner never shows. The model tries the API: the guard knows better.
        ctx = {}
        def pick(m):
            ctx["inv"] = last(m)["data"]["invoices"][0]
            return tc("browser_open", url="/billing")
        def fill(m):
            i = ctx["inv"]
            return [tc("browser_fill", selector=selector(m, "Invoice Number"), value=i["id"]),
                    tc("browser_fill", selector=selector(m, "Payment Amount"), value=str(i["total"]))]
        def retry_via_api(m):
            self.assertEqual(last(m)["page"]["alerts"], [])      # the model really saw no banner
            i = ctx["inv"]
            return tc("erp_request", method="POST", path="/api/billing/submit",
                      body={"invoice_id": i["id"], "company": i["company"], "amount": i["total"], "due_date": i["due_date"]})
        def adopt(m):
            r = last(m)
            self.assertEqual(r["code"], "already_done_by_this_run")
            return tc("verify_payment", invoice_id="INV-1043")
        script = Script(tc("erp_request", method="GET", path="/api/invoices", query={"query": "GlobalInc"}), pick, fill,
                        lambda m: tc("browser_click", selector=submit_selector(m)), retry_via_api, adopt,
                        lambda m: tc("finish", status="completed", confirmation_id=last(m)["confirmation_id"], summary="Paid once; verified."))
        result, _, _ = self.run_agent("Pay the GlobalInc invoice.", script, browser=FakeBrowser(self.base, hide_banner=True))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.submissions()), 1)             # exactly one payment

    def test_fabricated_confirmation_id_is_rejected(self):
        script = Script(
            tc("erp_request", method="GET", path="/api/invoices"),
            tc("finish", status="completed", summary="Paid. Confirmation APX-CONF-00000-1043."),
            lambda m: (self.assertTrue(last(m)["blocked_by_guard"]), tc("finish", status="failed", summary="I did not pay anything; my earlier claim was wrong."))[1])
        result, _, _ = self.run_agent("Pay INV-1043", script)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.submissions(), [])

    def test_repeated_unverifiable_claims_end_as_failed(self):
        fake = tc("finish", status="completed", summary="Done: APX-CONF-11111-0000")
        script = Script(fake, tc("finish", status="completed", summary="Done: APX-CONF-11111-0000"))
        result, _, _ = self.run_agent("Pay INV-1043", script)
        self.assertEqual(result["status"], "failed")


class HumanInTheLoopTests(ERPCase):
    def approval_script(self, vendor_query):
        turns, ctx = self.pay_turns(vendor_query)
        return Script(*turns), ctx

    def test_large_amount_needs_approval_and_a_rejection_stops_everything(self):
        script, _ = self.approval_script("MegaRetail")
        result, events, questions = self.run_agent("Pay the MegaRetail invoice.", script, answers=[{"approved": False}])
        self.assertEqual(result["status"], "declined")
        self.assertEqual(questions[0]["kind"], "approval")
        self.assertIn("approval limit", questions[0]["question"])
        self.assertEqual(self.submissions(), [])

    def test_large_amount_proceeds_after_approval(self):
        script, _ = self.approval_script("MegaRetail")
        result, _, questions = self.run_agent("Pay the MegaRetail invoice.", script, answers=[{"approved": True}])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(questions), 1)
        self.assertEqual(self.submissions()[0]["amount"], 141600)

    def test_paying_someone_the_user_never_named_needs_approval(self):
        # "oldest pending invoice" names no vendor, so the model's pick is not grounded in what the user said
        script, _ = self.approval_script("GlobalInc")
        result, _, questions = self.run_agent("Pay the oldest pending invoice.", script, answers=[{"approved": False}])
        self.assertEqual(result["status"], "declined")
        self.assertIn("did not name", questions[0]["question"])
        self.assertEqual(self.submissions(), [])

    def test_unknown_vendor_is_clarified_not_guessed(self):
        ctx = {}
        def search_empty(m):
            self.assertEqual(last(m)["data"]["count"], 0)
            return tc("ask_user", question="I can't find 'Acme Corp'. Which vendor did you mean?",
                      options=["TechCorp Solutions", "GlobalInc Pvt Ltd", "StartupXYZ"])
        turns, _ = self.pay_turns("GlobalInc")
        script = Script(tc("erp_request", method="GET", path="/api/invoices", query={"query": "Acme"}), search_empty, *turns)
        result, events, questions = self.run_agent("Pay the Acme Corp invoice.", script, answers=[{"answer": "GlobalInc Pvt Ltd"}])
        self.assertEqual(result["status"], "completed")
        self.assertEqual([q["kind"] for q in questions], ["clarification"])   # asked once; the answer grounds the payment
        self.assertEqual(self.submissions()[0]["invoice_id"], "INV-1043")

    def test_no_answer_means_nothing_is_changed(self):
        script = Script(tc("ask_user", question="Which vendor?"))
        result, _, _ = self.run_agent("Pay the invoice.", script, ask=False)
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(self.submissions(), [])

    def test_a_write_with_no_safety_policy_requires_approval(self):
        script = Script(tc("erp_request", method="POST", path="/api/something/new", body={"x": 1}))
        result, _, questions = self.run_agent("Do something unusual", script, answers=[{"approved": False}])
        self.assertEqual(result["status"], "declined")
        self.assertIn("no safety policy", questions[0]["question"])


class RobustnessTests(ERPCase):
    def test_same_failing_call_is_cut_off(self):
        bad = tc("erp_request", method="GET", path="/api/invoices/INV-9999")
        script = Script(*[(lambda m: tc("erp_request", method="GET", path="/api/invoices/INV-9999"))] * 6)
        result, _, _ = self.run_agent("Look up INV-9999", script)
        self.assertEqual(result["status"], "failed")
        self.assertIn("stuck", result["summary"].lower())

    def test_step_budget_ends_a_runaway_agent(self):
        script = Script(*[(lambda m: tc("update_plan", steps=["again"]))] * 10)
        result, _, _ = self.run_agent("loop forever", script, max_steps=4)
        self.assertEqual(result["status"], "failed")
        self.assertIn("budget", result["summary"].lower())

    def test_a_forgotten_method_defaults_to_a_safe_read_but_never_guesses_a_write(self):
        def check(m):
            rs = results(m)
            self.assertTrue(rs[-2]["ok"])                       # no method + no body -> a plain GET worked
            self.assertIn("Specify the method", rs[-1]["error"])  # a body with no method is refused, not guessed
            return tc("finish", status="completed", summary="ok")
        script = Script(tc("erp_request", path="/api/invoices"),
                        tc("erp_request", path="/api/billing/submit", body={"invoice_id": "INV-1043", "amount": 1}), check)
        result, _, _ = self.run_agent("test", script)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.submissions(), [])

    def test_unknown_tool_and_bad_arguments_are_reported_back_to_the_model(self):
        def recover(m):
            rs = results(m)
            self.assertIn("Unknown tool", rs[-3]["error"])
            self.assertIn("Bad arguments", rs[-2]["error"])
            self.assertIn("not valid JSON", rs[-1]["error"])
            return tc("finish", status="completed", summary="recovered")
        bad_json = {"id": "x1", "name": "list_files", "arguments": {"_parse_error": "{oops"}, "raw_arguments": "{oops"}
        script = Script(tc("teleport", where="moon"), tc("list_files", wrong_arg=1), bad_json, recover)
        result, _, _ = self.run_agent("test", script)
        self.assertEqual(result["status"], "completed")

    def test_model_outage_is_reported_not_crashed(self):
        class Down:
            configured, model = True, "down"
            def chat(self, *_):
                raise LLMError("Model call failed: 503")
        result, events, _ = self.run_agent("anything", Down())
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(e["type"] == "error" for e in events))

    def test_without_a_running_model_the_agent_refuses_to_pretend(self):
        orch = TaskOrchestrator(base_url=self.base, llm=LLMClient(host="http://127.0.0.1:9"))
        result = orch.run_task("Find the latest invoice from TechCorp and pay it")
        self.assertEqual(result["status"], "failed")
        self.assertIn("not reachable", result["summary"])
        self.assertEqual(self.submissions(), [])

    def test_plain_text_after_tools_is_nudged_to_finish(self):
        script = Script(tc("list_files"), "I think I'm done.", tc("finish", status="completed", summary="Listed the files."))
        result, _, _ = self.run_agent("list the files", script)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"], "Listed the files.")

    def test_working_memory_is_shown_to_the_model_each_turn(self):
        script = Script(tc("remember", key="vendor_pick", value="GlobalInc"), tc("finish", status="completed", summary="ok"))
        self.run_agent("remember something", script)
        second_turn = script.seen[1]
        self.assertIn("vendor_pick: GlobalInc", second_turn[-1]["content"])     # note sits at the END...
        self.assertNotIn("vendor_pick", second_turn[0]["content"])             # ...so the prompt prefix stays cacheable
        self.assertEqual(script.seen[0][0]["content"], second_turn[0]["content"])


class ToolTests(ERPCase):
    def test_file_tool_cannot_leave_the_workspace(self):
        ft = FileTool()
        self.assertFalse(ft.read_file("../app/server.py")["ok"])
        self.assertFalse(ft.read_file("/etc/passwd")["ok"])
        self.assertTrue(ft.read_file("invoices/INV-1042_TechCorp_Solutions.txt")["ok"])

    def test_invoice_documents_come_back_parsed(self):
        r = FileTool().read_file("invoices/INV-1042_TechCorp_Solutions.txt")
        self.assertEqual(r["parsed_invoice"]["amount"], 99710.0)
        self.assertEqual(r["parsed_invoice"]["due_date"], "2024-11-15")

    def test_erp_publishes_a_catalogue_the_agent_can_discover(self):
        cat = requests.get(f"{self.base}/api").json()
        self.assertIn("/api/billing/submit", [e["path"] for e in cat["endpoints"]])

    def test_browser_may_not_leave_the_erp(self):
        script = Script(tc("browser_open", url="https://example.com"),
                        lambda m: tc("finish", status="failed" if not last(m)["ok"] else "completed", summary="blocked"))
        result, _, _ = self.run_agent("open example.com", script)
        self.assertEqual(result["history"][0]["status"], "failed")

    def test_long_conversations_are_compacted(self):
        msgs = [{"role": "system", "content": "s"}] + [{"role": "tool", "content": "x" * 20000, "tool_call_id": str(i)} for i in range(8)] \
               + [{"role": "user", "content": "recent"}] * 8
        TaskOrchestrator._compact(msgs)
        self.assertLess(len(msgs[1]["content"]), 600)


class FakeOllama:
    """A tiny HTTP server that speaks Ollama's /api/tags and /api/chat, so LLMClient's real HTTP path is exercised."""
    def __init__(self, models=("qwen2.5:7b",)):
        from flask import Flask, jsonify, request
        self.replies, self.requests, self.models = [], [], list(models)
        app = Flask("fake_ollama")

        @app.get("/api/tags")
        def tags():
            return jsonify({"models": [{"name": m} for m in self.models]})

        @app.post("/api/chat")
        def chat():
            body = request.get_json()
            self.requests.append(body)
            reply = self.replies.pop(0)
            if isinstance(reply, tuple):                       # (status_code, text)
                return reply[1], reply[0]
            return jsonify({"model": body["model"], "message": reply, "done": True})

        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


def call(name, args):
    return {"function": {"name": name, "arguments": args}}


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        self.ollama = FakeOllama()
        self.addCleanup(self.ollama.stop)
        self.llm = LLMClient(host=self.ollama.url, sleep=lambda s: None)

    def test_tool_calls_are_parsed_and_the_request_is_configured_for_local_use(self):
        self.ollama.replies.append({"role": "assistant", "content": "looking", "tool_calls": [call("read_file", {"path": "a.txt"})]})
        out = self.llm.chat([{"role": "user", "content": "hi"}], TOOL_SPECS)
        self.assertEqual((out["tool_calls"][0]["name"], out["tool_calls"][0]["arguments"]), ("read_file", {"path": "a.txt"}))
        sent = self.ollama.requests[0]
        self.assertFalse(sent["stream"])
        self.assertEqual(sent["options"], {"temperature": 0, "num_ctx": 16384})
        self.assertEqual(sent["keep_alive"], "30m")                              # stay loaded between steps   # a big window: Ollama's default would truncate
        self.assertEqual(len(sent["tools"]), len(TOOL_SPECS))

    def test_arguments_sent_as_a_json_string_are_accepted(self):
        self.ollama.replies.append({"role": "assistant", "content": "", "tool_calls": [call("read_file", '{"path": "b.txt"}')]})
        self.assertEqual(self.llm.chat([], TOOL_SPECS)["tool_calls"][0]["arguments"], {"path": "b.txt"})

    def test_a_tool_call_written_as_text_is_recovered(self):
        text = '```json\n{"name": "list_files", "arguments": {"subdir": "invoices"}}\n```'
        self.ollama.replies.append({"role": "assistant", "content": text})
        out = self.llm.chat([], TOOL_SPECS)
        self.assertEqual(out["tool_calls"][0]["name"], "list_files")
        self.assertIsNone(out["content"])

    def test_ordinary_json_in_an_answer_is_not_mistaken_for_a_tool_call(self):
        self.ollama.replies.append({"role": "assistant", "content": 'The config is {"name": "settings", "arguments": {"a": 1}}.'})
        out = self.llm.chat([], TOOL_SPECS)
        self.assertEqual(out["tool_calls"], [])
        self.assertIn("settings", out["content"])

    def test_think_blocks_are_stripped(self):
        self.ollama.replies.append({"role": "assistant", "content": "<think>hmm, tricky</think>The answer is 4."})
        self.assertEqual(self.llm.chat([], TOOL_SPECS)["content"], "The answer is 4.")

    def test_server_errors_are_retried_then_succeed(self):
        self.ollama.replies += [(503, "busy"), (500, "oops"), {"role": "assistant", "content": "ok"}]
        self.assertEqual(self.llm.chat([], TOOL_SPECS)["content"], "ok")
        self.assertEqual(len(self.ollama.requests), 3)

    def test_persistent_failure_raises_a_clear_error(self):
        self.ollama.replies += [(500, "x")] * 3
        with self.assertRaises(LLMError):
            self.llm.chat([], TOOL_SPECS)

    def test_missing_model_says_how_to_pull_it(self):
        self.ollama.replies.append((404, "model not found"))
        with self.assertRaisesRegex(LLMError, "ollama pull qwen2.5:7b"):
            self.llm.chat([], TOOL_SPECS)

    def test_a_model_without_tool_support_is_explained(self):
        self.ollama.replies.append((400, '{"error":"registry.ollama.ai/library/gemma does not support tools"}'))
        with self.assertRaisesRegex(LLMError, "does not support tool calling"):
            self.llm.chat([], TOOL_SPECS)

    def test_check_reports_ready_missing_model_and_unreachable(self):
        self.assertEqual(self.llm.check(), (True, ""))
        ok, msg = LLMClient(host=self.ollama.url, model="llama3.1").check()
        self.assertFalse(ok)
        self.assertIn("ollama pull llama3.1", msg)
        ok, msg = LLMClient(host="http://127.0.0.1:9").check()
        self.assertFalse(ok)
        self.assertIn("not reachable", msg)

    def test_a_bare_model_name_matches_its_latest_tag(self):
        self.ollama.models = ["llama3.1:latest"]
        self.assertTrue(LLMClient(host=self.ollama.url, model="llama3.1").check()[0])

    def test_openai_style_history_is_translated_for_ollama(self):
        history = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"},
                   {"role": "assistant", "content": "", "tool_calls": [
                       {"id": "c1", "type": "function", "function": {"name": "list_files", "arguments": '{"subdir": "x"}'}}]},
                   {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'}]
        self.ollama.replies.append({"role": "assistant", "content": "done"})
        self.llm.chat(history, TOOL_SPECS)
        sent = self.ollama.requests[0]["messages"]
        self.assertEqual(sent[2]["tool_calls"][0]["function"]["arguments"], {"subdir": "x"})   # dict, not a string
        self.assertEqual(sent[3]["tool_name"], "list_files")

    def test_host_and_model_come_from_the_environment(self):
        os.environ.update({"OLLAMA_HOST": "0.0.0.0:11434", "OLLAMA_MODEL": "llama3.1:8b", "OLLAMA_NUM_CTX": "8192"})
        try:
            c = LLMClient()
            self.assertEqual((c.host, c.model, c.num_ctx), ("http://localhost:11434", "llama3.1:8b", 8192))
            self.assertEqual(c.context_chars, 20480)
        finally:
            for k in ("OLLAMA_HOST", "OLLAMA_MODEL", "OLLAMA_NUM_CTX"):
                os.environ.pop(k, None)


class AgentOverOllamaTests(ERPCase):
    """The whole agent loop talking to a (fake) Ollama over real HTTP, with a small-model-style quirk or two."""
    def setUp(self):
        super().setUp()
        self.ollama = FakeOllama()
        self.addCleanup(self.ollama.stop)

    def run_with(self, prompt, replies, **kw):
        self.ollama.replies += replies
        orch = TaskOrchestrator(base_url=self.base, llm=LLMClient(host=self.ollama.url, sleep=lambda s: None), **kw)
        events = []
        return orch.run_task(prompt, on_event=events.append), events

    def test_a_plain_answer_needs_no_tools(self):
        result, _ = self.run_with("What is the capital of France?", [{"role": "assistant", "content": "Paris."}])
        self.assertEqual((result["status"], result["summary"]), ("completed", "Paris."))

    def test_a_write_task_end_to_end_with_stringified_arguments(self):
        note = "Needs approval from finance."
        replies = [
            {"role": "assistant", "tool_calls": [call("update_plan", '{"steps": [{"text": "add note", "status": "in_progress"}]}')]},
            {"role": "assistant", "tool_calls": [call("erp_request", {"method": "POST", "path": "/api/invoices/INV-1043/notes",
                                                                        "body": '{"note": "%s"}' % note})]},
            {"role": "assistant", "content": '{"name": "finish", "arguments": {"status": "completed", "summary": "Note added to INV-1043."}}'}]
        result, events = self.run_with("Add a note to INV-1043: " + note, replies)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.invoice("INV-1043")["notes"][0]["text"], note)
        self.assertEqual(result["plan"][0]["description"], "add note")          # the stringified plan was understood
        self.assertTrue(any(e["type"] == "thinking" for e in events))

    def test_empty_replies_are_nudged_then_give_up(self):
        result, _ = self.run_with("hello", [{"role": "assistant", "content": ""}] * 4)
        self.assertEqual(result["status"], "failed")
        self.assertIn("empty", result["summary"])

    def test_the_context_budget_follows_the_models_window(self):
        self.ollama.replies.append({"role": "assistant", "content": "ok"})
        llm = LLMClient(host=self.ollama.url, num_ctx=4096, sleep=lambda s: None)
        self.assertEqual(llm.context_chars, 10240)


def _chromium_available():
    try:
        from playwright.sync_api import sync_playwright
        p = sync_playwright().start()
        b = p.chromium.launch()
        b.close()
        p.stop()
        return True
    except Exception:
        return False


@unittest.skipUnless(_chromium_available(), "Chromium is not available (run: playwright install chromium)")
class RealBrowserTests(ERPCase):
    """The same agent loop, but the browser is a real headless Chromium reading and driving the real /billing page."""

    def test_real_chromium_discovers_the_form_and_pays(self):
        from agent.tools.browser_tool import BrowserTool
        shots = tempfile.mkdtemp()
        browser = BrowserTool(headless=True, screenshots_dir=shots)
        self.addCleanup(browser.close)
        self.addCleanup(shutil.rmtree, shots, True)
        turns, _ = self.pay_turns("GlobalInc")
        result, events, _ = self.run_agent("Pay the GlobalInc invoice.", Script(*turns), browser=browser)
        self.assertEqual(result["status"], "completed", result.get("summary"))
        self.assertEqual(len(self.submissions()), 1)
        snaps = [e for e in events if e["type"] == "tool_result" and e["tool"].startswith("browser_")]
        self.assertTrue(snaps)
        self.assertTrue(any(e.get("screenshot") for e in snaps))
        self.assertEqual(result["writes"][0]["via"], "browser")

    def test_snapshot_reports_labels_options_and_submit_buttons(self):
        from agent.tools.browser_tool import BrowserTool
        shots = tempfile.mkdtemp()
        b = BrowserTool(headless=True, screenshots_dir=shots)
        self.addCleanup(b.close)
        self.addCleanup(shutil.rmtree, shots, True)
        b.navigate(self.base + "/billing")
        snap = b.snapshot()
        labels = [e["label"] for e in snap["elements"]]
        self.assertTrue(any("Invoice Number" in l for l in labels))
        select = next(e for e in snap["elements"] if e["tag"] == "select")
        self.assertIn("Security Audit", [o["value"] for o in select["options"]])
        submit = next(e for e in snap["elements"] if e.get("submits_form"))
        self.assertTrue(b.is_submit(submit["selector"]))
        self.assertFalse(b.is_submit("#input-amount"))


if __name__ == "__main__":
    unittest.main()
