import re
import unicodedata


def normalize_text(text: str) -> str:
    """Convert fancy unicode fonts and symbols into a stable ASCII-ish form."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def extract_utr(text: str) -> str | None:
    """
    Extract a UTR / reference value from a transaction description message.

    Supports common labels such as:
    - UTR
    - Ref No / Ref ID
    - Transaction ID / Txn ID
    - UPI Ref
    """
    normalized = normalize_text(text or "")
    patterns = [
        r"(?:\bUTR\b|\bUPI\s*Ref\b|\bTransaction\s*ID\b|\bTxn\s*ID\b|\bRef\s*(?:No\.?|ID)?\b)\s*[:\-]?\s*([A-Z0-9\/\-]+)",
        r"(?:\bRef\s*(?:No\.?|ID)?\b)\s*[:\-]?\s*([A-Z0-9\/\-]{6,})",
    ]

    for pattern in patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            return match.group(1).strip().upper()

    return None


def parse_upi_message(msg: str) -> dict:
    normalized = normalize_text(msg or "")
    patterns = {
        "account": r"a/c\s+([A-Z0-9]+)",
        "cu": r"(INR)",
        "amount": r"INR\s+([\d.]+)",
        "value_time": r"on\s+(\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
    }

    extracted: dict[str, str | float | None] = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            extracted[key] = float(match.group(1)) if key == "amount" else match.group(1)

    extracted["utr"] = extract_utr(normalized)
    extracted["post_time"] = extracted.get("value_time")
    extracted["raw"] = normalized
    return extracted
