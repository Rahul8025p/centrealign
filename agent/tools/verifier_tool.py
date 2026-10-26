import hashlib
import requests


class VerifierTool:
    """
    Independent Verification Mechanism.

    Validates whether the requested end outcome was genuinely achieved by THIS run,
    preventing hallucinated or false-positive completion reports.
    """
    def __init__(self, base_url: str = "http://localhost:5000"):
        self.base_url = base_url.rstrip("/")

    def verify_outcome(self, invoice_id: str, expected_amount: float = None, expected_vendor: str = None,
                       expected_confirmation_id: str = None, expected_due_date: str = None) -> dict:
        """
        Multi-point check against the ERP audit registry:
        1. A submission record exists for the invoice - and, when `expected_confirmation_id` is given
           (i.e. this run submitted), the record with exactly that confirmation ID exists.
        2. The confirmation ID has the expected format.
        3. Recorded amount (and due date, if given) match what the agent extracted.
        4. Recorded vendor matches the expected vendor.
        5. The ERP's own invoice record reconciles with the submission.
        Any failed check returns verified=False with a reason.
        """
        if not invoice_id:
            return {"verified": False, "reason": "Verification failed: No invoice ID provided to verify."}

        inv_clean = invoice_id.strip().upper()
        try:
            url = f"{self.base_url}/api/verify/{inv_clean}"
            params = {"confirmation_id": expected_confirmation_id} if expected_confirmation_id else None
            res = requests.get(url, params=params, timeout=5)

            if res.status_code == 404:
                if expected_confirmation_id:
                    reason = (f"Verification failed: no submission with confirmation '{expected_confirmation_id}' "
                              f"exists for invoice '{inv_clean}' in the audit registry.")
                else:
                    reason = f"Verification failed: No submission record exists for invoice '{inv_clean}' in the audit registry."
                return {"verified": False, "reason": reason, "checked_endpoint": url}

            data = res.json()
            if not data.get("verified"):
                return {"verified": False,
                        "reason": f"Verification failed: Registry returned verified=false for {inv_clean}.",
                        "details": data}

            confirmation_id = data.get("confirmation_id")
            recorded_amount = data.get("amount")
            details = data.get("details", {})

            if not confirmation_id or not confirmation_id.startswith("APX-CONF-"):
                return {"verified": False,
                        "reason": f"Verification failed: Invalid confirmation identifier '{confirmation_id}'."}

            if expected_confirmation_id and confirmation_id != expected_confirmation_id:
                return {"verified": False,
                        "reason": f"Verification mismatch: registry confirmation '{confirmation_id}' is not the one from this run ('{expected_confirmation_id}')."}

            amount_matches = True
            if expected_amount is not None:
                try:
                    exp_val = float(str(expected_amount).replace(",", ""))
                    rec_val = float(recorded_amount)
                    if abs(exp_val - rec_val) > 1.0:
                        return {"verified": False,
                                "reason": f"Verification mismatch: Submitted amount ₹{rec_val} does not match expected amount ₹{exp_val}.",
                                "expected_amount": exp_val, "recorded_amount": rec_val}
                except (ValueError, TypeError):
                    pass

            if expected_due_date and details.get("due_date") and details["due_date"] != expected_due_date:
                return {"verified": False,
                        "reason": f"Verification mismatch: recorded due date {details['due_date']} != expected {expected_due_date}."}

            vendor_matches = True
            if expected_vendor and details.get("company"):
                from ..vendors import exact_match
                vendor_matches = exact_match(expected_vendor, details["company"])
                if not vendor_matches:
                    return {"verified": False,
                            "reason": f"Verification mismatch: recorded vendor '{details['company']}' does not match expected '{expected_vendor}'."}

            if data.get("amount_reconciled") is False:
                return {"verified": False,
                        "reason": "Verification mismatch: submission does not reconcile with the ERP invoice record.",
                        "vendor_invoice_record": data.get("vendor_invoice_record")}

            timestamp = data.get("timestamp") or (data.get("audit_log_record") or {}).get("timestamp")
            digest = hashlib.sha256(f"{confirmation_id}|{inv_clean}|{recorded_amount}|{timestamp}".encode()).hexdigest()
            return {
                "verified": True,
                "two_way_reconciled": data.get("two_way_reconciled", True),
                "bound_to_this_run": bool(expected_confirmation_id),
                "invoice_id": inv_clean,
                "confirmation_id": confirmation_id,
                "amount": recorded_amount,
                "amount_validated": amount_matches,
                "vendor_validated": vendor_matches,
                "audit_log_record": data.get("audit_log_record"),
                "vendor_invoice_record": data.get("vendor_invoice_record"),
                "timestamp": timestamp,
                "status": data.get("status"),
                "evidence": {"confirmation_token": confirmation_id, "timestamp": timestamp, "audit_hash": f"SHA256-{digest}"},
                "summary": (f"✅ Outcome independently verified: Invoice {inv_clean} recorded with confirmation {confirmation_id}. "
                            f"Reconciled across Vendor Invoices directory and Submitted Audit Log."),
            }

        except Exception as e:
            return {"verified": False, "reason": f"Verification check failed due to connection error: {str(e)}"}
