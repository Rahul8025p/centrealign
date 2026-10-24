import os
import re
import json

class FileTool:
    """
    Tool for searching, reading, and parsing local invoice documents
    stored in data/invoices/ or app/data/invoices.json.
    """
    def __init__(self, base_dir: str = None):
        if base_dir is None:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        else:
            self.base_dir = base_dir
        self.invoices_dir = os.path.join(self.base_dir, "data", "invoices")

    def list_files(self):
        """Returns list of all invoice documents in the folder."""
        if not os.path.exists(self.invoices_dir):
            return []
        return [f for f in os.listdir(self.invoices_dir) if os.path.isfile(os.path.join(self.invoices_dir, f))]

    def find_invoice(self, query: str):
        """
        Searches local invoice files for a given query (e.g., 'TechCorp', 'INV-1042').
        Returns matched file details and parsed content.
        """
        query_norm = query.lower().strip()
        files = self.list_files()
        matched_file = None

        # Check by filename first
        for f in files:
            if query_norm in f.lower().replace("_", " "):
                matched_file = f
                break

        # If not found by name, scan content of each file
        if not matched_file:
            for f in files:
                content = self.read_file(f)
                if query_norm in content.lower():
                    matched_file = f
                    break

        if not matched_file:
            return {
                "success": False,
                "error": f"No invoice matching '{query}' found in local invoices repository ({self.invoices_dir}).",
                "available_files": files
            }

        parsed_data = self.parse_invoice_file(matched_file)
        return {
            "success": True,
            "filename": matched_file,
            "filepath": os.path.join(self.invoices_dir, matched_file),
            "data": parsed_data
        }

    def read_file(self, filename: str) -> str:
        """Reads raw text content of an invoice file."""
        filepath = os.path.join(self.invoices_dir, filename)
        if not os.path.exists(filepath):
            return ""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"Error reading file: {str(e)}"

    def parse_invoice_file(self, filename: str) -> dict:
        """
        Extracts structured entities from the text invoice:
        - invoice_id
        - company / vendor
        - amount
        - due_date
        - category
        - description
        """
        raw_text = self.read_file(filename)
        data = {
            "raw_text": raw_text,
            "invoice_id": None,
            "company": None,
            "amount": None,
            "due_date": None,
            "category": None,
            "status": None
        }

        # Regex extractors
        id_match = re.search(r"INVOICE:\s*([A-Za-z0-9\-]+)", raw_text)
        if id_match:
            data["invoice_id"] = id_match.group(1).strip()

        from_match = re.search(r"FROM:\s*([^\n]+)", raw_text)
        if from_match:
            data["company"] = from_match.group(1).strip()

        due_match = re.search(r"DUE DATE:\s*([0-9\-]+)", raw_text)
        if due_match:
            data["due_date"] = due_match.group(1).strip()

        # Look for subtotal or total amount
        amount_match = re.search(r"SUBTOTAL:\s*[A-Z]{3}\s*([0-9,]+(?:\.[0-9]+)?)", raw_text)
        if not amount_match:
            amount_match = re.search(r"TOTAL DUE:\s*[A-Z]{3}\s*([0-9,]+(?:\.[0-9]+)?)", raw_text)
        
        if amount_match:
            clean_num = amount_match.group(1).replace(",", "")
            try:
                data["amount"] = float(clean_num)
            except ValueError:
                data["amount"] = clean_num

        cat_match = re.search(r"SERVICE CATEGORY:\s*([^\n]+)", raw_text)
        if cat_match:
            data["category"] = cat_match.group(1).strip()

        status_match = re.search(r"STATUS:\s*([A-Za-z]+)", raw_text)
        if status_match:
            data["status"] = status_match.group(1).strip().lower()

        return data
