from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any

from ..config import GEMINI_API_KEY
from ..security import sanitize_for_external_ai
from .constants import (
    CREDIT_CARD_PAYMENT_CATEGORY,
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    LOAN_CATEGORY,
    REVERSAL_CATEGORY,
    TRANSFER_CATEGORY,
)

# Keyword rules for deterministic Indian financial classification
EXPENSE_RULES: list[tuple[str, list[str]]] = [
    ("Food", ["ZOMATO", "SWIGGY", "BLINKIT", "ZEPTO", "INSTAMART", "BIGBASKET", "GROFERS", "DMART", "DUNZO", "MILKBASKET", "COUNTRY DELIGHT", "GROCERY", "SUPERMARKET", "BAKERY", "DAIRY"]),
    ("Restaurants", ["STARBUCKS", "MCDONALDS", "KFC", "DOMINOS", "PIZZA HUT", "BURGER KING", "HALDIRAM", "BARBEQUE", "CAFE", "RESTAURANT", "BISTRO", "DINER", "BIRYANI", "CHAI", "COFFEE"]),
    ("Fuel", ["HPCL", "BPCL", "IOCL", "INDIAN OIL", "BHARAT PETROLEUM", "HINDUSTAN PETROLEUM", "SHELL", "PETROL", "DIESEL", "FUEL", "NAYARA", "JIO-BP"]),
    ("Transportation", ["UBER", "OLA", "RAPIDO", "METRO", "DMRC", "BMTC", "FASTAG", "PARKING", "TOLL", "AUTO", "CAB", "RAILWAY", "REDBUS", "BUS TICKET"]),
    ("Travel", ["IRCTC", "MAKEMYTRIP", "GOIBIBO", "INDIGO", "AIR INDIA", "VISTARA", "AKASA", "SPICEJET", "CLEARTRIP", "OYO", "AIRBNB", "BOOKING.COM", "HOTEL", "FLIGHT", "RESORT"]),
    ("Electricity", ["BESCOM", "TATA POWER", "ADANI ELECTRICITY", "MSEB", "MAHADISCOM", "BSES", "TORRENT POWER", "ELECTRICITY", "POWER BILL", "UPPCL", "CESC"]),
    ("Bills", ["AIRTEL", "JIO", "VODAFONE", "VI ", "BSNL", "ACT FIBERNET", "BROADBAND", "WIFI", "DTH", "TATA PLAY", "GAS BILL", "IGL", "MGL", "WATER BILL", "POSTPAID", "RECHARGE", "BBPS", "UTILITY"]),
    ("Rent", ["RENT", "NOBROKER", "MYGATE", "HOUSING", "LANDLORD", "PG RENT", "HOSTEL FEE", "MAINTENANCE"]),
    ("Subscriptions", ["NETFLIX", "SPOTIFY", "AMAZON PRIME", "HOTSTAR", "DISNEY", "YOUTUBE", "APPLE.COM/BILL", "APPLE SERVICES", "GOOGLE ONE", "ICLOUD", "CHATGPT", "OPENAI", "ADOBE", "MICROSOFT", "CANVA", "LINKEDIN", "SUBSCRIPTION"]),
    ("Entertainment", ["BOOKMYSHOW", "PVR", "INOX", "CINEPOLIS", "STEAM", "PLAYSTATION", "XBOX", "GAMING", "CONCERT", "CLUB", "BOWLING"]),
    ("Healthcare", ["APOLLO", "PHARMEASY", "1MG", "TATA 1MG", "NETMEDS", "MEDPLUS", "HOSPITAL", "CLINIC", "PHARMACY", "MEDICAL", "DIAGNOSTIC", "LAL PATH", "THYROCARE", "DOCTOR", "DENTAL"]),
    ("Education", ["UDEMY", "COURSERA", "UNACADEMY", "BYJUS", "PHYSICS WALLAH", "SCHOOL", "COLLEGE", "UNIVERSITY", "TUITION", "EXAM FEE", "BOOKS", "KINDLE"]),
    ("Shopping", ["AMAZON", "FLIPKART", "MYNTRA", "AJIO", "NYKAA", "MEESHO", "CROMA", "RELIANCE DIGITAL", "DECATHLON", "IKEA", "ZARA", "H&M", "WESTSIDE", "TRENT", "LIFESTYLE", "SHOPPING", "MALL"]),
    ("ATM withdrawals", ["ATM", "CASH WDL", "NFS/CASH", "ATW-", "ATM-CASH"]),
    ("Online payments", ["RAZORPAY", "CASHFREE", "PAYU", "BILLDESK", "PHONEPE", "GPAY", "GOOGLE PAY", "PAYTM"]),
]

INCOME_RULES: list[tuple[str, list[str]]] = [
    ("Salary", ["SALARY", "PAYROLL", "SAL CR", "MONTHLY PAY", "STIPEND", "NEFT/SALARY", "WAGES"]),
    ("Freelancing", ["FREELANCE", "UPWORK", "FIVERR", "TOPTAL", "DEEL", "WISE", "PAYONEER", "CONSULTING", "INVOICE"]),
    ("Business income", ["SETTLEMENT", "MERCHANT PAYOUT", "RAZORPAYX", "STRIPE", "SHOPIFY", "CLIENT PAYMENT", "BUSINESS"]),
    ("Refunds", ["REFUND", "REVERSAL", "CASHBACK", "CHARGEBACK", "RETURN", "REV/"]),
    ("Interest", ["INT.PD", "INTEREST", "INT CREDIT", "SBINT", "FD INTEREST", "DIVIDEND"]),
    ("Cash deposits", ["CASH DEP", "BY CASH", "CDM", "SELF DEPOSIT"]),
]

KNOWN_MERCHANT_KEYWORDS = {
    "ZOMATO", "SWIGGY", "BLINKIT", "ZEPTO", "INSTAMART", "BIGBASKET", "DMART", "AMAZON", "FLIPKART",
    "MYNTRA", "AJIO", "NYKAA", "MEESHO", "UBER", "OLA", "RAPIDO", "IRCTC", "MAKEMYTRIP", "GOIBIBO",
    "INDIGO", "NETFLIX", "SPOTIFY", "HOTSTAR", "YOUTUBE", "APPLE", "GOOGLE", "OPENAI", "CHATGPT",
    "AIRTEL", "JIO", "BESCOM", "TATAPOWER", "CRED", "APOLLO", "PHARMEASY", "1MG", "BOOKMYSHOW",
    "PVR", "INOX", "STARBUCKS", "MCDONALDS", "DOMINOS", "KFC", "HPCL", "BPCL", "IOCL", "SHELL",
    "RAZORPAY", "PAYTM", "PHONEPE", "NOBROKER",
}

NON_PERSON_TOKENS = {
    "PRIVATE", "PVT", "LTD", "LIMITED", "LLP", "ENTERPRISES", "STORE", "MART", "TRADERS",
    "SERVICES", "TECHNOLOGIES", "SOLUTIONS", "BANK", "PAYMENTS", "RETAIL", "INDIA", "CORP",
    "PHARMACY", "HOSPITAL", "HOTEL", "RESTAURANT", "CAFE", "PETROLEUM", "MOTORS", "ONLINE",
    "TELECOM", "BILLDESK", "RAZORPAY", "CRED", "AUTOPAY", "MANDATE", "ATM", "SELF", "CASH",
    "SALARY", "INTEREST", "REFUND", "REVERSAL", "TAX", "GST", "INSURANCE", "MUTUAL", "FUND",
    "SHOP", "MEDICAL", "VEG", "LOYALTY", "GRAND", "CENTER", "CENTRE", "BAZAAR", "BAKERY",
    "UDUPI", "DAIRY", "SUPERMARKET", "HARDWARE", "ELECTRONICS", "COLLECTION", "AGENCY",
}


def compute_fingerprint(tx_date: str, debit: float, credit: float, description: str, reference: str | None) -> str:
    norm_desc = re.sub(r"[^A-Z0-9]", "", (description or "").upper())[:32]
    amount = f"{debit:.2f}:{credit:.2f}"
    ref = (reference or "").strip().upper()
    raw = f"{tx_date}|{amount}|{ref}|{norm_desc}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def extract_merchant_and_person(description: str, payment_method: str, tx_type: str) -> tuple[str | None, str | None]:
    desc = (description or "").strip()
    upper = desc.upper()

    # Check known merchants first (using whole-word match so e.g. OLA doesn't match NICHOLAS)
    for kw in KNOWN_MERCHANT_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", upper):
            return kw.title(), None

    candidate = None
    # UPI/DR/123456789012/RAHUL SHARMA/SBIN/rahul@okaxis/Lunch
    if "UPI" in upper:
        parts = [p.strip() for p in re.split(r"[/\-|]", desc) if p.strip()]
        filtered = []
        for p in parts:
            pu = p.upper()
            if pu in {"UPI", "DR", "CR", "DEBIT", "CREDIT", "REFUND", "P2M", "P2A", "P2P", "PAY", "TRANSFER", "PAYMENT"}:
                continue
            if re.match(r"^\d{6,}$", p):
                continue
            if "@" in p:
                continue
            if re.match(r"^[A-Z]{4}0[A-Z0-9]{6}$", pu) or pu in {"SBIN", "HDFC", "ICIC", "UTIB", "KKBK", "OKAXIS", "OKICICI", "YESB", "PYTM"}:
                continue
            filtered.append(p)
        if filtered:
            candidate = filtered[0]

    # IMPS/NEFT patterns: IMPS-IN/12345/PRIYA VERMA/...
    if not candidate and any(k in upper for k in ["IMPS", "NEFT", "RTGS"]):
        parts = [p.strip() for p in re.split(r"[/\-|]", desc) if p.strip()]
        for p in parts:
            pu = p.upper()
            if pu in {"IMPS", "NEFT", "RTGS", "IN", "OUT", "P2A", "P2P", "MB", "IB"}:
                continue
            if re.match(r"^[A-Z0-9]{10,}$", pu) and any(ch.isdigit() for ch in pu):
                continue
            if len(p) >= 3:
                candidate = p
                break

    if not candidate:
        # Clean common prefixes
        cleaned = re.sub(r"(?i)^(POS|ECOM|ACH|NACH|ATM|BIL|INB|MB)[\s/\-:]+", "", desc).strip()
        cleaned = re.split(r"[/|]", cleaned)[0].strip()
        candidate = cleaned[:60] if cleaned else None

    if not candidate:
        return None, None

    cand_clean = re.sub(r"\s+", " ", candidate).strip(" -_.,")
    cand_upper = cand_clean.upper()

    # Decide if candidate looks like an individual person name
    is_non_person = any(tok in cand_upper.split() for tok in NON_PERSON_TOKENS) or any(kw in cand_upper for kw in KNOWN_MERCHANT_KEYWORDS)
    looks_like_person = (
        not is_non_person
        and payment_method in {"UPI", "IMPS", "NEFT", "RTGS"}
        and bool(re.match(r"^[A-Za-z][A-Za-z.\s]{2,38}$", cand_clean))
        and len(cand_clean.split()) <= 4
        and not any(ch.isdigit() for ch in cand_clean)
    )

    person_name = cand_clean.title() if looks_like_person else None
    merchant_name = cand_clean.title() if cand_clean else None
    return merchant_name, person_name


def classify_transaction_deterministic(
    description: str,
    debit: float,
    credit: float,
    payment_method: str,
    user_account_masks: list[str] | None = None,
) -> dict[str, Any]:
    upper = (description or "").upper()
    user_account_masks = user_account_masks or []

    # 1. Credit Card Bill Payment detection (prevent double counting)
    if debit > 0 and any(k in upper for k in ["CREDIT CARD", "CARD PAYMENT", "CC BILL", "CC PAY", "CRED CLUB", "CRED PAY", "HDFC CARD", "ICICI CARD", "SBI CARD", "AMEX"]):
        merchant, _ = extract_merchant_and_person(description, payment_method, "credit_card_payment")
        return {
            "tx_type": "credit_card_payment",
            "category": CREDIT_CARD_PAYMENT_CATEGORY,
            "merchant_or_recipient": merchant or "Credit Card Bill",
            "p2p_person": None,
            "is_internal_transfer": False,
        }

    # 2. Internal self-transfer detection
    is_self = any(k in upper for k in ["SELF TRANSFER", "OWN ACCOUNT", "TRF TO SELF", "INTERNAL TRF", "SWEEP", "AUTO SWEEP"])
    if not is_self and user_account_masks:
        for mask in user_account_masks:
            last4 = mask[-4:]
            if last4 and last4 != "0000" and re.search(rf"(?:XX|X+|\*+|A/C\s*){re.escape(last4)}\b", upper):
                is_self = True
                break

    if is_self:
        merchant, _ = extract_merchant_and_person(description, payment_method, "transfer")
        return {
            "tx_type": "transfer",
            "category": TRANSFER_CATEGORY,
            "merchant_or_recipient": merchant or "Self Account Transfer",
            "p2p_person": None,
            "is_internal_transfer": True,
        }

    # 3. Credit transactions
    if credit > 0 and debit == 0:
        if any(k in upper for k in ["LOAN DISB", "LOAN CREDIT", "PERSONAL LOAN", "DISBURSEMENT"]):
            return {
                "tx_type": "loan_disbursement",
                "category": LOAN_CATEGORY,
                "merchant_or_recipient": "Loan Provider",
                "p2p_person": None,
                "is_internal_transfer": False,
            }
        if any(k in upper for k in ["REVERSAL", "REV/", "CHQ RETURN", "BOUNCE REV"]):
            return {
                "tx_type": "reversal",
                "category": REVERSAL_CATEGORY,
                "merchant_or_recipient": "Bank Reversal",
                "p2p_person": None,
                "is_internal_transfer": False,
            }

        cat = None
        for cname, kws in INCOME_RULES:
            if any(k in upper for k in kws):
                cat = cname
                break

        merchant, person = extract_merchant_and_person(description, payment_method, "income")
        if cat == "Refunds":
            return {
                "tx_type": "refund",
                "category": "Refunds",
                "merchant_or_recipient": merchant or "Merchant Refund",
                "p2p_person": None,
                "is_internal_transfer": False,
            }
        if not cat:
            cat = "Money received from people" if person else "Other credits"

        return {
            "tx_type": "income",
            "category": cat,
            "merchant_or_recipient": merchant or "Credit",
            "p2p_person": person,
            "is_internal_transfer": False,
        }

    # 4. Debit transactions
    cat = None
    for cname, kws in EXPENSE_RULES:
        if any(k in upper for k in kws):
            cat = cname
            break

    merchant, person = extract_merchant_and_person(description, payment_method, "expense")
    if not cat:
        if payment_method == "ATM":
            cat = "ATM withdrawals"
        elif person:
            cat = "Online payments"
        else:
            cat = "Other expenses"

    return {
        "tx_type": "expense",
        "category": cat,
        "merchant_or_recipient": merchant or "Expense",
        "p2p_person": person,
        "is_internal_transfer": False,
    }


def enrich_ambiguous_with_gemini(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Optional Gemini classification ONLY for ambiguous descriptions, using sanitized strings."""
    if not GEMINI_API_KEY:
        return items
    ambiguous = [
        it for it in items
        if it["category"] in {"Other expenses", "Other credits", "Online payments"}
    ]
    if not ambiguous:
        return items

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        payload = [
            {
                "id": it["temp_id"],
                "desc": sanitize_for_external_ai(it["description"])[:120],
                "kind": "credit" if it["credit"] > 0 else "debit",
                "method": it["payment_method"],
            }
            for it in ambiguous[:40]
        ]
        prompt = (
            "Classify these Indian bank transaction descriptions into one exact category.\n"
            f"Income categories: {INCOME_CATEGORIES}\n"
            f"Expense categories: {EXPENSE_CATEGORIES}\n"
            "Return ONLY a JSON array of objects: [{\"id\": \"...\", \"category\": \"...\", \"merchant\": \"...\"}].\n"
            f"Transactions: {json.dumps(payload)}"
        )
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        raw = (resp.text or "").strip()
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            by_id = {str(x.get("id")): x for x in parsed if isinstance(x, dict)}
            valid_cats = set(INCOME_CATEGORIES + EXPENSE_CATEGORIES)
            for it in items:
                upd = by_id.get(it["temp_id"])
                if upd:
                    cat = upd.get("category")
                    if cat in valid_cats:
                        it["category"] = cat
                    if upd.get("merchant"):
                        it["merchant_or_recipient"] = str(upd["merchant"])[:100]
    except Exception:
        # Gracefully fall back to deterministic classification
        pass
    return items


def detect_duplicates_and_transfers(
    staged_items: list[dict[str, Any]],
    existing_transactions: list[Any],
    user_account_ids: list[int],
    current_account_id: int,
) -> list[dict[str, Any]]:
    """Compare date, amount, description, and reference number against existing DB records and current batch."""
    seen_in_batch: dict[str, str] = {}

    for item in staged_items:
        fp = compute_fingerprint(
            item["tx_date"],
            item["debit"],
            item["credit"],
            item["description"],
            item.get("reference_number"),
        )
        item["fingerprint"] = fp

        # Check within batch
        if fp in seen_in_batch:
            item["is_duplicate_flagged"] = True
            item["duplicate_reason"] = f"Identical transaction in this upload (matches {seen_in_batch[fp]})"
            item["include_in_import"] = False
            continue
        seen_in_batch[fp] = item["tx_date"]

        # Check against existing DB transactions
        item_amt = item["debit"] if item["debit"] > 0 else item["credit"]
        for ex in existing_transactions:
            ex_date = ex.tx_date.isoformat() if hasattr(ex.tx_date, "isoformat") else str(ex.tx_date)
            ex_amt = ex.debit if ex.debit > 0 else ex.credit

            # Internal transfer across different user bank accounts (same date/amount, opposite debit/credit)
            if (
                ex.account_id != current_account_id
                and ex.account_id in user_account_ids
                and ex_date == item["tx_date"]
                and abs(ex_amt - item_amt) < 0.01
                and ((ex.debit > 0 and item["credit"] > 0) or (ex.credit > 0 and item["debit"] > 0))
            ):
                item["is_internal_transfer"] = True
                item["tx_type"] = "transfer"
                item["category"] = TRANSFER_CATEGORY
                item["duplicate_reason"] = f"Matched opposing entry in another linked account on {ex_date}"
                break

            # Exact reference + amount match in same account
            if (
                ex.account_id == current_account_id
                and item.get("reference_number")
                and ex.reference_number
                and item["reference_number"].upper() == ex.reference_number.upper()
                and abs(ex_amt - item_amt) < 0.01
            ):
                item["is_duplicate_flagged"] = True
                item["duplicate_match_id"] = ex.id
                item["duplicate_reason"] = f"Matching reference #{ex.reference_number} & amount ₹{item_amt:,.2f} on {ex_date}"
                item["include_in_import"] = False
                break

            # Same date, same account, same amount, high description similarity
            if ex.account_id == current_account_id and ex_date == item["tx_date"] and abs(ex_amt - item_amt) < 0.01:
                sim = SequenceMatcher(None, (ex.description or "").upper(), (item["description"] or "").upper()).ratio()
                if sim >= 0.78:
                    item["is_duplicate_flagged"] = True
                    item["duplicate_match_id"] = ex.id
                    item["duplicate_reason"] = f"Possible duplicate of existing transaction on {ex_date} ({int(sim*100)}% match)"
                    item["include_in_import"] = False
                    break

    return staged_items
