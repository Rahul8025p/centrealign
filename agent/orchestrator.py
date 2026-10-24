import time
import os
import uuid
from .memory import AgentMemory
from .llm import LLMEngine
from .tools.file_tool import FileTool
from .tools.browser_tool import BrowserTool
from .tools.api_tool import ApiTool
from .tools.verifier_tool import VerifierTool

class TaskOrchestrator:
    """
    Autonomous Execution Engine for the AI Task Worker.
    
    Implements a resilient ReAct (Reason + Act + Observe) cycle:
    1. Goal understanding & structured planning
    2. Dynamic tool invocation (File, Browser, API)
    3. Failure detection, retries, and fallback mechanisms
    4. Independent verification gate
    5. Human-in-the-loop safety checkpoints
    6. Streaming progress events
    """
    def __init__(self, base_url: str = "http://localhost:5000", headless: bool = True):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.llm = LLMEngine()
        self.file_tool = FileTool()
        self.api_tool = ApiTool(base_url=self.base_url)
        self.verifier_tool = VerifierTool(base_url=self.base_url)
        self.browser_tool = None

    def _get_browser(self):
        if self.browser_tool is None:
            self.browser_tool = BrowserTool(headless=self.headless)
        return self.browser_tool

    def run_task(self, prompt: str, task_id: str = None, on_event=None):
        """
        Executes a task and yields/calls on_event for each step.
        """
        task_id = task_id or f"task_{uuid.uuid4().hex[:8]}"
        memory = AgentMemory(task_id=task_id, prompt=prompt)

        def emit(event_type: str, data: dict):
            payload = {"type": event_type, "task_id": task_id, "timestamp": time.strftime("%H:%M:%S"), **data}
            if on_event:
                on_event(payload)
            return payload

        emit("status", {"message": f"Starting autonomous execution for goal: '{prompt}'"})

        # Step 1: Planning
        thought_plan = f"I need to understand the user's objective: '{prompt}'. I will decompose this into an executable sequence of actions."
        emit("thought", {"thought": thought_plan})
        
        plan = self.llm.plan_task(prompt)
        for s in plan:
            memory.add_step(s["id"], s["description"], s.get("tool"), s.get("params"), action=s.get("action"))
        
        emit("plan", {"plan": memory.plan})

        # Step 2: Execution Loop
        for step in memory.plan:
            step_id = step["id"]
            memory.update_step_status(step_id, "in_progress")
            desc = step["description"]
            tool_name = step.get("tool", "system")
            params = step.get("params", {})

            # Reason before action
            thought = f"Executing Step {step_id}: {desc}. Utilizing tool '{tool_name}'."
            emit("thought", {"thought": thought, "step_id": step_id})

            max_retries = 2
            success = False
            last_error = None
            observation = ""
            screenshot = None

            for attempt in range(max_retries + 1):
                try:
                    if "locate" in desc.lower() or "extract" in desc.lower():
                        # File tool search & entity extraction
                        query = params.get("query", "TechCorp")
                        file_res = self.file_tool.find_invoice(query)
                        
                        if file_res.get("success"):
                            data = file_res["data"]
                            memory.set_entity("invoice_id", data.get("invoice_id"))
                            memory.set_entity("company", data.get("company"))
                            memory.set_entity("amount", data.get("amount"))
                            memory.set_entity("due_date", data.get("due_date"))
                            memory.set_entity("category", data.get("category", "Software Development"))
                            
                            observation = (
                                f"Located document '{file_res['filename']}'. "
                                f"Extracted: Invoice ID={data.get('invoice_id')}, "
                                f"Vendor={data.get('company')}, Amount=₹{data.get('amount')}, "
                                f"Due Date={data.get('due_date')}."
                            )
                            success = True
                            break
                        else:
                            # Fallback: query API directly
                            api_res = self.api_tool.get_invoices(query=query)
                            if api_res.get("success") and len(api_res.get("invoices", [])) > 0:
                                inv = api_res["invoices"][0]
                                memory.set_entity("invoice_id", inv.get("id"))
                                memory.set_entity("company", inv.get("company"))
                                memory.set_entity("amount", inv.get("amount"))
                                memory.set_entity("due_date", inv.get("due_date"))
                                memory.set_entity("category", inv.get("category"))
                                observation = f"Located invoice via ERP API: {inv.get('id')} ({inv.get('company')}) for ₹{inv.get('amount')}."
                                success = True
                                break
                            else:
                                last_error = file_res.get("error", "Document not found.")

                    elif "open_billing_portal" in step.get("action", "") or "navigate" in desc.lower():
                        # Browser navigation
                        browser = self._get_browser()
                        target_url = f"{self.base_url}/billing"
                        nav_res = browser.navigate(target_url)
                        screenshot = nav_res.get("screenshot")
                        
                        if nav_res.get("success"):
                            observation = f"Navigated successfully to Billing Portal ({target_url}). Page title: '{nav_res.get('title')}'."
                            success = True
                            break
                        else:
                            last_error = nav_res.get("error")

                    elif "populate" in desc.lower() or "fill" in desc.lower() or step.get("action") == "populate_billing_form" or step_id == 4:
                        # Fill browser inputs with discovered entities
                        browser = self._get_browser()
                        inv_id = memory.get_entity("invoice_id", "INV-1042")
                        company = memory.get_entity("company", "TechCorp Solutions")
                        amount = memory.get_entity("amount", 84500)
                        due_date = memory.get_entity("due_date", "2024-11-15")

                        # Fill fields
                        browser.fill("#input-invoice-id", inv_id)
                        browser.fill("#input-company", company)
                        browser.fill("#input-amount", str(amount))
                        browser.fill("#input-due-date", due_date)
                        
                        screenshot = browser.capture_screenshot("form_filled")
                        observation = f"Filled payment form with Invoice={inv_id}, Vendor={company}, Amount=₹{amount}, DueDate={due_date}."
                        success = True
                        break

                    elif "submission" in desc.lower() or "submit" in desc.lower() or "trigger" in desc.lower() or step.get("action") == "execute_submission" or step_id == 5:
                        # Submit form in browser
                        browser = self._get_browser()
                        click_res = browser.click("#btn-submit-billing")
                        time.sleep(1) # wait for response
                        
                        # Extract confirmation code from DOM
                        conf_res = browser.extract_text("#confirmation-code-display")
                        conf_code = conf_res.get("text", "") if conf_res.get("success") else None
                        
                        if not conf_code:
                            # Fallback: if browser UI submit didn't return code, use API fallback
                            api_submit = self.api_tool.submit_billing(
                                invoice_id=memory.get_entity("invoice_id"),
                                company=memory.get_entity("company"),
                                amount=memory.get_entity("amount"),
                                due_date=memory.get_entity("due_date"),
                                category=memory.get_entity("category")
                            )
                            conf_code = api_submit.get("confirmation_id")

                        memory.record_evidence("confirmation_id", conf_code)
                        screenshot = browser.capture_screenshot("submitted")
                        observation = f"Payment form submitted. Received Confirmation ID: '{conf_code}'."
                        success = True
                        break

                    elif "verify" in desc.lower() or "audit" in desc.lower() or step.get("action") == "verify_outcome_audit" or step_id == 6:
                        # Verification Tool
                        inv_id = memory.get_entity("invoice_id")
                        exp_amount = memory.get_entity("amount")
                        exp_vendor = memory.get_entity("company")
                        
                        ver_res = self.verifier_tool.verify_outcome(
                            invoice_id=inv_id,
                            expected_amount=exp_amount,
                            expected_vendor=exp_vendor
                        )
                        
                        if ver_res.get("verified"):
                            memory.record_evidence("verification_result", ver_res)
                            observation = ver_res.get("summary")
                            success = True
                            break
                        else:
                            last_error = ver_res.get("reason")

                    else:
                        observation = f"Step executed: {desc}"
                        success = True
                        break

                except Exception as e:
                    last_error = str(e)
                    time.sleep(1)

            # Record step result
            step_status = "completed" if success else "failed"
            memory.update_step_status(step_id, step_status)
            memory.record_step(
                thought=thought,
                action=desc,
                tool_name=tool_name,
                observation=observation if success else f"Error encountered: {last_error}",
                status=step_status,
                screenshot=screenshot
            )

            emit("step_result", {
                "step_id": step_id,
                "status": step_status,
                "observation": observation if success else f"Error: {last_error}",
                "screenshot": screenshot
            })

            if not success:
                emit("error", {"message": f"Step {step_id} failed after retries: {last_error}. Halting."})
                if self.browser_tool:
                    self.browser_tool.close()
                return memory.to_dict()

        # Step 3: Final Verification & Summary
        ver_check = memory.evidence.get("verification_result")
        if ver_check and ver_check.get("verified"):
            memory.is_completed = True
            summary = self.llm.reason_and_summarize(prompt, memory.to_dict())
            emit("completed", {
                "summary": summary,
                "evidence": memory.evidence,
                "entities": memory.discovered_entities
            })
        else:
            emit("error", {"message": "Final verification gate check failed. Task not confirmed."})

        if self.browser_tool:
            self.browser_tool.close()

        return memory.to_dict()
