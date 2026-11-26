from __future__ import annotations

import io
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from fastapi.testclient import TestClient
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table

from app.main import app


client = TestClient(app)


def _auth_headers(email: str, name: str = "Aarav Mehta"):
    res = client.post("/api/auth/signup", json={"full_name": name, "email": email, "password": "Password123!"})
    if res.status_code == 409:
        res = client.post("/api/auth/login", json={"email": email, "password": "Password123!"})
    assert res.status_code == 200, res.text
    token = res.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _make_sample_csv() -> bytes:
    return (
        "HDFC Bank Account Statement\n"
        "Account Number: XXXXXXXX4821\n"
        "Opening Balance: INR 50,000.00\n"
        "Date,Narration,Chq/Ref No,Withdrawal Amt,Deposit Amt,Closing Balance\n"
        "01/10/2026,NEFT/SALARY/TECHCORP INDIA,NEFT99001122,,80000.00,130000.00\n"
        "03/10/2026,UPI/DR/441122334455/SWIGGY FOOD,441122334455,650.00,,129350.00\n"
        "05/10/2026,UPI/DR/441122334466/RAHUL SHARMA/SBIN/Dinner,441122334466,2500.00,,126850.00\n"
        "07/10/2026,UPI/CR/441122334477/AMAZON REFUND,441122334477,,1200.00,128050.00\n"
        "10/10/2026,POS/HPCL PETROL PUMP,POS778899,3100.00,,124950.00\n"
    ).encode("utf-8")


def _make_sample_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["ICICI Bank Statement", "", "", "", ""])
    ws.append(["Date", "Description", "Debit", "Credit", "Balance"])
    ws.append(["02-10-2026", "UPI/DR/998877665544/ZOMATO ORDER", "450.50", "", "19549.50"])
    ws.append(["06-10-2026", "UPI/DR/998877665555/NETFLIX SUBSCRIPTION", "649.00", "", "18900.50"])
    ws.append(["09-10-2026", "IMPS/CR/998877665566/PRIYA VERMA/Repayment", "", "1500.00", "20400.50"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_sample_text_pdf() -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    data = [
        ["Txn Date", "Particulars", "Ref No", "Debit", "Credit", "Balance"],
        ["12/10/2026", "UPI/DR/112233445566/BESCOM ELECTRICITY", "112233445566", "1,850.00", "", "1,23,100.00"],
        ["15/10/2026", "ATM/CASH WDL/BENGALURU", "ATM009911", "5,000.00", "", "1,18,100.00"],
    ]
    tbl = Table(data)
    doc.build([tbl])
    return buf.getvalue()


def test_full_end_to_end_pipeline(tmp_path):
    headers = _auth_headers("e2e_kk@example.com")
    # Clean slate
    client.delete("/api/account/purge-data", headers=headers)

    # 1. Create bank account
    acc_res = client.post(
        "/api/accounts",
        json={
            "bank_name": "HDFC Bank",
            "nickname": "HDFC Primary",
            "account_type": "Savings",
            "account_number": "123456784821",
        },
        headers=headers,
    )
    assert acc_res.status_code == 200
    acc_id = acc_res.json()["id"]
    assert acc_res.json()["masked_number"] == "XXXX4821"

    # 2. Upload CSV + XLSX + PDF
    files = [
        ("files", ("hdfc_oct.csv", _make_sample_csv(), "text/csv")),
        ("files", ("icici_oct.xlsx", _make_sample_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ("files", ("sbi_oct.pdf", _make_sample_text_pdf(), "application/pdf")),
    ]
    up_res = client.post("/api/statements/upload", files=files, data={"account_id": str(acc_id)}, headers=headers)
    assert up_res.status_code == 200, up_res.text
    uploaded = up_res.json()["uploaded"]
    assert len(uploaded) == 3

    # 3. Analyze & Confirm each
    for st in uploaded:
        an_res = client.post(f"/api/statements/{st['id']}/analyze", headers=headers)
        assert an_res.status_code == 200, an_res.text
        an_data = an_res.json()
        assert len(an_data["preview_transactions"]) >= 2, f"Expected rows for {st['file_name']}, got {an_data}"
        cf_res = client.post(
            f"/api/statements/{st['id']}/confirm",
            json={
                "transactions": an_data["preview_transactions"],
                "opening_balance": an_data["statement"]["opening_balance"],
                "closing_balance": an_data["statement"]["closing_balance"],
            },
            headers=headers,
        )
        assert cf_res.status_code == 200

    # 4. Duplicate detection on re-uploading same CSV
    dup_up = client.post(
        "/api/statements/upload",
        files=[("files", ("hdfc_oct_dup.csv", _make_sample_csv(), "text/csv"))],
        data={"account_id": str(acc_id)},
        headers=headers,
    )
    dup_id = dup_up.json()["uploaded"][0]["id"]
    dup_an = client.post(f"/api/statements/{dup_id}/analyze", headers=headers).json()
    flagged = [t for t in dup_an["preview_transactions"] if t["is_duplicate_flagged"]]
    assert len(flagged) >= 4
    client.delete(f"/api/statements/{dup_id}", headers=headers)

    # 5. Dashboard KPI verification
    dash = client.get("/api/dashboard?period=all", headers=headers).json()
    kpis = dash["kpis"]
    assert kpis["total_received"] == 81500.0  # 80000 salary + 1500 Priya (1200 refund tracked separately!)
    assert kpis["total_refunds"] == 1200.0
    assert kpis["total_spent"] == 14199.5  # 650 + 2500 + 3100 + 450.5 + 649 + 1850 + 5000
    assert kpis["net_cash_flow"] == 67300.5
    assert kpis["total_transactions"] == 10

    # 6. P2P Money Sent & Received
    p2p = client.get("/api/p2p", headers=headers).json()
    assert p2p["total_sent"] == 2500.0
    assert p2p["total_received"] == 1500.0

    # 7. KHARCH AI Assistant
    ai = client.post("/api/assistant/ask", json={"question": "How much did I spend on food?"}, headers=headers).json()
    assert "650.00" in ai["content"] or "1,100.50" in ai["content"]
    assert len(ai["supporting_transactions"]) >= 1

    # 8. Monthly report & PDF/CSV export
    rep = client.get("/api/reports/2026-10", headers=headers).json()
    assert rep["total_income"] == 81500.0
    assert rep["total_expenses"] == 14199.5
    pdf_resp = client.get("/api/reports/2026-10/export/pdf", headers=headers)
    assert pdf_resp.status_code == 200
    assert pdf_resp.content.startswith(b"%PDF")
    csv_resp = client.get("/api/reports/2026-10/export/csv", headers=headers)
    assert csv_resp.status_code == 200

    # 9. Demo Mode load & clear
    demo_res = client.post("/api/demo/load", headers=headers)
    assert demo_res.status_code == 200
    assert demo_res.json()["transactions_created"] == 28
    client.delete("/api/demo/clear", headers=headers)

    # 10. User isolation check
    other_headers = _auth_headers("other_user_kk@example.com", "Other User")
    other_dash = client.get("/api/dashboard?period=all", headers=other_headers).json()
    assert other_dash["kpis"]["total_transactions"] == 0


def test_scanned_pdf_ocr():
    headers = _auth_headers("ocr_kk@example.com")
    client.delete("/api/account/purge-data", headers=headers)

    # Render an image-only PDF with a crisp transaction line for Tesseract OCR
    img = Image.new("RGB", (1400, 420), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=24)
    draw.text((40, 40), "DATE        DESCRIPTION                        DEBIT      BALANCE", fill=(0, 0, 0), font=font)
    draw.text((40, 100), "14/10/2026  UPI/DR/776655443322/SWIGGY ORDER     850.00     14150.00", fill=(0, 0, 0), font=font)
    draw.text((40, 160), "16/10/2026  POS/STARBUCKS COFFEE               420.00     13730.00", fill=(0, 0, 0), font=font)

    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF")
    pdf_bytes = pdf_buf.getvalue()

    up_res = client.post(
        "/api/statements/upload",
        files=[("files", ("scanned_stmt.pdf", pdf_bytes, "application/pdf"))],
        headers=headers,
    )
    assert up_res.status_code == 200
    st_id = up_res.json()["uploaded"][0]["id"]
    an_res = client.post(f"/api/statements/{st_id}/analyze", headers=headers)
    assert an_res.status_code == 200
    data = an_res.json()
    assert data["method_used"] == "tesseract_ocr"
    assert len(data["preview_transactions"]) >= 1


def test_direct_no_signin_workspace_access():
    dash_res = client.get("/api/dashboard?period=all")
    assert dash_res.status_code == 200
    assert "kpis" in dash_res.json()
    acc_res = client.get("/api/accounts")
    assert acc_res.status_code == 200


def test_slice_apostrophe_date_and_negative_rupee_parser():
    from app.services.parser import _parse_text_lines_fallback
    sample = """
    01 Nov '26 - 26 Nov '26
    1/16
    01 Nov '26 Interest Cr. for 31-Oct-2026 1000001 ₹10.8 ₹71,668.53
    01 Nov '26 UPI-Debit-630500587168-SWIGGY INSTAMART 630500587168 -₹499 ₹71,169.53
    01 Nov '26 UPI-Refund-630500587168-SWIGGY INSTAMART 630500587168 ₹59 ₹71,228.53
    """
    txs = _parse_text_lines_fallback(sample)
    assert len(txs) == 3
    assert txs[0].tx_date == "2026-11-01"
    assert txs[0].credit == 10.80
    assert txs[1].debit == 499.00
    assert txs[2].credit == 59.00


