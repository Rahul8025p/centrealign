import os
import re
import json

class LLMEngine:
    """
    Handles LLM reasoning, goal interpretation, and tool-call decisions.
    
    Operates in two modes:
    1. Active API Mode: Uses OpenAI API (e.g., GPT-4o) when OPENAI_API_KEY is configured.
    2. Autonomous Reasoner Mode: Embedded deterministic agent reasoning engine that
       interprets tasks, handles edge cases, and executes plans without requiring an external API key.
    """
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o")
        self.client = None

        if self.api_key and not self.api_key.startswith("sk-your-openai"):
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=self.api_key)
                self.has_api = True
            except Exception as e:
                print(f"[LLM] OpenAI client init notice: {e}")
                self.has_api = False
        else:
            self.has_api = False

    def is_api_active(self) -> bool:
        return self.has_api and self.client is not None

    def plan_task(self, prompt: str) -> list:
        """
        Decomposes a user task prompt into an actionable sequence of tool steps.
        """
        if self.is_api_active():
            try:
                system_prompt = """You are an Autonomous AI Task Worker. Given a user's operational goal, decompose it into a JSON array of sequential steps.
Each step must have:
- "id": int
- "action": short name (e.g., "search_invoice", "extract_data", "navigate_portal", "fill_billing_form", "submit_form", "verify_outcome")
- "description": human-readable plan description
- "tool": one of ["file_tool", "browser_tool", "api_tool", "verifier_tool"]
- "params": object with relevant parameters (such as query, url, fields)

Only output the raw JSON array, with no other text."""
                
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Decompose this task: {prompt}"}
                    ],
                    temperature=0.1
                )
                content = response.choices[0].message.content.strip()
                # Clean markdown blocks if present
                content = re.sub(r"^```json\s*", "", content)
                content = re.sub(r"^```\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
                plan = json.loads(content)
                if isinstance(plan, list) and len(plan) > 0:
                    return plan
            except Exception as e:
                print(f"[LLM] API call failed, switching to Autonomous Reasoner: {e}")

        # Autonomous Reasoner Fallback
        return self._rule_based_planner(prompt)

    def _rule_based_planner(self, prompt: str) -> list:
        """Intelligent, dynamic decomposition engine for enterprise tasks."""
        p_lower = prompt.lower()
        
        # 1. Extract vendor name or invoice ID mentioned in prompt
        vendor = "TechCorp"
        if "globalinc" in p_lower:
            vendor = "GlobalInc"
        elif "startupxyz" in p_lower:
            vendor = "StartupXYZ"
        elif "megaretail" in p_lower:
            vendor = "MegaRetail"
        elif "financefirst" in p_lower:
            vendor = "FinanceFirst"
        elif "techcorp" in p_lower:
            vendor = "TechCorp"
            
        # Detect invoice ID if present
        inv_match = re.search(r"INV-\d+", prompt, re.IGNORECASE)
        inv_id = inv_match.group(0).upper() if inv_match else None
        target_query = inv_id if inv_id else vendor

        # 2. Classify User Intent:
        # Check if the user explicitly wants action taken in the billing system
        submit_keywords = ["submit", "enter", "pay", "payment", "disburse", "fill", "portal", "system", "process payment"]
        has_submit_intent = any(k in p_lower for k in submit_keywords)

        # Check if user only wants to check verification or status
        status_keywords = ["status", "is paid", "has been submitted", "verify status", "check status", "audit record"]
        has_status_intent = any(k in p_lower for k in status_keywords) and not has_submit_intent

        # CASE A: Status Check Only
        if has_status_intent:
            return [
                {
                    "id": 1,
                    "action": "verify_outcome_audit",
                    "description": f"Query audit registry and verify current payment status for '{target_query}'.",
                    "tool": "verifier_tool",
                    "params": {"query": target_query}
                }
            ]

        # CASE B: Search / Extract Only (User did NOT ask to submit or enter into system)
        if not has_submit_intent:
            return [
                {
                    "id": 1,
                    "action": "locate_invoice_document",
                    "description": f"Locate and inspect invoice document for '{target_query}' across files and ERP repository.",
                    "tool": "file_tool",
                    "params": {"query": target_query}
                },
                {
                    "id": 2,
                    "action": "extract_billing_entities",
                    "description": "Extract critical invoice details: Invoice ID, Vendor, Amount, Due Date, and Category.",
                    "tool": "file_tool",
                    "params": {"query": target_query}
                }
            ]

        # CASE C: Full Workflow (Locate -> Extract -> Navigate -> Fill -> Submit -> Verify)
        return [
            {
                "id": 1,
                "action": "locate_invoice_document",
                "description": f"Locate and inspect invoice records for '{target_query}' across file and portal repositories.",
                "tool": "file_tool",
                "params": {"query": target_query}
            },
            {
                "id": 2,
                "action": "extract_billing_entities",
                "description": "Extract critical billing metadata: Invoice ID, Vendor, Payable Amount, Due Date, and Category.",
                "tool": "file_tool",
                "params": {"query": target_query}
            },
            {
                "id": 3,
                "action": "open_billing_portal",
                "description": "Launch browser and navigate to internal company billing & disbursal system.",
                "tool": "browser_tool",
                "params": {"url": "http://localhost:5000/billing"}
            },
            {
                "id": 4,
                "action": "populate_billing_form",
                "description": "Fill internal payment form inputs with verified invoice figures.",
                "tool": "browser_tool",
                "params": {}
            },
            {
                "id": 5,
                "action": "execute_submission",
                "description": "Trigger submission button and capture confirmation token.",
                "tool": "browser_tool",
                "params": {"selector": "#btn-submit-billing"}
            },
            {
                "id": 6,
                "action": "verify_outcome_audit",
                "description": "Independently verify transaction status in audit registry and validate confirmation hash.",
                "tool": "verifier_tool",
                "params": {}
            }
        ]

    def reason_and_summarize(self, task_goal: str, memory_dict: dict) -> str:
        """Generates a concise completion report adapted to the task goal."""
        entities = memory_dict.get("discovered_entities", {})
        evidence = memory_dict.get("evidence", {})
        conf_id = evidence.get("confirmation_id")
        amount = entities.get("amount", "N/A")
        due_date = entities.get("due_date", "N/A")
        vendor = entities.get("company", "Vendor")
        inv_id = entities.get("invoice_id", "Invoice")
        category = entities.get("category", "General")

        try:
            amt_formatted = f"₹{float(amount):,}" if amount != "N/A" else "N/A"
        except (ValueError, TypeError):
            amt_formatted = f"₹{amount}"

        # If no submission was performed (Search / Inquiry task)
        if not conf_id:
            return (
                f"🔍 **Invoice Information Retrieved**\n\n"
                f"• **Vendor / Payee:** {vendor}\n"
                f"• **Invoice Reference:** `{inv_id}`\n"
                f"• **Billed Amount:** {amt_formatted} (Due: {due_date})\n"
                f"• **Category:** {category}\n"
                f"• **Action Status:** Located and verified from source document (no submission requested).\n"
            )

        # Full Submission Task
        summary = (
            f"🎯 **Task Completed Successfully**\n\n"
            f"• **Vendor / Payee:** {vendor}\n"
            f"• **Invoice Reference:** `{inv_id}`\n"
            f"• **Total Disbursed:** {amt_formatted} (Due: {due_date})\n"
            f"• **Audit Confirmation ID:** `{conf_id}`\n"
            f"• **Verification:** Validated against internal ERP audit registry.\n"
        )
        return summary
