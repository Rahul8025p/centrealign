"""
Vendor / invoice-ID resolution.

Core rule: a vendor is only accepted on an EXACT (normalised) match.
Near matches are returned as suggestions for the user to confirm; an unknown
vendor is never silently replaced by a different one.
"""
import re
import difflib

SUFFIX_TOKENS = {
    "pvt", "ltd", "limited", "private", "inc", "llc", "llp", "corp", "corporation",
    "co", "company", "solutions", "bank", "technologies", "systems", "services", "group",
}

# Words that can follow "from/for/by..." but are not vendor names.
STOPWORDS = {
    "the", "a", "an", "our", "my", "your", "this", "that", "it", "me", "us", "them",
    "and", "then", "to", "in", "into", "on", "at", "with", "of",
    "latest", "last", "recent", "pending", "overdue", "unpaid", "new", "newest", "oldest",
    "invoice", "invoices", "payment", "payments", "bill", "bills", "details", "amount",
    "internal", "system", "portal", "billing", "extract", "enter", "submit", "pay",
    "find", "locate", "verify", "check", "confirm", "tell", "please", "status",
    "now", "today", "all", "any", "each", "every",
}

PREPOSITIONS = {"from", "for", "by", "vendor", "supplier", "company", "payee", "of"}


def _tokens(text: str):
    return re.findall(r"[A-Za-z0-9&']+", text or "")


def squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def core_tokens(name: str):
    toks = [t.lower() for t in _tokens(name)]
    core = [t for t in toks if t not in SUFFIX_TOKENS]
    return core or toks


def forms(name: str) -> set:
    """Comparable forms of a vendor name: fully squashed and suffix-stripped."""
    full = squash(name)
    core = squash("".join(core_tokens(name)))
    return {f for f in (full, core) if f}


def exact_match(candidate: str, vendor_name: str) -> bool:
    a, b = forms(candidate), forms(vendor_name)
    return any(len(x) >= 4 for x in (a & b))


def similarity(candidate: str, vendor_name: str) -> float:
    a = squash("".join(core_tokens(candidate)))
    b = squash("".join(core_tokens(vendor_name)))
    if not a or not b:
        return 0.0
    score = difflib.SequenceMatcher(None, a, b).ratio()
    if len(a) >= 4 and b.startswith(a):  # "Tech" -> "TechCorp"
        score = max(score, 0.8)
    return score


def suggest_vendors(candidate: str, vendor_names, threshold: float = 0.75, limit: int = 3):
    scored = [(similarity(candidate, v), v) for v in vendor_names]
    scored = [s for s in scored if s[0] >= threshold]
    scored.sort(reverse=True)
    return [v for _, v in scored[:limit]]


def extract_vendor_candidate(prompt: str):
    """Best guess at the vendor name the user typed (used only for messaging/suggestions)."""
    toks = _tokens(prompt)
    best = None
    for i, tok in enumerate(toks):
        if tok.lower() not in PREPOSITIONS:
            continue
        words = []
        for nxt in toks[i + 1:i + 5]:
            low = nxt.lower()
            if low in STOPWORDS or re.fullmatch(r"inv-?\d+", low):
                if low in ("the",) and not words:
                    continue
                break
            words.append(nxt)
        if words:
            best = " ".join(words)  # keep the last plausible candidate
    return best


def find_exact_vendors(prompt: str, vendor_names):
    """Vendors whose name appears (as 1-4 consecutive words) in the prompt."""
    toks = _tokens(prompt)
    found = []
    for n in range(1, 5):
        for i in range(len(toks) - n + 1):
            gram = " ".join(toks[i:i + n])
            for v in vendor_names:
                if v not in found and exact_match(gram, v):
                    found.append(v)
    return found


def resolve_target(prompt: str, vendors, invoice_ids):
    """
    vendors: list of vendor display names; invoice_ids: list of known invoice IDs.
    Returns either
      {"status": "ok", "kind": "invoice"|"vendor", "query": str, "vendor": str|None}
    or
      {"status": "needs_clarification", "reason": str, "requested": str|None,
       "suggestions": [...], "known_vendors": [...]}
    """
    known_ids = {i.upper(): i for i in invoice_ids}
    vendors = list(dict.fromkeys(vendors))

    ids_in_prompt = [m.upper() for m in re.findall(r"INV-\d+", prompt or "", re.IGNORECASE)]
    if ids_in_prompt:
        missing = [i for i in ids_in_prompt if i not in known_ids]
        if missing:
            return {
                "status": "needs_clarification",
                "reason": f"Invoice {', '.join(missing)} was not found in the invoice files or the ERP.",
                "requested": missing[0],
                "suggestions": sorted(known_ids.values()),
                "known_vendors": vendors,
            }
        return {"status": "ok", "kind": "invoice", "query": ids_in_prompt[0], "vendor": None}

    exact = find_exact_vendors(prompt, vendors)
    if len(exact) == 1:
        return {"status": "ok", "kind": "vendor", "query": exact[0], "vendor": exact[0]}
    if len(exact) > 1:
        return {
            "status": "needs_clarification",
            "reason": "The request matches more than one vendor.",
            "requested": None,
            "suggestions": exact,
            "known_vendors": vendors,
        }

    candidate = extract_vendor_candidate(prompt)
    if candidate:
        return {
            "status": "needs_clarification",
            "reason": f"No vendor named '{candidate}' exists in the invoice files or the ERP.",
            "requested": candidate,
            "suggestions": suggest_vendors(candidate, vendors),
            "known_vendors": vendors,
        }
    return {
        "status": "needs_clarification",
        "reason": "I couldn't tell which vendor or invoice you mean.",
        "requested": None,
        "suggestions": [],
        "known_vendors": vendors,
    }
