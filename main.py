"""
Autonomous AI Task Worker - Main Entry Point

  1. Web UI (ApexFlow ERP + Mission Control):
       python main.py

  2. One task from the command line (the ERP is started for you if it is not already running):
       python main.py --cli --task "Which invoices are overdue and what is the total outstanding?"
       python main.py --cli --visual --task "Pay the GlobalInc invoice"

The agent runs on a local Ollama model:  ollama pull qwen2.5:7b   (see README).
"""
import argparse
import os
import sys
import threading
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

try:  # load .env if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT_DIR, ".env"))
except ImportError:
    pass


def port() -> int:
    return int(os.environ.get("PORT", 5000))


def port_in_use() -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port())) == 0


def is_current_server(base: str) -> bool:
    """True only for THIS version of the app (the /api catalogue and agent status exist only in it)."""
    import requests
    try:
        return requests.get(base + "/api/agent/status", timeout=2).json().get("agent") == "tool-calling"
    except Exception:
        return False


def run_server():
    """Starts the ApexFlow ERP and the Mission Control UI."""
    if port_in_use():
        print(f"\n[!] Port {port()} is already in use - an OLD copy of this app is probably still running, and your")
        print("    browser would show that old version. Stop it first:")
        print(f"      Windows:  netstat -ano | findstr :{port()}   then   taskkill /PID <pid> /F")
        print(f"      Mac/Linux: lsof -i :{port()}   then   kill <pid>")
        print("    (or start this one on another port:  PORT=5001 python main.py)")
        sys.exit(1)
    from app.server import app
    from agent.llm import LLMClient
    llm = LLMClient.from_env()
    ready, problem = llm.check()
    print("=" * 65)
    print("[*] AUTONOMOUS AI TASK WORKER")
    print("=" * 65)
    print(f"[-] Mission Control (agent UI):  http://localhost:{port()}/agent")
    print(f"[-] ApexFlow ERP:                http://localhost:{port()}")
    print(f"[-] Local model (Ollama):         {'ready - ' + llm.model if ready else 'NOT READY - ' + problem}")
    print("=" * 65)
    app.run(host="0.0.0.0", port=port(), debug=False)


def ensure_erp_running() -> str:
    """CLI mode talks to the ERP over HTTP, so start it in a background thread when nothing is listening."""
    import requests
    base = f"http://localhost:{port()}"
    if port_in_use():
        if is_current_server(base):
            return base
        print(f"[!] Something on port {port()} is not this version of the app (an old copy may still be running).")
        print(f"    Stop it (Windows: netstat -ano | findstr :{port()} then taskkill /PID <pid> /F) and try again.")
        sys.exit(1)
    from werkzeug.serving import make_server
    from app.server import app
    server = make_server("127.0.0.1", port(), app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)
    print(f"[*] Started the ApexFlow ERP on {base}")
    return base


def run_cli(task_prompt: str, visual: bool = False) -> int:
    from agent.orchestrator import TaskOrchestrator
    base = ensure_erp_running()

    print("=" * 65)
    print(f"[*] Goal: {task_prompt}")
    print("=" * 65)

    def on_event(evt):
        kind, ts = evt.get("type"), evt.get("timestamp", time.strftime("%H:%M:%S"))
        if kind == "thought":
            print(f"\n[{ts}] 🧠 {evt['thought']}")
        elif kind == "plan":
            print(f"\n[{ts}] 📋 PLAN")
            icon = {"done": "✓", "in_progress": "→", "skipped": "-", "pending": " "}
            for s in evt["plan"]:
                print(f"   [{icon.get(s['status'], ' ')}] {s['id']}. {s['description']}")
        elif kind == "tool_call":
            print(f"[{ts}] ⚙️  {evt['tool']} {evt.get('args')}")
        elif kind == "tool_result":
            print(f"[{ts}]    {'⛔' if evt.get('blocked') else ('✓' if evt.get('ok') else '✗')} {evt['observation']}")
        elif kind == "needs_input":
            print(f"\n[{ts}] ❓ {evt['question']}")
            if evt.get("options"):
                print(f"   Options: {', '.join(evt['options'])}")
        elif kind == "completed":
            print("\n" + "=" * 65 + f"\n{evt['summary']}\n" + "=" * 65)
        elif kind == "halted":
            print("\n" + "=" * 65 + f"\n[{ts}] STOPPED ({evt['status']}): {evt['message']}\n" + "=" * 65)
        elif kind == "error":
            print(f"\n[{ts}] ❌ {evt['message']}")

    def ask_user(question):
        if not sys.stdin.isatty():
            return None
        if question.get("kind") == "approval":
            return {"approved": input("   Approve? [y/N]: ").strip().lower() in ("y", "yes")}
        answer = input("   Your answer (blank to cancel): ").strip()
        return {"answer": answer} if answer else None

    result = TaskOrchestrator(base_url=base, headless=not visual).run_task(
        prompt=task_prompt, on_event=on_event, ask_user=ask_user)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous AI Task Worker")
    parser.add_argument("--cli", action="store_true", help="Run one task from the command line")
    parser.add_argument("--task", type=str, help="Natural-language task (required with --cli)")
    parser.add_argument("--visual", action="store_true", help="Show the browser window while the agent works")
    args = parser.parse_args()

    if args.cli:
        if not args.task:
            parser.error("--cli needs --task \"...\"")
        sys.exit(run_cli(args.task, visual=args.visual))
    run_server()
