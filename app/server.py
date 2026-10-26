import os
import sys
import json
import time
import random
import uuid
import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from flask import Flask, request, jsonify, render_template, send_from_directory, Response

try:
    from flask_cors import CORS
    has_cors = True
except ImportError:
    has_cors = False

app = Flask(__name__, template_folder="templates", static_folder="static")

if has_cors:
    CORS(app)
else:
    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        return response

@app.after_request
def no_cache_pages(response):
    """Pages are tiny and change while developing: never let a browser show a stale copy."""
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Agent-Build"] = "tool-calling"
    return response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
INVOICES_FILE = os.path.join(DATA_DIR, "invoices.json")
SUBMISSIONS_FILE = os.path.join(DATA_DIR, "submissions.json")
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "static", "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Helper to read/write JSON
def load_json(filepath, default=None):
    if not os.path.exists(filepath):
        return default if default is not None else []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else []

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# --- Routes for Mock Company ERP ("ApexFlow Enterprise") ---

@app.route("/")
def dashboard():
    invoices = load_json(INVOICES_FILE, [])
    submissions = load_json(SUBMISSIONS_FILE, [])
    pending_count = sum(1 for inv in invoices if inv.get("status") == "pending")
    total_payable = sum(inv.get("amount", 0) for inv in invoices if inv.get("status") == "pending")
    return render_template(
        "dashboard.html",
        invoices=invoices,
        submissions=submissions,
        pending_count=pending_count,
        total_payable=total_payable,
        total_invoices=len(invoices)
    )

@app.route("/invoices")
def invoices_page():
    invoices = load_json(INVOICES_FILE, [])
    query = request.args.get("search", "").lower()
    if query:
        invoices = [
            inv for inv in invoices
            if query in inv.get("company", "").lower() or query in inv.get("id", "").lower()
        ]
    return render_template("invoices.html", invoices=invoices, search=query)

@app.route("/invoices/<inv_id>")
def invoice_detail(inv_id):
    invoices = load_json(INVOICES_FILE, [])
    inv = next((i for i in invoices if i.get("id") == inv_id), None)
    if not inv:
        return render_template("404.html", message="Invoice not found"), 404
    return render_template("invoice_detail.html", invoice=inv)

@app.route("/billing")
def billing_portal():
    inv_id = request.args.get("invoice_id", "")
    invoices = load_json(INVOICES_FILE, [])
    preset = next((i for i in invoices if i.get("id") == inv_id), None)
    return render_template("billing_portal.html", preset=preset or {})

@app.route("/submissions")
def submissions_page():
    submissions = load_json(SUBMISSIONS_FILE, [])
    # reverse order so newest is first
    submissions = list(reversed(submissions))
    return render_template("submissions.html", submissions=submissions)

# --- Mock ERP REST API Endpoints ---

@app.route("/api/invoices", methods=["GET"])
def api_get_invoices():
    invoices = load_json(INVOICES_FILE, [])
    query = request.args.get("query", "").lower()
    status = request.args.get("status", "").lower()
    results = invoices
    if query:
        results = [
            i for i in results
            if query in i.get("company", "").lower() or query in i.get("id", "").lower()
        ]
    if status:
        results = [i for i in results if i.get("status", "").lower() == status]
    return jsonify({"success": True, "count": len(results), "invoices": results})

@app.route("/api/invoices/<inv_id>", methods=["GET"])
def api_get_invoice(inv_id):
    invoices = load_json(INVOICES_FILE, [])
    inv = next((i for i in invoices if i.get("id").upper() == inv_id.upper()), None)
    if not inv:
        return jsonify({"success": False, "error": f"Invoice {inv_id} not found"}), 404
    return jsonify({"success": True, "invoice": inv})

@app.route("/api", methods=["GET"])
@app.route("/api/", methods=["GET"])
def api_catalogue():
    """Self-describing catalogue so an agent can discover what this system can do."""
    return jsonify({
        "service": "ApexFlow Enterprise ERP",
        "currency": "INR (an invoice's payable amount is its tax-inclusive 'total')",
        "endpoints": [
            {"method": "GET", "path": "/api/invoices", "query": ["query (vendor name or invoice id substring)", "status"],
             "description": "List invoices (fields: id, company, amount=subtotal, tax, total=payable, due_date, issue_date, status, category, notes)."},
            {"method": "GET", "path": "/api/invoices/<id>", "description": "One invoice in full."},
            {"method": "POST", "path": "/api/invoices/<id>/notes", "body": {"note": "text"},
             "description": "Attach a note to an invoice (low risk)."},
            {"method": "POST", "path": "/api/billing/submit",
             "body": {"invoice_id": "", "company": "", "amount": "payable total", "due_date": "YYYY-MM-DD", "category": "", "notes": ""},
             "description": "Submit an invoice for payment. SIDE EFFECT; also available as the form on /billing."},
            {"method": "GET", "path": "/api/submissions", "description": "Audit log of every payment submission (the source of truth)."},
            {"method": "GET", "path": "/api/verify/<invoice_id>", "query": ["confirmation_id"],
             "description": "Check whether a payment record exists for an invoice."},
        ],
        "ui_pages": ["/", "/invoices", "/invoices/<id>", "/billing", "/submissions"],
    })

@app.route("/api/invoices/<inv_id>/notes", methods=["POST"])
def api_add_note(inv_id):
    data = request.get_json() or {}
    text = str(data.get("note", "")).strip()
    if not text:
        return jsonify({"success": False, "error": "The 'note' field is required."}), 400
    invoices = load_json(INVOICES_FILE, [])
    inv = next((i for i in invoices if i.get("id", "").upper() == inv_id.upper()), None)
    if not inv:
        return jsonify({"success": False, "error": f"Invoice {inv_id} not found"}), 404
    inv.setdefault("notes", []).append({"text": text, "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    save_json(INVOICES_FILE, invoices)
    return jsonify({"success": True, "invoice_id": inv["id"], "notes": inv["notes"]}), 201

@app.route("/api/billing/submit", methods=["POST"])
def api_submit_billing():
    data = request.get_json() or request.form.to_dict()
    
    invoice_id = data.get("invoice_id", "").strip().upper()
    company = data.get("company", "").strip()
    amount_str = str(data.get("amount", "")).replace(",", "").strip()
    due_date = data.get("due_date", "").strip()
    category = data.get("category", "General")
    notes = data.get("notes", "Submitted via automated billing process")
    
    if not invoice_id or not amount_str:
        return jsonify({
            "success": False,
            "error": "Missing required fields: invoice_id and amount are mandatory."
        }), 400
    
    try:
        amount = float(amount_str)
    except ValueError:
        return jsonify({
            "success": False,
            "error": f"Invalid amount format: {amount_str}"
        }), 400

    # Guards: never record a payment for an unknown invoice, a duplicate, or a wrong amount
    invoices = load_json(INVOICES_FILE, [])
    matched_inv = next((i for i in invoices if i.get("id").upper() == invoice_id), None)
    if not matched_inv:
        return jsonify({
            "success": False,
            "error": f"Invoice {invoice_id} does not exist in the ERP; refusing to record a payment for it."
        }), 404

    existing = [s for s in load_json(SUBMISSIONS_FILE, []) if s.get("invoice_id", "").upper() == invoice_id]
    if existing:
        return jsonify({
            "success": False,
            "error": f"Invoice {invoice_id} was already submitted (confirmation {existing[-1].get('confirmation_id')}); duplicate payment blocked.",
            "existing_confirmation_id": existing[-1].get("confirmation_id")
        }), 409
    if str(matched_inv.get("status", "")).lower() == "paid":
        return jsonify({"success": False, "error": f"Invoice {invoice_id} is already marked as paid in the ERP."}), 409

    payable = float(matched_inv.get("total", matched_inv.get("amount", 0)))
    if abs(payable - amount) > 1.0:
        return jsonify({
            "success": False,
            "error": f"Amount mismatch: submitted ₹{amount:,.2f} but the ERP payable total for {invoice_id} is ₹{payable:,.2f}."
        }), 422

    # Generate unique confirmation ID
    confirmation_id = f"APX-CONF-{random.randint(10000, 99999)}-{invoice_id[-4:] if len(invoice_id)>=4 else '0000'}"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    submission_record = {
        "confirmation_id": confirmation_id,
        "invoice_id": invoice_id,
        "company": company or matched_inv.get("company"),
        "amount": amount,
        "due_date": due_date or matched_inv.get("due_date", ""),
        "category": category,
        "notes": notes,
        "timestamp": timestamp,
        "status": "APPROVED_AND_SCHEDULED",
        "verified": True
    }

    submissions = load_json(SUBMISSIONS_FILE, [])
    submissions.append(submission_record)
    save_json(SUBMISSIONS_FILE, submissions)

    matched_inv["status"] = "processing"
    save_json(INVOICES_FILE, invoices)

    return jsonify({
        "success": True,
        "message": f"Invoice {invoice_id} successfully submitted for payment processing.",
        "confirmation_id": confirmation_id,
        "record": submission_record
    }), 200

@app.route("/api/submissions", methods=["GET"])
def api_get_submissions():
    submissions = load_json(SUBMISSIONS_FILE, [])
    return jsonify({"success": True, "count": len(submissions), "submissions": submissions})

@app.route("/api/verify/<invoice_id>", methods=["GET"])
def api_verify_submission(invoice_id):
    submissions = load_json(SUBMISSIONS_FILE, [])
    invoices = load_json(INVOICES_FILE, [])
    inv_id = invoice_id.strip().upper()

    # 1. Check Audit Log (submissions.json)
    matched_subs = [s for s in submissions if s.get("invoice_id", "").upper() == inv_id]
    wanted_conf = request.args.get("confirmation_id", "").strip()
    if wanted_conf:
        matched_subs = [s for s in matched_subs if s.get("confirmation_id") == wanted_conf]
    if not matched_subs:
        return jsonify({
            "verified": False,
            "invoice_id": inv_id,
            "message": f"Verification failed: No record for invoice '{inv_id}'"
                       + (f" with confirmation '{wanted_conf}'" if wanted_conf else "")
                       + " found in the submitted audit log."
        }), 404

    latest_sub = matched_subs[-1]

    # 2. Check Original Vendor Invoices Record (invoices.json)
    matched_inv = next((i for i in invoices if i.get("id", "").upper() == inv_id), None)

    # 3. Two-way reconciliation check
    amount_match = True
    if matched_inv and "amount" in latest_sub:
        payable = float(matched_inv.get("total", matched_inv.get("amount", 0)))
        amount_match = abs(payable - float(latest_sub["amount"])) <= 1.0

    return jsonify({
        "verified": True,
        "two_way_reconciled": amount_match,
        "invoice_id": inv_id,
        "confirmation_id": latest_sub.get("confirmation_id"),
        "amount": latest_sub.get("amount"),
        "timestamp": latest_sub.get("timestamp"),
        "status": latest_sub.get("status"),
        "audit_log_record": {
            "found": True,
            "submitted_amount": latest_sub.get("amount"),
            "due_date": latest_sub.get("due_date"),
            "timestamp": latest_sub.get("timestamp"),
            "status": latest_sub.get("status")
        },
        "vendor_invoice_record": {
            "found": matched_inv is not None,
            "vendor_name": matched_inv.get("company") if matched_inv else None,
            "invoice_amount": matched_inv.get("total", matched_inv.get("amount")) if matched_inv else None,
            "current_erp_status": matched_inv.get("status") if matched_inv else None
        },
        "amount_reconciled": amount_match,
        "details": latest_sub
    })

@app.route("/api/reset", methods=["POST"])
def api_reset():
    # Resets submissions and initial invoices for testing
    initial_invoices = [
        {
            "id": "INV-1042",
            "company": "TechCorp Solutions",
            "contact": "Rahul Sharma",
            "email": "rahul@techcorp.io",
            "amount": 84500,
            "currency": "INR",
            "due_date": "2024-11-15",
            "issue_date": "2024-10-15",
            "status": "pending",
            "category": "Software Development",
            "description": "Monthly retainer for backend API development services - October 2024",
            "tax": 15210,
            "total": 99710
        },
        {
            "id": "INV-1043",
            "company": "GlobalInc Pvt Ltd",
            "contact": "Priya Mehta",
            "email": "priya@globalinc.com",
            "amount": 32000,
            "currency": "INR",
            "due_date": "2024-11-20",
            "issue_date": "2024-10-20",
            "status": "pending",
            "category": "Cloud Infrastructure",
            "description": "AWS infrastructure setup and DevOps consulting - Q4 2024",
            "tax": 5760,
            "total": 37760
        },
        {
            "id": "INV-1044",
            "company": "StartupXYZ",
            "contact": "Amit Verma",
            "email": "amit@startupxyz.in",
            "amount": 55000,
            "currency": "INR",
            "due_date": "2024-11-10",
            "issue_date": "2024-10-10",
            "status": "overdue",
            "category": "UI/UX Design",
            "description": "Product design and prototyping for mobile app - Sprint 3",
            "tax": 9900,
            "total": 64900
        },
        {
            "id": "INV-1045",
            "company": "MegaRetail Ltd",
            "contact": "Sunita Patel",
            "email": "sunita@megaretail.com",
            "amount": 120000,
            "currency": "INR",
            "due_date": "2024-12-01",
            "issue_date": "2024-11-01",
            "status": "pending",
            "category": "Data Analytics",
            "description": "Business intelligence dashboard development and data pipeline setup",
            "tax": 21600,
            "total": 141600
        },
        {
            "id": "INV-1046",
            "company": "FinanceFirst Bank",
            "contact": "Vikram Singh",
            "email": "vikram@financefirst.in",
            "amount": 75000,
            "currency": "INR",
            "due_date": "2024-10-30",
            "issue_date": "2024-09-30",
            "status": "paid",
            "category": "Security Audit",
            "description": "Cybersecurity audit and penetration testing - Phase 1",
            "tax": 13500,
            "total": 88500
        }
    ]
    save_json(INVOICES_FILE, initial_invoices)
    save_json(SUBMISSIONS_FILE, [])
    return jsonify({"success": True, "message": "Database reset to initial demo state"})

# --- Mission Control UI and Streaming API for the AI Task Worker ---

import sys
import queue
import threading

ROOT_DIR = os.path.dirname(BASE_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from agent.orchestrator import TaskOrchestrator

# Active event queues for SSE streaming: task_id -> queue.Queue
agent_task_queues = {}
# Answers from the user to clarification / approval questions: task_id -> queue.Queue
agent_task_responses = {}
USER_RESPONSE_TIMEOUT = int(os.environ.get("AGENT_USER_TIMEOUT", 300))

@app.route("/agent")
def agent_mission_control():
    return render_template("agent_control.html")

@app.route("/api/agent/status")
def api_agent_status():
    from agent.llm import LLMClient
    llm = LLMClient.from_env()
    ready, message = llm.check()
    return jsonify({"llm_ready": ready, "message": message, "model": llm.model, "host": llm.host, "agent": "tool-calling"})

@app.route("/api/agent/run", methods=["POST"])
def api_run_agent():
    data = request.get_json() or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"success": False, "error": "Prompt cannot be empty."}), 400

    task_id = f"task_{uuid.uuid4().hex[:8]}"
    event_queue = queue.Queue()
    agent_task_queues[task_id] = event_queue
    agent_task_responses[task_id] = queue.Queue()

    port = int(os.environ.get("PORT", 5000))
    base_url = f"http://localhost:{port}"

    def background_worker():
        try:
            orchestrator = TaskOrchestrator(base_url=base_url, headless=True)
            def on_event(event):
                event_queue.put(event)

            def ask_user(question):
                """Blocks the agent until the user answers in Mission Control (or the wait times out)."""
                try:
                    return agent_task_responses[task_id].get(timeout=USER_RESPONSE_TIMEOUT)
                except queue.Empty:
                    return None

            result = orchestrator.run_task(prompt=prompt, task_id=task_id, on_event=on_event, ask_user=ask_user)
        except Exception as e:
            event_queue.put({
                "type": "error",
                "task_id": task_id,
                "message": f"Execution error: {str(e)}",
                "timestamp": time.strftime("%H:%M:%S")
            })
        finally:
            # End of stream sentinel
            event_queue.put(None)

    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()

    return jsonify({"success": True, "task_id": task_id})

@app.route("/api/agent/respond/<task_id>", methods=["POST"])
def api_agent_respond(task_id):
    """User's answer to a clarification ({"answer": "..."}) or approval ({"approved": true|false})."""
    if task_id not in agent_task_responses:
        return jsonify({"success": False, "error": f"Task {task_id} not found"}), 404
    data = request.get_json() or {}
    if "approved" not in data and not str(data.get("answer", "")).strip():
        return jsonify({"success": False, "error": "Provide 'answer' or 'approved'."}), 400
    agent_task_responses[task_id].put(data)
    return jsonify({"success": True})

@app.route("/api/agent/stream/<task_id>")
def api_agent_stream(task_id):
    if task_id not in agent_task_queues:
        return jsonify({"error": f"Task {task_id} not found"}), 404

    event_queue = agent_task_queues[task_id]

    def event_generator():
        while True:
            try:
                event = event_queue.get(timeout=30)
                if event is None:
                    # Stream complete
                    break
                yield f"data: {json.dumps(event)}\n\n"
            except queue.Empty:
                # Keep-alive heartbeat
                yield f": keep-alive\n\n"

    return Response(event_generator(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 ApexFlow Enterprise Mock Server starting on http://localhost:{port}")
    print(f"🤖 AI Task Worker Mission Control available at http://localhost:{port}/agent")
    app.run(host="0.0.0.0", port=port, debug=False)
