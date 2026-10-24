import requests

class ApiTool:
    """
    Direct REST API tool for interacting with the internal company system (ApexFlow ERP).
    Provides rapid querying and fallback capabilities when browser automation encounters
    DOM or rendering constraints.
    """
    def __init__(self, base_url: str = "http://localhost:5000"):
        self.base_url = base_url.rstrip("/")

    def get_invoices(self, query: str = None, status: str = None):
        """Fetch invoices with optional filtering."""
        try:
            params = {}
            if query:
                params["query"] = query
            if status:
                params["status"] = status
            res = requests.get(f"{self.base_url}/api/invoices", params=params, timeout=5)
            return res.json()
        except Exception as e:
            return {"success": False, "error": f"API request failed: {str(e)}"}

    def get_invoice_details(self, invoice_id: str):
        """Fetch full details for an invoice by ID."""
        try:
            res = requests.get(f"{self.base_url}/api/invoices/{invoice_id}", timeout=5)
            return res.json()
        except Exception as e:
            return {"success": False, "error": f"API request failed: {str(e)}"}

    def submit_billing(self, invoice_id: str, company: str, amount: float, due_date: str, category: str = "General Operations", notes: str = None):
        """Submit an invoice for payment via API."""
        try:
            payload = {
                "invoice_id": invoice_id,
                "company": company,
                "amount": amount,
                "due_date": due_date,
                "category": category,
                "notes": notes or "Automated clearance via API Tool"
            }
            res = requests.post(f"{self.base_url}/api/billing/submit", json=payload, timeout=5)
            return res.json()
        except Exception as e:
            return {"success": False, "error": f"API request failed: {str(e)}"}

    def get_submissions(self):
        """Retrieve audit log of submitted invoices."""
        try:
            res = requests.get(f"{self.base_url}/api/submissions", timeout=5)
            return res.json()
        except Exception as e:
            return {"success": False, "error": f"API request failed: {str(e)}"}
