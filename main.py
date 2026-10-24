"""
Autonomous AI Task Worker - Main Entry Point

Usage:
  1. Start Web Server & Mission Control UI:
     python main.py

  2. Run Headless CLI Task directly:
     python main.py --cli --task "Find the latest invoice from TechCorp, extract the amount and due date, enter it into our internal system, and tell me once it is done."
"""

import sys
import os
import argparse
import time

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Ensure root directory is on Python path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

def run_server():
    """Starts the ApexFlow ERP Mock Server and Mission Control UI."""
    from app.server import app
    port = int(os.environ.get("PORT", 5000))
    print("=" * 65)
    print("[*] AUTONOMOUS AI TASK WORKER SYSTEM STARTING")
    print("=" * 65)
    print(f"[-] ApexFlow ERP Portal:           http://localhost:{port}")
    print(f"[-] Mission Control (Live Agent):  http://localhost:{port}/agent")
    print(f"[-] Invoices Directory:            http://localhost:{port}/invoices")
    print(f"[-] Billing & Payment Portal:      http://localhost:{port}/billing")
    print(f"[-] Audit Log & Verifications:     http://localhost:{port}/submissions")
    print("=" * 65)
    app.run(host="0.0.0.0", port=port, debug=False)

def run_cli(task_prompt: str, visual: bool = False):
    """Executes a task directly from the command line interface."""
    from agent.orchestrator import TaskOrchestrator

    print("=" * 65)
    print("[*] AUTONOMOUS AI TASK WORKER - CLI MODE")
    print(f"[*] Goal: {task_prompt}")
    print("=" * 65)

    def print_event(evt):
        etype = evt.get("type")
        timestamp = evt.get("timestamp", time.strftime("%H:%M:%S"))
        if etype == "thought":
            print(f"\n[{timestamp}] 🧠 THOUGHT: {evt.get('thought')}")
        elif etype == "plan":
            print(f"\n[{timestamp}] 📋 ACTION PLAN ({len(evt.get('plan', []))} steps):")
            for s in evt.get("plan", []):
                print(f"   [{s['id']}] {s['description']} (Tool: {s.get('tool')})")
        elif etype == "step_result":
            status_icon = "✓" if evt.get("status") == "completed" else "✗"
            print(f"[{timestamp}] {status_icon} STEP {evt.get('step_id')}: {evt.get('observation')}")
        elif etype == "completed":
            print("\n" + "=" * 65)
            print(evt.get("summary"))
            print("=" * 65)
        elif etype == "error":
            print(f"\n[{timestamp}] ❌ ERROR: {evt.get('message')}")

    orchestrator = TaskOrchestrator(base_url="http://localhost:5000", headless=not visual)
    orchestrator.run_task(prompt=task_prompt, on_event=print_event)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous AI Task Worker")
    parser.add_argument("--cli", action="store_true", help="Run in CLI mode instead of web server")
    parser.add_argument("--task", type=str, default="Find the latest invoice from TechCorp, extract the amount and due date, enter it into our internal system, and tell me once it is done.", help="Natural language task prompt")
    parser.add_argument("--visual", action="store_true", help="Run browser visibly (non-headless)")

    args = parser.parse_args()

    if args.cli:
        run_cli(args.task, visual=args.visual)
    else:
        run_server()
