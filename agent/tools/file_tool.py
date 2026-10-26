import os
import re

MAX_READ_CHARS = 8000


def parse_invoice_text(raw_text: str) -> dict:
    """
    Best-effort structured extraction from a plain-text invoice document.
    Returns a dict with invoice_id / company / amount ... (values are None when not found).
    The payable `amount` is the TOTAL DUE (tax included); the subtotal is only a fallback.
    """
    data = {"invoice_id": None, "company": None, "amount": None, "subtotal": None, "tax": None,
            "issue_date": None, "due_date": None, "category": None, "status": None}

    def find(pattern):
        m = re.search(pattern, raw_text)
        return m.group(1).strip() if m else None

    def number(pattern):
        v = find(pattern)
        try:
            return float(v.replace(",", "")) if v else None
        except ValueError:
            return None

    data["invoice_id"] = find(r"INVOICE:\s*([A-Za-z0-9\-]+)")
    data["company"] = find(r"FROM:\s*([^\n\r]+)")
    data["due_date"] = find(r"DUE DATE:\s*([0-9\-]+)")
    data["issue_date"] = find(r"ISSUE DATE:\s*([0-9\-]+)")
    data["category"] = find(r"SERVICE CATEGORY:\s*([^\n\r]+)")
    status = find(r"STATUS:\s*([A-Za-z]+)")
    data["status"] = status.lower() if status else None
    data["subtotal"] = number(r"SUBTOTAL:\s*[A-Z]{3}\s*([0-9,]+(?:\.[0-9]+)?)")
    data["tax"] = number(r"TAX[^:\n]*:\s*[A-Z]{3}\s*([0-9,]+(?:\.[0-9]+)?)")
    total_due = number(r"TOTAL DUE:\s*[A-Z]{3}\s*([0-9,]+(?:\.[0-9]+)?)")
    data["amount"] = total_due if total_due is not None else data["subtotal"]
    return data


class FileTool:
    """
    Sandboxed, generic file access for the agent: list / read / search under the project's `data/`
    folder (vendor invoice documents live in data/invoices/). Nothing outside `data/` is reachable.
    """

    def __init__(self, base_dir: str = None):
        root = base_dir or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.workspace = os.path.realpath(os.path.join(root, "data"))

    def _resolve(self, rel: str):
        rel = (rel or "").strip().lstrip("/\\")
        full = os.path.realpath(os.path.join(self.workspace, rel))
        if full != self.workspace and not full.startswith(self.workspace + os.sep):
            return None
        return full

    def list_files(self, subdir: str = "") -> dict:
        base = self._resolve(subdir)
        if not base or not os.path.isdir(base):
            return {"ok": False, "error": f"No such folder in the workspace: '{subdir}'."}
        files = []
        for dirpath, _dirs, names in os.walk(base):
            for n in sorted(names):
                full = os.path.join(dirpath, n)
                files.append({"path": os.path.relpath(full, self.workspace).replace(os.sep, "/"),
                              "bytes": os.path.getsize(full)})
        return {"ok": True, "folder": subdir or ".", "count": len(files), "files": files}

    def read_file(self, path: str) -> dict:
        full = self._resolve(path)
        if not full or not os.path.isfile(full):
            return {"ok": False, "error": f"File not found in the workspace: '{path}'. Use list_files to see what exists."}
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            return {"ok": False, "error": f"Could not read '{path}': {e}"}
        result = {"ok": True, "path": os.path.relpath(full, self.workspace).replace(os.sep, "/"),
                  "content": text[:MAX_READ_CHARS], "truncated": len(text) > MAX_READ_CHARS}
        parsed = parse_invoice_text(text)
        if parsed.get("invoice_id"):
            result["parsed_invoice"] = parsed
        return result

    def search(self, query: str, max_hits: int = 20) -> dict:
        query = (query or "").strip().lower()
        if not query:
            return {"ok": False, "error": "Empty search query."}
        tokens = [t for t in re.split(r"\s+", query) if t]
        hits, files = [], []
        for dirpath, _dirs, names in os.walk(self.workspace):
            for n in sorted(names):
                full = os.path.join(dirpath, n)
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                low = text.lower()
                rel = os.path.relpath(full, self.workspace).replace(os.sep, "/")
                if query in low or all(t in low for t in tokens):
                    files.append(rel)
                    for i, line in enumerate(text.splitlines(), start=1):
                        if any(t in line.lower() for t in tokens) and len(hits) < max_hits:
                            hits.append({"path": rel, "line": i, "text": line.strip()[:160]})
        return {"ok": True, "query": query, "matching_files": files, "hits": hits}
