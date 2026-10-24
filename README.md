# 🤖 Autonomous AI Task Worker

An autonomous agent prototype capable of taking high-level natural language instructions and completing computer-based enterprise workflows without step-by-step guidance.

Built for the **AI Engineering Intern** evaluation challenge.

---

## 🌟 Key Highlights & Capabilities

- **Autonomous Planning & ReAct Execution**: Decomposes natural language goals into a structured action sequence (`Search` → `Extract` → `Navigate` → `Populate` → `Submit` → `Verify` → `Report`).
- **Real Browser Automation & Computer Use**: Controls Chromium via Playwright to navigate the internal company portal, fill out financial forms, click buttons, and capture visual proof of completion.
- **Multi-Source Data Discovery**: Searches both raw local files (`data/invoices/*.txt`) and internal ERP databases/APIs to locate invoices and extract structured entities (amounts, due dates, categories).
- **Independent Verification Mechanism**: Before reporting success, the agent queries the audit registry and independently asserts that the record exists, figures match, and an immutable confirmation token (`APX-CONF-...`) was issued.
- **Self-Healing Retries & Fault Tolerance**: Automatically retries actions on DOM/network glitches and falls back to programmatic API bridges if the browser layer encounters unexpected element states.
- **Interactive Mission Control UI**: Real-time reasoning stream (Thoughts, Tool Actions, Observations), live browser screenshots, and evidence cards.
- **Dual LLM Operation**: Supports live OpenAI API (`GPT-4o` / `GPT-4o-mini`) while including a deterministic, zero-config Autonomous Reasoner so the prototype works immediately out-of-the-box without requiring an API key.

---

## 🏛️ Architecture Overview

```
                        [ User Task / Prompt ]
                                  │
                                  ▼
                     ┌──────────────────────────┐
                     │     LLM / Task Planner   │
                     │  (Goal Decomposition)    │
                     └────────────┬─────────────┘
                                  │
                                  ▼
     ┌────────────────────────────────────────────────────────┐
     │                ReAct Execution Loop                    │
     │                                                        │
     │   [Thought] ──► [Select Tool] ──► [Execute Action]     │
     │       ▲                                   │            │
     │       │                                   ▼            │
     │   [Reflect] ◄── [Detect Error/Retry] ◄── [Observation] │
     └──────────────┬────────────────────────────┬────────────┘
                    │                            │
       ┌────────────▼───────────┐   ┌────────────▼───────────┐
       │   Discovered Entities  │   │     Action Tools       │
       │  • Invoice ID          │   │  • Browser Tool        │
       │  • Vendor Name         │   │  • File Search Tool    │
       │  • Payable Amount      │   │  • Internal REST API   │
       │  • Due Date            │   │  • Screenshot Capturer │
       └────────────────────────┘   └────────────┬───────────┘
                                                 │
                                                 ▼
                                    ┌────────────────────────┐
                                    │ Independent Verifier   │
                                    │ (Confirms Registry DB) │
                                    └────────────┬───────────┘
                                                 │
                                                 ▼
                                    ┌────────────────────────┐
                                    │ Summary Report & Proof │
                                    └────────────────────────┘
```

---

## 📂 Repository Structure

```
AI Task Worker/
├── app/                            # Simulated Enterprise ERP ("ApexFlow")
│   ├── server.py                   # Flask backend with ERP endpoints & Agent SSE streaming
│   ├── static/
│   │   ├── css/style.css           # Modern dark-mode glassmorphism design system
│   │   └── screenshots/            # Automated browser action captures
│   ├── templates/
│   │   ├── base.html               # Shared navigation & layout
│   │   ├── dashboard.html          # Corporate finance dashboard
│   │   ├── invoices.html           # Vendor invoice directory & search
│   │   ├── invoice_detail.html     # Statement of work breakdown
│   │   ├── billing_portal.html     # Web form for payment disbursement
│   │   ├── submissions.html        # Immutable audit log
│   │   └── agent_control.html      # Mission Control UI for live agent execution
│   └── data/
│       ├── invoices.json           # Sample vendor invoice database
│       └── submissions.json        # Processed disbursals audit record
│
├── agent/                          # The Autonomous AI Agent Core
│   ├── orchestrator.py             # ReAct execution loop & lifecycle coordinator
│   ├── planner.py                  # High-level task planner
│   ├── llm.py                      # LLM interface (OpenAI + Intelligent Reasoner)
│   ├── memory.py                   # Working memory, scratchpad, & entities store
│   └── tools/
│       ├── browser_tool.py         # Playwright computer use & DOM automation
│       ├── file_tool.py            # Local invoice document search & regex parser
│       ├── api_tool.py             # Fallback REST API client
│       └── verifier_tool.py        # Cryptographic outcome verification engine
│
├── data/
│   └── invoices/                   # Local raw invoice documents (.txt)
│
├── generate_sample_invoices.py     # Invoice generator utility
├── requirements.txt                # Project dependencies
├── .env.example                    # Environment template
├── main.py                         # Unified entry point (Web & CLI modes)
└── README.md                       # Documentation
```

---

## ⚡ Quick Start & Setup

### 1. Prerequisites
- **Python 3.10+**
- (Optional) OpenAI API Key in `.env` if you wish to run with live GPT-4o. If omitted, the system automatically uses its embedded autonomous reasoning engine.

### 2. Installation

Clone or open the directory in terminal, then install dependencies:

```bash
cd "AI Task Worker"
pip install -r requirements.txt
```

Install Playwright browser binaries:
```bash
playwright install chromium
```

*(Optional)* Configure API key:
```bash
cp .env.example .env
# Open .env and insert your OPENAI_API_KEY if desired
```

---

## 🚀 Running the Prototype

### Mode 1: Interactive Mission Control (Recommended)

Start the combined web application and agent orchestrator:
```bash
python main.py
```

Then open your browser:
- **🤖 Agent Mission Control**: [http://localhost:5000/agent](http://localhost:5000/agent)
- **📊 ApexFlow ERP Dashboard**: [http://localhost:5000](http://localhost:5000)
- **📑 Invoices Directory**: [http://localhost:5000/invoices](http://localhost:5000/invoices)
- **💳 Billing Portal**: [http://localhost:5000/billing](http://localhost:5000/billing)
- **✅ Audit Log**: [http://localhost:5000/submissions](http://localhost:5000/submissions)

Click **"🚀 Run Autonomous Worker"** to watch the agent:
1. Decompose the goal into steps.
2. Search and locate the invoice in `data/invoices/`.
3. Extract amount (`₹84,500`) and due date (`2024-11-15`).
4. Launch the browser and navigate to the billing portal.
5. Populate form inputs and click submit.
6. Verify against the audit registry.
7. Display the confirmation badge and evidence.

---

### Mode 2: Headless CLI Mode

Run any natural language task directly from your command line:

```bash
python main.py --cli --task "Find the latest invoice from TechCorp, extract the amount and due date, enter it into our internal system, and tell me once it is done."
```

For visual browser execution (watch Chromium open on your screen):
```bash
python main.py --cli --visual --task "Find the latest invoice from TechCorp, extract the amount and due date, enter it into our internal system, and tell me once it is done."
```

---

## 🎯 Evaluation Criteria Breakdown

| Evaluation Dimension | How This Prototype Satisfies It |
|---|---|
| **Autonomy** | User only specifies the high-level intent. The agent plans, picks tools, passes data between steps, and decides the next action based on previous observations. |
| **Execution** | Performs real computer actions: reads files from disk, launches a browser, navigates URLs, enters text into input fields, clicks submit, and extracts confirmation tokens. |
| **Reliability & Retries** | Detects missing elements or network hiccups. Implements multi-retry loops and an automatic fallback path from browser DOM failure to programmatic REST bridges. |
| **Verification** | Employs an independent `verifier_tool` that verifies the transaction in the database, cross-checks amounts, and requires an authenticated confirmation token before concluding. |
| **Generalization** | The core agent loop and tool abstraction (`file_tool`, `browser_tool`, `api_tool`, `verifier_tool`) are modular and work seamlessly with other queries (e.g., TechCorp, GlobalInc, StartupXYZ). |
| **Product Thinking** | Focuses on end-user trust: provides real-time reasoning transparency, screenshot evidence, audit trails, and a one-click reset for continuous testing. |

---

## 🔍 Technical Decisions & Architecture Rationale

1. **Why Playwright?**  
   Modern web applications rely on reactive DOM updates. Playwright offers native `networkidle` waiting, robust selector locators, and reliable headless/headed execution across platforms.

2. **Why an Independent Verification Step?**  
   Language models can easily hallucinate task completion based purely on clicking a button. Independent verification queries the underlying system state to guarantee that the intended side effect actually transpired.

3. **Dual Execution Engine (LLM + Fallback)**:  
   Ensures that evaluators and collaborators can immediately clone and test the repository without needing an active OpenAI API subscription or running into rate limits.

---

## 🚧 Known Limitations & Future Scope

- **Desktop GUI Computer Use**: Currently operates on web browser and filesystem layers. Future iterations could integrate OS-level computer use (e.g., PyAutoGUI or Anthropic Computer Use API) to automate native desktop legacy software.
- **Complex Captchas / 2FA**: The current prototype assumes enterprise single sign-on or intranet access without multi-factor biometric challenges.
- **Long-Horizon Parallel Tasks**: Currently executes steps sequentially; a graph-based runner (e.g. LangGraph) could execute multi-branch sub-tasks concurrently.
