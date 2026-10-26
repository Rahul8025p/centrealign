# 🤖 Autonomous AI Task Worker

An AI worker that takes a goal in plain language and completes it on a computer, **running entirely on a local model via Ollama** (no cloud, no API key): it chooses its own tools and steps, watches what happens, recovers from failures, asks you when it cannot safely proceed, and **independently verifies** the outcome before it reports success.

| | |
|---|---|
| **Source code** | `<ADD GITHUB REPOSITORY URL HERE>` |
| **Demo video** | `<ADD DEMO VIDEO URL HERE>` |

It is not wired to one task. Try *"Which invoices are overdue and what is the total outstanding?"*, *"Pay the GlobalInc invoice"*, *"Add a note to INV-1044 …"*, *"Has the FinanceFirst invoice been paid?"*, or even *"What is the capital of France?"* — each runs through the same loop with different tool choices.

---

## 1. How it works (architecture)

```
 User goal ──► ┌────────────────────────── Agent loop (orchestrator.py) ──────────────────────────┐
               │  the MODEL sees: goal + working memory + every tool result so far                 │
               │  the MODEL picks the next tool call (or finish)        ◄── local LLM via Ollama   │
               │        │                                                                           │
               │        ▼                                                                           │
               │   Toolbox (tools/toolbox.py)  ── generic tools, no task knowledge ──               │
               │   update_plan · remember · list/read/search_files · erp_request                    │
               │   browser_open/snapshot/fill/select/click · verify_payment · ask_user · finish     │
               │        │ every WRITE (browser submit OR HTTP) passes through                       │
               │        ▼                                                                           │
               │   Guard (guard.py) ── deterministic safety layer ──                                │
               │   allow │ block (with a reason the model can act on) │ ask human approval          │
               │        │ then records what REALLY changed (audit-registry diff)                    │
               │        ▼                                                                           │
               │   ApexFlow ERP (Flask): invoices · billing form · audit log · /api catalogue       │
               └────────────────────────────────────────────────────────────────────────────────────┘
   finish("completed") is only accepted after the Guard independently re-verifies every write.
```

**The split that matters:** the model has *freedom* (what to do, in what order, which route, how to recover); the code has the *final say* on side effects and on whether "done" is true.

| File | Role |
|---|---|
| `agent/orchestrator.py` | The loop: model call → tool call → observe → repeat. Step budget, stuck-detection, context compaction, finish handling. |
| `agent/llm.py` | Ollama client (native `/api/chat` with tool calling): readiness check, retries, and tolerance for small-model quirks (stringified arguments, `<think>` blocks, tool calls written as text). |
| `agent/prompts.py` | System prompt: environment description and operating rules. Contains no vendor/invoice/task names. |
| `agent/tools/toolbox.py` | The generic tools + JSON schemas. Browser tools work from a *page snapshot* (labels, values, selectors), so forms are discovered, not hard-coded. |
| `agent/guard.py` | Safety policies, write recording, independent verification, finish review. |
| `agent/memory.py` | Run state: plan, remembered facts, history, user's own words, approvals, recorded writes, evidence. |
| `agent/tools/{browser,file,api,verifier}_tool.py` | Playwright wrapper, sandboxed file access, ERP client, registry verifier. |
| `app/server.py` | The simulated company ERP, its REST API (self-describing at `GET /api`), the agent endpoints and the SSE stream. |
| `app/templates/agent_control.html` | Mission Control UI: live plan, reasoning/action stream, browser screenshots, approvals, result card. |

### One run, end to end
1. **Goal** arrives (UI or CLI). The model first decides what kind of request it is. If it isn't about the company's systems, it just answers and touches nothing.
2. **Plan**: `update_plan` (shown live in the UI, revised as reality changes).
3. **Act and observe** in a loop: read files / call the API / look at pages / fill forms. Every result goes back to the model, which decides the next step. Facts are kept via `remember` and shown to the model every turn.
4. **Guard** intercepts any write. It may block it (duplicate, already paid, amount/vendor/due-date mismatch, unknown invoice), pause for **human approval**, or allow it. After the write it diffs the audit registry to record what truly happened.
5. **Finish**: the model calls `finish`. A `completed` claim is accepted only if the Guard re-verifies every recorded write and every confirmation ID cited actually exists. Otherwise the model is told why and must correct itself; a second unsupported claim ends the run as `failed`.

### How it recovers from a wrong state
| Situation | What happens |
|---|---|
| Selector wrong / page not as expected | The tool returns the error **plus a fresh page snapshot**; the model re-reads and adjusts. |
| Required field missing / invalid | The snapshot marks fields `required`/`invalid`, so the model sees what to fix. |
| UI route fails | The model can switch to the API. The Guard applies identical checks to both routes. |
| Click worked but the success banner was missed | A retry is recognised via the registry diff: blocked as `already_done_by_this_run`, so **no double payment**. |
| Same failing call repeated | Warned at 3 repeats, run stopped at 5. |
| Model keeps looping | Step budget (default 30) ends the run as `failed`. |
| Flaky page load / network | Tool-level retries (navigation once, HTTP up to 3×); LLM calls retry with backoff on 429/5xx. |
| Guard block (duplicate, mismatch…) | Reported to the model with the right terminal status; the run ends cleanly, nothing is changed. |

---

## 2. Setup and run

**Requirements:** Python 3.10+, [Ollama](https://ollama.com), and enough RAM for the model (about 8 GB for a 7B model).

```bash
# 1) the local model
ollama pull qwen2.5:7b          # any tool-calling model works; see OLLAMA_MODEL below
ollama serve                    # if it is not already running

# 2) the project
git clone <ADD GITHUB REPOSITORY URL HERE>
cd <repo>
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env            # optional - the defaults work with a stock Ollama
```

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama listens. |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Model to use. **Must support tool calling** (e.g. `llama3.1:8b`, `mistral-nemo`, `qwen2.5:14b`). |
| `OLLAMA_NUM_CTX` | `16384` | Context window in tokens. Ollama's own default (~4k) would silently truncate the agent's prompt, so this is always set explicitly. |
| `OLLAMA_TIMEOUT` | `300` | Seconds to wait for one model reply (CPU-only machines can be slow). |
| `OLLAMA_KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded (its own default is 5 minutes, after which the next step pays the load time again). |
| `PORT` | `5000` | Web port. |
| `AGENT_USER_TIMEOUT` | `300` | Seconds to wait for your answer before the run stops safely. |

**Web UI (recommended):** `python main.py` → open http://localhost:5000/agent. A banner tells you if Ollama isn't running or the model isn't pulled. Type any task or click an example.
**Command line:** `python main.py --cli --task "Which invoices are overdue?"` (starts the ERP for you; add `--visual` to watch the browser).
**Reset the demo data:** `POST /api/reset` (clears the audit log).
**Tests:** `python -m unittest tests.test_agent -v` (no Ollama needed; see section 6).

If Ollama isn't running or the model isn't installed, the agent **stops with a clear message** (and the exact `ollama pull` command). It never falls back to a scripted imitation, because the model is the part that makes the decisions.

**Speed.** Every step is one model call, so total time is (number of steps) × (time per call). To see where time goes, run `AGENT_DEBUG=1 python main.py` (each decision prints with its seconds) and `ollama ps` while a task runs: the PROCESSOR column says whether the model is on **GPU** or **CPU**. On CPU a 7B model takes tens of seconds per step. What helps, in order: use a GPU; use a smaller model (`qwen2.5:3b`, with weaker tool use); keep `OLLAMA_NUM_CTX` no larger than you need. The agent already keeps its prompt prefix identical between steps (the changing working memory is appended at the end), so Ollama can reuse its cache instead of re-reading the system prompt and tool list every step, and it asks Ollama to keep the model loaded between steps.

**Using a local model well:** a response takes seconds to a minute on a laptop, so the UI shows "Thinking (step n)…". Bigger models (14B+) follow the tool-calling rules noticeably better than 7B ones; if the agent loops or picks odd tools, try a larger model before changing code.

---

## 3. Demo

**Video / live link:** `<ADD LINK HERE>`

Suggested 3-minute walkthrough (each is a different task through the same loop):
1. *"Which invoices are overdue, and what is the total amount outstanding?"* — read-only analysis, no writes.
2. *"Pay the GlobalInc invoice and tell me once it is confirmed."* — plan → browser form (discovered from the page) → submit → verification card with confirmation ID. Then open `/submissions`.
3. Run #2 again — blocked as a duplicate, nothing re-paid.
4. *"Pay the MegaRetail invoice."* — approval request (amount above the limit); try Reject, then Approve.
5. *"Pay the invoice from Acme Corp."* — no such vendor → the agent asks instead of guessing.
6. *"What is the capital of France?"* — answered directly, no systems touched.

---

## 4. Design decisions

1. **A real tool-calling loop, not a pipeline.** Earlier versions resolved the vendor with regexes and ran a fixed six-step invoice plan, so every prompt was forced into an invoice job. Now the model picks each next action from generic tools; nothing in the prompt, tools or loop mentions a vendor or invoice (a test asserts this).
2. **Freedom for the model, authority for code.** Money-moving actions are safety-critical, so the Guard (not the model) decides whether a write may happen, and it sits on *every* write path. Switching from browser to API can't bypass a rule.
3. **Unknown writes are never silently allowed.** Any write with no policy requires human approval, so new capabilities fail safe.
4. **Grounding.** A payment must be for a vendor/invoice the *user* named (in the task or in an answer to a question); otherwise it needs approval. This replaces the old hard-coded "never substitute a vendor" rule and works for any vendor.
5. **Trust the registry, not the page or the model.** What a write did is learned by diffing the audit registry, so a missed banner, a crash or a lying model can't create a phantom or duplicate payment.
6. **Verification the model can't skip.** At `finish`, the Guard re-verifies every recorded write (payments against intent, vendor, due date and the ERP invoice record; notes by read-back) and rejects confirmation IDs that don't exist.
7. **Discoverable environment.** The ERP publishes `GET /api`; browser tools return page snapshots (labels, values, options, required/invalid, which button submits). The agent learns the system by looking at it.
8. **Human in the loop.** `ask_user` for ambiguity; approvals for risky writes; no answer ⇒ nothing changes.
9. **Simulated ERP.** Lets the agent do real browser and API work against a system whose state can be inspected and reset.

## 5. Models, APIs, frameworks and services

| Component | Use |
|---|---|
| **Ollama** serving a local tool-calling model (default `qwen2.5:7b`, configurable) via its native `/api/chat` API | The agent's decision-making (function calling) |
| **Playwright + Chromium** | Real browser automation and screenshots |
| **Flask** (+ flask-cors) | Simulated ERP, REST API, agent endpoints, SSE stream |
| **requests**, **python-dotenv** | Ollama and ERP clients, verifier; `.env` loading |
| HTML/CSS/JS, Jinja2 | ERP pages and Mission Control UI |
| fpdf2 (optional) | Only for generating PDF sample invoices |

Everything runs on your machine. There are no cloud APIs and no API keys.

## 6. Testing — and what that does and doesn't prove

`tests/test_agent.py` (56 tests) runs the **real** loop, toolbox, guard, verifier and Flask ERP, plus a **real headless Chromium** that discovers and submits the billing form. The model is replaced in two ways, neither of which is a real model:

- **Scripted stand-in** for most scenarios: each turn reads earlier tool results and chooses the next call. This proves the loop, tools, guard, recovery and verification.
- **Fake Ollama server over real HTTP** for the client and a few end-to-end runs: this proves the request/response handling (payload, `num_ctx`, translation of the message history, retries, 404/400 handling, readiness check) and the small-model tolerances (stringified arguments, `<think>` blocks, a tool call written as text, empty replies).

Neither tells you **how well a real local model will do**. That has to be measured by running the agent on your model.

Covered: unrelated question; read-only analysis; a second write type through the same loop; payment by browser and by API; duplicate / already-paid / wrong-amount / document-vs-ERP mismatch blocks and self-correction; approval accept/reject; unnamed payee; unknown vendor clarification; no answer ⇒ no change; click failure → API; missed banner without double payment; fabricated confirmation IDs; stuck and runaway loops; model outage; Ollama not running. Disabling the guard's payment checks or finish-time verification makes 8 and 4 of these tests fail respectively.

## 7. Assumptions
- The payable amount is the invoice's tax-inclusive total (stated in the system prompt and `/api` catalogue). Currency is INR.
- The ERP and its audit registry are the source of truth; the ERP is reachable without login/CAPTCHA/MFA.
- Payments at or above ₹100,000 need human approval (`TaskOrchestrator(approval_threshold=...)`).
- The "user's request" is what the user typed plus their answers; a payee not named there needs approval.
- The local model supports tool calling (Ollama reports an error if it doesn't).
- A user who doesn't answer within `AGENT_USER_TIMEOUT` doesn't want the action.

## 8. Known limitations
- **Not yet run against a real Ollama model in this repo's test environment.** The client follows Ollama's documented API and is tested against a fake Ollama server, but real behaviour (tool-choice quality, prompt adherence, speed) depends on your model and hardware and must be checked by running it. 7B models can pick wrong tools, loop, or ignore instructions; the Guard still stops unsafe writes, but the task may fail or need retrying. Larger models do better.
- Local inference is slow on CPU-only machines (seconds to a minute per step), and the context window is limited (`OLLAMA_NUM_CTX`); long runs rely on compaction of old tool output.
- Safety **policies are written per write type** (payments, notes). Anything else falls back to "ask for approval", which is safe but not smart.
- The grounding check is a name/ID match on the user's text; it can't understand intent beyond that.
- Browser snapshots are DOM-based (no vision); canvas-heavy or heavily dynamic pages would need more.
- One ERP, one currency, single user, local JSON storage, sequential execution, no auth.
- Plain-text invoices only (no PDF/OCR).
- The verifier assumes the ERP's audit log is honest.

## 9. What I would build next
1. Evaluate real local models: a scored task suite (including adversarial prompts) run against 7B / 14B / 32B models, tracking success, steps, time and unsafe-action rate, and tune the prompt per model.
2. Policies as data: declare write types and their rules/verifiers in config so adding a new system doesn't touch the Guard.
3. Vision fallback and richer page understanding (accessibility tree, screenshots to the model).
4. Idempotency keys on the ERP submit endpoint, so a duplicate is impossible even if the registry check races.
5. Persistent run history, replayable traces, and a database instead of JSON files.
6. Parallel / batch tasks with a review queue; real integrations with auth and role-based approvals.
7. Latency controls: streaming model output to the UI, smarter context compaction, and a small model for easy steps.
