from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
import pdfplumber
import pypdfium2 as pdfium
import pytesseract
import xlrd
from PIL import Image

try:
    import pymupdf
except ImportError:  # Lightweight Vercel serverless fallback uses pypdfium2
    pymupdf = None


DATE_PATTERNS = [
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%d %b %Y",
    "%d-%b-%Y",
    "%d %B %Y",
    "%d-%B-%Y",
    "%d-%b-%y",
    "%d %b %y",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
]

DATE_REGEX = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}[-\s][A-Za-z]{3,9}[-\s]'?\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b"
)

HEADER_KEYWORDS = {
    "date": ["date", "tx date", "txn date", "tran date", "value dt", "value date", "posting date", "transaction date"],
    "description": ["description", "narration", "particulars", "remarks", "details", "transaction details", "memo", "description/narration"],
    "debit": ["debit", "withdrawal", "withdrawals", "dr", "debit amount", "withdrawal amt", "withdrawal (dr)", "paid out", "debit(inr)"],
    "credit": ["credit", "deposit", "deposits", "cr", "credit amount", "deposit amt", "deposit (cr)", "paid in", "credit(inr)"],
    "amount": ["amount", "amt", "txn amount", "transaction amount", "amount(inr)", "amount (inr)"],
    "type": ["type", "dr/cr", "cr/dr", "drcr", "txn type", "indicator"],
    "balance": ["balance", "closing balance", "running balance", "available balance", "bal", "balance(inr)"],
    "reference": ["ref", "ref no", "chq/ref", "chq./ref.no.", "reference", "utr", "txn id", "refno", "cheque no", "chq no"],
}


@dataclass
class ParsedTransaction:
    tx_date: str
    description: str
    debit: float = 0.0
    credit: float = 0.0
    running_balance: float | None = None
    reference_number: str | None = None
    payment_method: str = "OTHER"
    confidence: float = 0.9


@dataclass
class ParseResult:
    transactions: list[ParsedTransaction] = field(default_factory=list)
    opening_balance: float | None = None
    closing_balance: float | None = None
    detected_bank: str | None = None
    detected_masked_account: str | None = None
    method_used: str = "unknown"
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)


def parse_indian_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s or s.lower() in {"nan", "nat", "none", "null", "-"}:
        return None
    # Strip trailing time if present
    s = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?(\s*[APap][Mm])?$", "", s).strip()
    # Normalize apostrophe in 2-digit year e.g. "01 Nov '26" -> "01 Nov 26"
    s = re.sub(r"\s*'(\d{2})\b", r" \1", s)
    s = re.sub(r"[,\s]+", " ", s).strip()
    for fmt in DATE_PATTERNS:
        try:
            dt = datetime.strptime(s, fmt).date()
            if 1990 <= dt.year <= 2100:
                return dt
        except ValueError:
            continue
    # Fallback regex extraction inside string
    m = DATE_REGEX.search(s)
    if m and m.group(1) != s:
        return parse_indian_date(m.group(1))
    return None


def parse_indian_amount(value: Any) -> tuple[float | None, str | None]:
    """Parse currency strings like '₹1,23,456.78 Cr', '(450.00)', 'Rs. 2,500' -> (float, 'CR'|'DR'|None)."""
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None, None
        return float(value), None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null", "-", "--", "nil", ""}:
        return None, None

    direction = None
    upper = s.upper()
    if re.search(r"\bCR\b\.?$", upper):
        direction = "CR"
    elif re.search(r"\bDR\b\.?$", upper):
        direction = "DR"

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    if s.startswith("-"):
        negative = True

    cleaned = re.sub(r"(?i)(INR|RS\.?|₹|CR\.?|DR\.?|\s)", "", s)
    cleaned = cleaned.replace(",", "")
    if cleaned.startswith("+") or cleaned.startswith("-"):
        if cleaned.startswith("-"):
            negative = True
        cleaned = cleaned[1:]

    if not cleaned or not re.match(r"^\d+(\.\d+)?$", cleaned):
        return None, direction

    val = float(cleaned)
    if negative:
        val = -abs(val)
    return val, direction


def detect_payment_method(description: str) -> str:
    upper = (description or "").upper()
    if "UPI" in upper or "@OK" in upper or "@YBL" in upper or "@IBL" in upper or "@PAYTM" in upper or "@AXIS" in upper:
        return "UPI"
    if "IMPS" in upper:
        return "IMPS"
    if "NEFT" in upper:
        return "NEFT"
    if "RTGS" in upper:
        return "RTGS"
    if "ATM" in upper or "CASH WDL" in upper or "NFS/" in upper:
        return "ATM"
    if "POS" in upper or "ECOM" in upper or "VISA" in upper or "MASTERCARD" in upper or "RUPAY" in upper or "DEBIT CARD" in upper or "CREDIT CARD" in upper:
        return "CARD"
    if "NACH" in upper or "ECS" in upper or "ACH" in upper or "MANDATE" in upper or "AUTOPAY" in upper:
        return "AUTO_DEBIT"
    if "CHQ" in upper or "CHEQUE" in upper or "CLG" in upper:
        return "CHEQUE"
    if "CASH DEP" in upper or "BY CASH" in upper:
        return "CASH"
    if "INT." in upper or "INTEREST" in upper:
        return "INTEREST"
    if "NETBANK" in upper or "INB" in upper or "MB/" in upper:
        return "NETBANKING"
    return "OTHER"


def extract_reference_number(description: str, explicit_ref: str | None = None) -> str | None:
    if explicit_ref:
        ref_clean = str(explicit_ref).strip()
        if ref_clean and ref_clean.lower() not in {"nan", "none", "-", "--", "0", "nil"}:
            return ref_clean[:100]
    desc = description or ""
    patterns = [
        r"(?:UTR|RRN|REF(?:\s*NO)?|TXN(?:\s*ID)?)[:/\s#-]*([A-Z0-9]{8,24})",
        r"UPI[/\-]([0-9]{10,16})",
        r"IMPS[/\-]([0-9]{10,16})",
        r"NEFT[/\-]([A-Z0-9]{10,22})",
        r"\b(\d{12})\b",
    ]
    for pat in patterns:
        m = re.search(pat, desc, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def detect_bank_metadata(text: str) -> tuple[str | None, str | None, float | None, float | None]:
    upper = text.upper()
    # Restrict bank name detection to the header section before transaction rows so counterparty VPAs don't override
    header_upper = upper.split("DATE DETAILS")[0][:1500]
    bank = None
    bank_map = [
        ("NESF", "Slice Small Finance Bank"),
        ("SLICE", "Slice Small Finance Bank"),
        ("HDFC", "HDFC Bank"),
        ("ICICI", "ICICI Bank"),
        ("STATE BANK OF INDIA", "State Bank of India (SBI)"),
        ("SBI", "State Bank of India (SBI)"),
        ("AXIS BANK", "Axis Bank"),
        ("KOTAK", "Kotak Mahindra Bank"),
        ("INDUSIND", "IndusInd Bank"),
        ("IDFC", "IDFC First Bank"),
        ("YES BANK", "Yes Bank"),
        ("PNB", "Punjab National Bank"),
        ("PUNJAB NATIONAL", "Punjab National Bank"),
        ("BANK OF BARODA", "Bank of Baroda"),
        ("CANARA", "Canara Bank"),
        ("FEDERAL BANK", "Federal Bank"),
    ]
    for key, label in bank_map:
        if key in header_upper:
            bank = label
            break

    masked_acc = None
    m_acc = re.search(r"(?:A/C|ACCOUNT|ACCT)\s*(?:NO\.?|NUMBER)?\s*[:\-]?\s*[X*\d]{4,18}(\d{4})", upper)
    if m_acc:
        masked_acc = f"XXXX{m_acc.group(1)}"

    opening_bal = None
    closing_bal = None
    m_open = re.search(r"OPENING\s+BALANCE\s*[:\-]?\s*(?:INR|RS\.?|₹)\s*([\d,]+(?:\.\d{1,2})?)", upper)
    if m_open:
        opening_bal, _ = parse_indian_amount(m_open.group(1))
    m_close = re.search(r"CLOSING\s+BALANCE\s*[:\-]?\s*(?:INR|RS\.?|₹)\s*([\d,]+(?:\.\d{1,2})?)", upper)
    if m_close:
        closing_bal, _ = parse_indian_amount(m_close.group(1))

    # Multi-column summary header (e.g. Slice: "Opening balance Total credits ... Closing balance" followed by ₹ values)
    if opening_bal is None or closing_bal is None:
        m_summary = re.search(
            r"OPENING\s+BALANCE\s+TOTAL\s+CREDITS.*?CLOSING\s+BALANCE.*?\n(?:.*?\n)?([^\n]*₹[^\n]+)",
            upper,
            re.DOTALL,
        )
        if m_summary:
            vals = re.findall(r"₹\s*[\d,]+(?:\.\d{1,2})?", m_summary.group(1))
            if len(vals) >= 2:
                if opening_bal is None:
                    opening_bal, _ = parse_indian_amount(vals[0])
                if closing_bal is None:
                    closing_bal, _ = parse_indian_amount(vals[-1])

    return bank, masked_acc, opening_bal, closing_bal


def _match_column_role(col_name: str) -> str | None:
    clean = re.sub(r"[^a-z0-9/\s()]", " ", str(col_name).lower()).strip()
    clean = re.sub(r"\s+", " ", clean)
    for role, synonyms in HEADER_KEYWORDS.items():
        if clean in synonyms:
            return role
    for role, synonyms in HEADER_KEYWORDS.items():
        for syn in synonyms:
            if len(syn) >= 3 and syn in clean:
                return role
    return None


def _map_headers(headers: list[Any]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, h in enumerate(headers):
        role = _match_column_role(str(h))
        if role and role not in mapping:
            mapping[role] = idx
    return mapping


def _find_header_row_in_matrix(rows: list[list[Any]]) -> tuple[int | None, dict[str, int]]:
    best_idx = None
    best_map: dict[str, int] = {}
    for idx, row in enumerate(rows[:40]):
        if not row or sum(1 for c in row if str(c).strip()) < 3:
            continue
        col_map = _map_headers(row)
        has_date = "date" in col_map
        has_money = ("debit" in col_map or "credit" in col_map or "amount" in col_map)
        if has_date and has_money and len(col_map) >= len(best_map):
            best_idx = idx
            best_map = col_map
            if "description" in col_map and ("debit" in col_map or "amount" in col_map):
                break
    return best_idx, best_map


def _rows_to_transactions(rows: list[list[Any]], col_map: dict[str, int]) -> list[ParsedTransaction]:
    txs: list[ParsedTransaction] = []
    for row in rows:
        if not row or len(row) <= col_map["date"]:
            continue
        raw_date = row[col_map["date"]]
        parsed_dt = parse_indian_date(raw_date)
        if not parsed_dt:
            # Handle multi-line description continuation in PDF tables
            if txs and "description" in col_map and len(row) > col_map["description"]:
                cont = str(row[col_map["description"]] or "").strip()
                non_empty_money = False
                for k in ("debit", "credit", "amount"):
                    if k in col_map and len(row) > col_map[k]:
                        amt, _ = parse_indian_amount(row[col_map[k]])
                        if amt and amt > 0:
                            non_empty_money = True
                if cont and not non_empty_money and cont.lower() not in {"nan", "none"}:
                    txs[-1].description = f"{txs[-1].description} {cont}".strip()
                    txs[-1].payment_method = detect_payment_method(txs[-1].description)
                    if not txs[-1].reference_number:
                        txs[-1].reference_number = extract_reference_number(txs[-1].description)
            continue

        desc = ""
        if "description" in col_map and len(row) > col_map["description"]:
            desc = str(row[col_map["description"]] or "").strip()
        if not desc or desc.lower() in {"nan", "none"}:
            # Fallback: join text cells that aren't date/money
            used_idxs = set(col_map.values())
            desc_parts = [str(c).strip() for i, c in enumerate(row) if i not in used_idxs and str(c).strip() not in {"", "nan", "None"}]
            desc = " ".join(desc_parts) or "Bank Transaction"

        # Skip opening/closing balance summary rows
        desc_upper = desc.upper()
        if desc_upper in {"OPENING BALANCE", "CLOSING BALANCE", "BROUGHT FORWARD", "CARRIED FORWARD", "TOTAL"}:
            continue

        debit = 0.0
        credit = 0.0
        if "debit" in col_map and len(row) > col_map["debit"]:
            d_val, _ = parse_indian_amount(row[col_map["debit"]])
            if d_val is not None:
                debit = abs(d_val)
        if "credit" in col_map and len(row) > col_map["credit"]:
            c_val, _ = parse_indian_amount(row[col_map["credit"]])
            if c_val is not None:
                credit = abs(c_val)

        if debit == 0.0 and credit == 0.0 and "amount" in col_map and len(row) > col_map["amount"]:
            a_val, inline_dir = parse_indian_amount(row[col_map["amount"]])
            if a_val is not None:
                tx_type_col = ""
                if "type" in col_map and len(row) > col_map["type"]:
                    tx_type_col = str(row[col_map["type"]] or "").strip().upper()
                direction = inline_dir or ("CR" if tx_type_col in {"CR", "CREDIT", "C", "DEP", "DEPOSIT"} else "DR" if tx_type_col in {"DR", "DEBIT", "D", "WDL", "WITHDRAWAL"} else None)
                if direction == "CR":
                    credit = abs(a_val)
                elif direction == "DR":
                    debit = abs(a_val)
                elif a_val < 0:
                    debit = abs(a_val)
                else:
                    # Infer from description if unsigned single column
                    if any(k in desc_upper for k in ["SALARY", "REFUND", "INTEREST", "CASH DEP", "RECEIVED", "CR/", "/CR"]):
                        credit = abs(a_val)
                    else:
                        debit = abs(a_val)

        if debit == 0.0 and credit == 0.0:
            continue

        bal = None
        if "balance" in col_map and len(row) > col_map["balance"]:
            b_val, _ = parse_indian_amount(row[col_map["balance"]])
            if b_val is not None:
                bal = b_val

        explicit_ref = None
        if "reference" in col_map and len(row) > col_map["reference"]:
            explicit_ref = str(row[col_map["reference"]] or "").strip()

        txs.append(
            ParsedTransaction(
                tx_date=parsed_dt.isoformat(),
                description=re.sub(r"\s+", " ", desc).strip(),
                debit=round(debit, 2),
                credit=round(credit, 2),
                running_balance=round(bal, 2) if bal is not None else None,
                reference_number=extract_reference_number(desc, explicit_ref),
                payment_method=detect_payment_method(desc),
                confidence=0.95,
            )
        )
    return txs


def _parse_text_lines_fallback(text: str) -> list[ParsedTransaction]:
    """Regex-based line parser for unstructured PDF text or Tesseract OCR output."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    txs: list[ParsedTransaction] = []
    # Match line starting with a date (including "01 Nov '26")
    line_date_pat = re.compile(
        r"^(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}[-\s][A-Za-z]{3}[-\s]'?\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\s+(.+)$"
    )
    money_pat = re.compile(
        r"(?:[-+]?\s*(?:₹|RS\.?|INR)\s*[\d,]+(?:\.\d{1,2})?|[-+]?\(?[\d,]+(?:\.\d{1,2})?\)?(?:\s*(?:CR|DR))?)",
        re.IGNORECASE,
    )

    prev_balance: float | None = None

    for ln in lines:
        m = line_date_pat.match(ln)
        if not m:
            if txs and not re.search(r"(?:₹|RS\.?|INR)\s*[\d,]+|\d+\.\d{2}", ln, re.IGNORECASE) and len(ln) < 120:
                upper_ln = ln.upper()
                if not (
                    re.match(r"^\d{1,3}/\d{1,3}$", ln.strip())
                    or any(
                        skip in upper_ln
                        for skip in [
                            "PAGE ",
                            "STATEMENT OF ACCOUNT",
                            "OPENING BALANCE",
                            "CLOSING BALANCE",
                            "DATE ",
                            "PARTICULARS",
                            "TOTAL CREDITS",
                            "INTEREST EARNED",
                        ]
                    )
                ):
                    txs[-1].description = f"{txs[-1].description} {ln}".strip()
                    txs[-1].payment_method = detect_payment_method(txs[-1].description)
                    if not txs[-1].reference_number:
                        txs[-1].reference_number = extract_reference_number(txs[-1].description)
            continue

        dt = parse_indian_date(m.group(1))
        if not dt:
            continue
        rest = m.group(2).strip()
        # Skip header date-range lines like "01 Nov '26 - 26 Nov '26"
        if rest.startswith("-") and parse_indian_date(rest.lstrip("- ").strip()):
            continue

        # Accept explicit currency symbol amounts (e.g. -₹499, ₹26, ₹10.8) OR formatted decimal/comma amounts
        tokens = [
            t.group(0).strip()
            for t in money_pat.finditer(rest)
            if re.search(r"₹|RS\.?|INR|\d+\.\d{1,2}|\d{1,3},\d{2,3}", t.group(0), re.IGNORECASE)
        ]
        if not tokens:
            continue

        amounts: list[tuple[float, str | None, str]] = []
        for tok in tokens[-3:]:
            val, drcr = parse_indian_amount(tok)
            if val is not None:
                amounts.append((val, drcr, tok))

        if not amounts:
            continue

        # Remove trailing amount tokens from description
        desc_part = rest
        for _, _, raw_tok in amounts:
            idx = desc_part.rfind(raw_tok)
            if idx != -1:
                desc_part = desc_part[:idx].strip()
        desc_part = re.sub(r"\s+", " ", desc_part).strip() or "Bank Transaction"
        if desc_part.upper() in {"OPENING BALANCE", "CLOSING BALANCE", "BROUGHT FORWARD", "CARRIED FORWARD"}:
            if amounts:
                prev_balance = abs(amounts[-1][0])
            continue

        # Extract trailing standalone reference number (e.g. 12-digit UTR right before amount)
        explicit_ref = None
        m_trail_ref = re.search(r"\s+(\d{10,18})$", desc_part)
        if m_trail_ref:
            explicit_ref = m_trail_ref.group(1)
            desc_part = desc_part[: m_trail_ref.start()].strip()

        debit = 0.0
        credit = 0.0
        running_bal = None

        if len(amounts) >= 2:
            tx_amt, drcr, raw_tx_tok = amounts[-2]
            bal_amt, _, _ = amounts[-1]
            running_bal = bal_amt
            if tx_amt < 0 or drcr == "DR":
                debit = abs(tx_amt)
            elif drcr == "CR":
                credit = abs(tx_amt)
            elif prev_balance is not None:
                if abs((prev_balance + abs(tx_amt)) - bal_amt) < 1.0:
                    credit = abs(tx_amt)
                elif abs((prev_balance - abs(tx_amt)) - bal_amt) < 1.0:
                    debit = abs(tx_amt)
                elif raw_tx_tok.lstrip().startswith(("₹", "Rs", "RS", "INR")) and not raw_tx_tok.lstrip().startswith("-"):
                    credit = abs(tx_amt)
                elif any(k in desc_part.upper() for k in ["CR", "SALARY", "REFUND", "INTEREST", "DEPOSIT", "RECEIVED", "UPI-CREDIT"]):
                    credit = abs(tx_amt)
                else:
                    debit = abs(tx_amt)
            else:
                if raw_tx_tok.lstrip().startswith(("₹", "Rs", "RS", "INR")) and not raw_tx_tok.lstrip().startswith("-"):
                    credit = abs(tx_amt)
                elif any(k in desc_part.upper() for k in ["CR", "SALARY", "REFUND", "INTEREST", "DEPOSIT", "RECEIVED", "UPI-CREDIT"]):
                    credit = abs(tx_amt)
                else:
                    debit = abs(tx_amt)
            prev_balance = bal_amt
        else:
            tx_amt, drcr, _ = amounts[0]
            if tx_amt < 0 or drcr == "DR":
                debit = abs(tx_amt)
            elif drcr == "CR" or (tx_amt > 0 and any(k in desc_part.upper() for k in ["SALARY", "REFUND", "INTEREST", "DEPOSIT", "RECEIVED", "/CR", "UPI-CREDIT"])):
                credit = abs(tx_amt)
            else:
                debit = abs(tx_amt)

        txs.append(
            ParsedTransaction(
                tx_date=dt.isoformat(),
                description=desc_part,
                debit=round(debit, 2),
                credit=round(credit, 2),
                running_balance=round(running_bal, 2) if running_bal is not None else None,
                reference_number=extract_reference_number(desc_part, explicit_ref),
                payment_method=detect_payment_method(desc_part),
                confidence=0.88 if running_bal is not None else 0.78,
            )
        )
    return txs


def parse_csv_statement(file_path: Path) -> ParseResult:
    raw_bytes = file_path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return ParseResult(warnings=["Unable to decode CSV file encoding."], method_used="csv")

    bank, masked_acc, open_bal, close_bal = detect_bank_metadata(text[:4000])
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except Exception:
        delimiter = ","

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    matrix = [row for row in reader if any(str(c).strip() for c in row)]
    header_idx, col_map = _find_header_row_in_matrix(matrix)
    if header_idx is None:
        return ParseResult(
            detected_bank=bank,
            detected_masked_account=masked_acc,
            opening_balance=open_bal,
            closing_balance=close_bal,
            method_used="csv",
            confidence=0.1,
            warnings=["Could not detect standard Date/Description/Amount headers in CSV. Please verify column names."],
        )

    txs = _rows_to_transactions(matrix[header_idx + 1 :], col_map)
    if not close_bal and txs and txs[-1].running_balance is not None:
        close_bal = txs[-1].running_balance
    return ParseResult(
        transactions=txs,
        opening_balance=open_bal,
        closing_balance=close_bal,
        detected_bank=bank,
        detected_masked_account=masked_acc,
        method_used="csv",
        confidence=0.95 if txs else 0.2,
        warnings=[] if txs else ["No valid transaction rows extracted from CSV."],
    )


def parse_excel_statement(file_path: Path) -> ParseResult:
    ext = file_path.suffix.lower()
    engine = "xlrd" if ext == ".xls" else "openpyxl"
    sheets_matrices: list[list[list[Any]]] = []

    try:
        if ext == ".xls":
            wb = xlrd.open_workbook(str(file_path))
            for sheet in wb.sheets():
                matrix = []
                for r_idx in range(sheet.nrows):
                    row_vals = []
                    for c_idx in range(sheet.ncols):
                        cell = sheet.cell(r_idx, c_idx)
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            dt = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                            row_vals.append(dt.date())
                        else:
                            row_vals.append("" if cell.value is None else cell.value)
                    matrix.append(row_vals)
                sheets_matrices.append(matrix)
        else:
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                matrix = [["" if cell is None else cell for cell in row] for row in ws.iter_rows(values_only=True)]
                sheets_matrices.append(matrix)
            wb.close()
    except Exception as exc:
        return ParseResult(method_used=f"excel:{engine}", warnings=[f"Could not read Excel file: {exc}"])

    best_txs: list[ParsedTransaction] = []
    meta_text_parts: list[str] = []

    for matrix in sheets_matrices:
        for r in matrix[:15]:
            meta_text_parts.append(" ".join(str(x) for x in r if str(x).strip()))
        header_idx, col_map = _find_header_row_in_matrix(matrix)
        if header_idx is not None:
            txs = _rows_to_transactions(matrix[header_idx + 1 :], col_map)
            if len(txs) > len(best_txs):
                best_txs = txs

    bank, masked_acc, open_bal, close_bal = detect_bank_metadata("\n".join(meta_text_parts))
    if not close_bal and best_txs and best_txs[-1].running_balance is not None:
        close_bal = best_txs[-1].running_balance

    return ParseResult(
        transactions=best_txs,
        opening_balance=open_bal,
        closing_balance=close_bal,
        detected_bank=bank,
        detected_masked_account=masked_acc,
        method_used=f"excel:{engine}",
        confidence=0.94 if best_txs else 0.2,
        warnings=[] if best_txs else ["No transaction table found in Excel workbook."],
    )


def _extract_pdf_text_fallback(file_path: Path) -> str:
    if pymupdf is not None:
        doc = pymupdf.open(file_path)
        text = "\n".join(page.get_text("text") for page in doc)
        doc.close()
        return text
    pdf_doc = pdfium.PdfDocument(str(file_path))
    pages_txt = []
    for page in pdf_doc:
        tp = page.get_textpage()
        pages_txt.append(tp.get_text_range())
        tp.close()
        page.close()
    pdf_doc.close()
    return "\n".join(pages_txt)


def _render_pdf_pages_for_ocr(file_path: Path) -> list[Image.Image]:
    images: list[Image.Image] = []
    if pymupdf is not None:
        doc = pymupdf.open(file_path)
        for page in doc:
            pix = page.get_pixmap(dpi=220)
            images.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
        doc.close()
        return images
    pdf_doc = pdfium.PdfDocument(str(file_path))
    for page in pdf_doc:
        bitmap = page.render(scale=220 / 72)
        images.append(bitmap.to_pil())
        page.close()
    pdf_doc.close()
    return images


def parse_pdf_statement(file_path: Path) -> ParseResult:
    warnings: list[str] = []
    all_text_pages: list[str] = []
    table_rows: list[list[Any]] = []
    header_map: dict[str, int] | None = None

    # 1. Try pdfplumber table extraction + text extraction
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                all_text_pages.append(txt)
                tables = page.extract_tables() or []
                for tbl in tables:
                    clean_tbl = [[(c or "").replace("\n", " ").strip() for c in r] for r in tbl if r]
                    if not clean_tbl:
                        continue
                    if header_map is None:
                        h_idx, col_map = _find_header_row_in_matrix(clean_tbl)
                        if h_idx is not None:
                            header_map = col_map
                            table_rows.extend(clean_tbl[h_idx + 1 :])
                    else:
                        # Skip repeated header row on subsequent pages
                        first_map = _map_headers(clean_tbl[0])
                        start = 1 if ("date" in first_map and len(first_map) >= 2) else 0
                        table_rows.extend(clean_tbl[start:])
    except Exception as exc:
        warnings.append(f"pdfplumber warning: {exc}")

    full_text = "\n".join(all_text_pages)
    method_used = "pdfplumber_table"
    txs: list[ParsedTransaction] = []

    if header_map and table_rows:
        txs = _rows_to_transactions(table_rows, header_map)

    # 2. Fallback to PyMuPDF/pypdfium2 text extraction if pdfplumber tables yielded nothing
    if not txs:
        if len(full_text.strip()) < 80:
            try:
                full_text = _extract_pdf_text_fallback(file_path)
            except Exception as exc:
                warnings.append(f"PDF text fallback warning: {exc}")

        if len(full_text.strip()) >= 80:
            method_used = "pdf_text_fallback"
            txs = _parse_text_lines_fallback(full_text)

    # 3. Fallback to Tesseract OCR for scanned PDFs
    if not txs and len(full_text.strip()) < 150:
        method_used = "tesseract_ocr"
        ocr_pages: list[str] = []
        try:
            images = _render_pdf_pages_for_ocr(file_path)
            for img in images:
                ocr_txt = pytesseract.image_to_string(img)
                ocr_pages.append(ocr_txt)
            full_text = "\n".join(ocr_pages)
            txs = _parse_text_lines_fallback(full_text)
            if not txs:
                warnings.append("OCR ran on scanned PDF but could not confidently parse tabular transactions. Please review.")
        except pytesseract.TesseractNotFoundError:
            warnings.append("Scanned PDF OCR requires the Tesseract binary, which is not available in this serverless runtime. Please upload a digital PDF, CSV, or Excel file.")
        except Exception as exc:
            warnings.append(f"OCR processing failed: {exc}")

    bank, masked_acc, open_bal, close_bal = detect_bank_metadata(full_text[:6000])
    if not close_bal and txs and txs[-1].running_balance is not None:
        close_bal = txs[-1].running_balance
    if not open_bal and txs and txs[0].running_balance is not None:
        first_tx = txs[0]
        open_bal = round(first_tx.running_balance + first_tx.debit - first_tx.credit, 2)

    conf = 0.92 if (txs and method_used == "pdfplumber_table") else 0.78 if txs else 0.15
    if not txs and not warnings:
        warnings.append("No transactions could be automatically extracted from this PDF. Please verify the file or review manually.")

    return ParseResult(
        transactions=txs,
        opening_balance=open_bal,
        closing_balance=close_bal,
        detected_bank=bank,
        detected_masked_account=masked_acc,
        method_used=method_used,
        confidence=conf,
        warnings=warnings,
    )


def parse_statement_file(file_path: Path) -> ParseResult:
    ext = file_path.suffix.lower()
    if ext == ".csv":
        return parse_csv_statement(file_path)
    if ext in {".xlsx", ".xls"}:
        return parse_excel_statement(file_path)
    if ext == ".pdf":
        return parse_pdf_statement(file_path)
    return ParseResult(warnings=[f"Unsupported file format: {ext}"], method_used="unsupported")
