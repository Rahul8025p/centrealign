"""
Generates sample invoice files (both text and PDF if fpdf2 is installed)
in data/invoices/ so the agent can read local documents as well as web pages.
"""
import os
import json

def generate_sample_files():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    invoices_dir = os.path.join(base_dir, "data", "invoices")
    os.makedirs(invoices_dir, exist_ok=True)

    json_path = os.path.join(base_dir, "app", "data", "invoices.json")
    if not os.path.exists(json_path):
        print(f"File not found: {json_path}")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        invoices = json.load(f)

    for inv in invoices:
        filename_txt = f"{inv['id']}_{inv['company'].replace(' ', '_')}.txt"
        filepath_txt = os.path.join(invoices_dir, filename_txt)
        content = f"""=======================================================
               INVOICE: {inv['id']}
=======================================================
ISSUED TO: Apex Enterprise Systems Pvt Ltd
FROM:      {inv['company']}
CONTACT:   {inv['contact']} ({inv['email']})
ISSUE DATE: {inv['issue_date']}
DUE DATE:   {inv['due_date']}
STATUS:     {inv['status'].upper()}

SERVICE CATEGORY: {inv['category']}
DESCRIPTION:      {inv['description']}

-------------------------------------------------------
SUBTOTAL:   {inv['currency']} {inv['amount']:,}
TAX (18%):  {inv['currency']} {inv['tax']:,}
TOTAL DUE:  {inv['currency']} {inv['total']:,}
-------------------------------------------------------
PAYMENT TERMS: Net 30 Days.
Bank Transfer to HDFC Bank A/C: 50100234981123, IFSC: HDFC0001234
Reference invoice number {inv['id']} during transfer.
=======================================================
"""
        with open(filepath_txt, "w", encoding="utf-8") as f:
            f.write(content)

    print(f"Generated {len(invoices)} invoice files in {invoices_dir}")

if __name__ == "__main__":
    generate_sample_files()
