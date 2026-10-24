import time
import json

class AgentMemory:
    """
    Maintains the working memory, discovered entities, plan state,
    and execution trace for an autonomous task run.
    """
    def __init__(self, task_id: str, prompt: str):
        self.task_id = task_id
        self.prompt = prompt
        self.created_at = time.time()
        self.plan = []  # List of step dicts: {"id": 1, "description": "...", "status": "pending"}
        self.discovered_entities = {}  # e.g., {"invoice_id": "...", "amount": 84500, ...}
        self.history = []  # List of {"step": n, "thought": "...", "action": "...", "observation": "...", "status": "..."}
        self.evidence = {}  # e.g. {"confirmation_id": "...", "screenshot": "...", "verification_result": ...}
        self.current_step_index = 0
        self.retries = {}  # step_id -> count
        self.is_completed = False
        self.needs_human = False
        self.human_question = None
        self.human_response = None

    def add_step(self, step_id: int, description: str, tool: str = None, params: dict = None, action: str = None):
        self.plan.append({
            "id": step_id,
            "description": description,
            "action": action,
            "tool": tool,
            "params": params or {},
            "status": "pending"
        })

    def update_step_status(self, step_id: int, status: str):
        for s in self.plan:
            if s["id"] == step_id:
                s["status"] = status
                break

    def record_step(self, thought: str, action: str, tool_name: str, observation: str, status: str = "success", screenshot: str = None):
        record = {
            "step_number": len(self.history) + 1,
            "timestamp": time.strftime("%H:%M:%S"),
            "thought": thought,
            "action": action,
            "tool": tool_name,
            "observation": observation,
            "status": status,
            "screenshot": screenshot
        }
        self.history.append(record)
        return record

    def set_entity(self, key: str, value):
        self.discovered_entities[key] = value

    def get_entity(self, key: str, default=None):
        return self.discovered_entities.get(key, default)

    def record_evidence(self, key: str, value):
        self.evidence[key] = value

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "prompt": self.prompt,
            "plan": self.plan,
            "discovered_entities": self.discovered_entities,
            "history": self.history,
            "evidence": self.evidence,
            "is_completed": self.is_completed,
            "needs_human": self.needs_human,
            "human_question": self.human_question
        }
