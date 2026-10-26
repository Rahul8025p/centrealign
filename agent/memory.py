import time


class AgentMemory:
    """
    Everything the agent knows about ONE run:

    - plan / history        what it intends to do and what it did (shown in the UI)
    - discovered_entities   facts the model chose to remember (invoice id, amount, ...)
    - user_texts            the user's own words (task + answers to questions); used by the
                            guard to check that a payment is actually what the user asked for
    - docs_seen             invoice facts parsed from documents the agent read (cross-checked by the guard)
    - approvals             human approvals / rejections, per invoice
    - writes                every side effect this run caused, as seen by the system (not claimed by the model)
    - evidence              confirmation id, verification result, screenshots, ...
    """

    def __init__(self, task_id: str, prompt: str):
        self.task_id = task_id
        self.prompt = prompt
        self.created_at = time.time()
        self.plan = []                 # [{"id": 1, "description": "...", "status": "pending|in_progress|done|skipped"}]
        self.discovered_entities = {}
        self.history = []              # one record per tool call
        self.evidence = {}
        self.is_completed = False
        # running | completed | failed | needs_input | declined | already_submitted | already_paid | discrepancy
        self.status = "running"
        self.needs_human = False
        self.human_question = None
        self.human_response = None

        # guard-related run state
        self.user_texts = [prompt]
        self.known_submission_ids = set()
        self.docs_seen = {}
        self.approvals = {}
        self.writes = []
        self.reviewed = {}            # invoice_id -> values the guard reviewed (the agent's intent)
        self.baseline_ok = False
        self.finish_rejections = 0

    # ---- plan -------------------------------------------------------------
    def set_plan(self, steps):
        """Replace the plan. `steps` is a list of strings or {"text"/"description", "status"} dicts."""
        plan = []
        for i, s in enumerate(steps or [], start=1):
            if isinstance(s, str):
                text, status = s, "pending"
            else:
                text = s.get("text") or s.get("description") or ""
                status = s.get("status", "pending")
            if status not in ("pending", "in_progress", "done", "skipped"):
                status = "pending"
            if text.strip():
                plan.append({"id": i, "description": text.strip(), "status": status})
        self.plan = plan
        return plan

    # ---- trace ------------------------------------------------------------
    def record_step(self, thought: str, action: str, tool_name: str, observation: str,
                    status: str = "success", screenshot: str = None):
        record = {
            "step_number": len(self.history) + 1,
            "timestamp": time.strftime("%H:%M:%S"),
            "thought": thought,
            "action": action,
            "tool": tool_name,
            "observation": observation,
            "status": status,
            "screenshot": screenshot,
        }
        self.history.append(record)
        return record

    # ---- facts ------------------------------------------------------------
    def set_entity(self, key: str, value):
        self.discovered_entities[key] = value

    def get_entity(self, key: str, default=None):
        return self.discovered_entities.get(key, default)

    def record_evidence(self, key: str, value):
        self.evidence[key] = value

    def working_memory_text(self) -> str:
        """Compact text block that is shown to the model on every turn."""
        lines = []
        if self.plan:
            lines.append("Plan:")
            icon = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "skipped": "[-]"}
            lines += [f"  {icon.get(p['status'], '[ ]')} {p['id']}. {p['description']}" for p in self.plan]
        if self.discovered_entities:
            lines.append("Remembered facts:")
            lines += [f"  {k}: {v}" for k, v in self.discovered_entities.items()]
        if self.writes:
            lines.append("Changes made so far in this run (recorded by the system):")
            for w in self.writes:
                lines.append("  - " + ", ".join(f"{k}={v}" for k, v in w.items() if v not in (None, "")))
        return "\n".join(lines)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "prompt": self.prompt,
            "plan": self.plan,
            "discovered_entities": self.discovered_entities,
            "history": self.history,
            "evidence": self.evidence,
            "writes": self.writes,
            "is_completed": self.is_completed,
            "status": self.status,
            "needs_human": self.needs_human,
            "human_question": self.human_question,
        }
