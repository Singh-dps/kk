from __future__ import annotations

from collections import defaultdict
import csv
import io
import json
from typing import Any
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy.orm import Session

from ..models import MonthlyReport, Transaction, UserPreference


def _prev_month(month_str: str) -> str:
    year, month = map(int, month_str.split("-"))
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def build_monthly_report(
    db: Session,
    user_id: int,
    report_month: str,
    account_id: int | None = None,
) -> dict[str, Any]:
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
    exclude_cc = pref.exclude_credit_card_payments if pref else True

    q = db.query(Transaction).filter(Transaction.user_id == user_id)
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    all_txs = q.order_by(Transaction.tx_date.asc()).all()

    prev_m = _prev_month(report_month)
    cur_txs = [t for t in all_txs if t.tx_date.strftime("%Y-%m") == report_month]
    prev_txs = [t for t in all_txs if t.tx_date.strftime("%Y-%m") == prev_m]

    def summarize(tx_list: list[Transaction]):
        inc = 0.0
        exp = 0.0
        cat_exp: dict[str, float] = defaultdict(float)
        inc_sources: list[Transaction] = []
        exp_items: list[Transaction] = []
        rec_items: list[Transaction] = []

        for t in tx_list:
            if t.is_internal_transfer or t.tx_type in {"transfer", "loan_disbursement", "reversal"}:
                continue
            if exclude_cc and t.tx_type == "credit_card_payment":
                continue
            if t.credit > 0 and t.tx_type == "income":
                inc += t.credit
                inc_sources.append(t)
            if t.debit > 0 and t.tx_type == "expense":
                exp += t.debit
                cat_exp[t.category] += t.debit
                exp_items.append(t)
            if t.is_recurring and t.debit > 0:
                rec_items.append(t)

        return inc, exp, cat_exp, inc_sources, exp_items, rec_items

    inc, exp, cat_exp, inc_sources, exp_items, rec_items = summarize(cur_txs)
    prev_inc, prev_exp, _, _, _, _ = summarize(prev_txs)

    net = inc - exp
    savings_rate = (net / inc * 100) if inc > 0 else 0.0

    top_5_expenses = [
        {
            "tx_date": t.tx_date.isoformat(),
            "merchant_or_recipient": t.merchant_or_recipient or t.description[:40],
            "category": t.category,
            "amount": round(t.debit, 2),
        }
        for t in sorted(exp_items, key=lambda x: x.debit, reverse=True)[:5]
    ]

    top_5_income = [
        {
            "tx_date": t.tx_date.isoformat(),
            "source": t.merchant_or_recipient or t.description[:40],
            "category": t.category,
            "amount": round(t.credit, 2),
        }
        for t in sorted(inc_sources, key=lambda x: x.credit, reverse=True)[:5]
    ]

    category_spending = [
        {"category": k, "amount": round(v, 2), "share_pct": round((v / exp * 100) if exp > 0 else 0.0, 1)}
        for k, v in sorted(cat_exp.items(), key=lambda x: x[1], reverse=True)
    ]

    recurring_payments = [
        {
            "tx_date": t.tx_date.isoformat(),
            "merchant": t.merchant_or_recipient or t.description[:40],
            "category": t.category,
            "amount": round(t.debit, 2),
        }
        for t in rec_items
    ]

    expense_change_pct = ((exp - prev_exp) / prev_exp * 100) if prev_exp > 0 else None
    income_change_pct = ((inc - prev_inc) / prev_inc * 100) if prev_inc > 0 else None

    report_data = {
        "report_month": report_month,
        "account_scope": str(account_id or "all"),
        "total_income": round(inc, 2),
        "total_expenses": round(exp, 2),
        "net_cash_flow": round(net, 2),
        "monthly_savings_estimate": round(max(net, 0.0), 2),
        "savings_rate_pct": round(savings_rate, 1),
        "transaction_count": len(cur_txs),
        "category_spending": category_spending,
        "top_5_expenses": top_5_expenses,
        "top_5_income_sources": top_5_income,
        "recurring_payments": recurring_payments,
        "previous_month_comparison": {
            "previous_month": prev_m,
            "previous_income": round(prev_inc, 2),
            "previous_expenses": round(prev_exp, 2),
            "expense_change_pct": round(expense_change_pct, 1) if expense_change_pct is not None else None,
            "income_change_pct": round(income_change_pct, 1) if income_change_pct is not None else None,
        },
    }

    scope = str(account_id or "all")
    existing = (
        db.query(MonthlyReport)
        .filter(
            MonthlyReport.user_id == user_id,
            MonthlyReport.report_month == report_month,
            MonthlyReport.account_scope == scope,
        )
        .first()
    )
    if existing:
        existing.total_income = report_data["total_income"]
        existing.total_expenses = report_data["total_expenses"]
        existing.net_cash_flow = report_data["net_cash_flow"]
        existing.report_json = json.dumps(report_data)
    else:
        db.add(
            MonthlyReport(
                user_id=user_id,
                report_month=report_month,
                account_scope=scope,
                total_income=report_data["total_income"],
                total_expenses=report_data["total_expenses"],
                net_cash_flow=report_data["net_cash_flow"],
                report_json=json.dumps(report_data),
            )
        )
    db.commit()
    return report_data


def export_report_csv(report_data: dict[str, Any]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["KHARCH KAROD - Monthly Financial Report", report_data["report_month"]])
    writer.writerow(["Total Income (INR)", report_data["total_income"]])
    writer.writerow(["Total Expenses (INR)", report_data["total_expenses"]])
    writer.writerow(["Net Cash Flow (INR)", report_data["net_cash_flow"]])
    writer.writerow(["Monthly Savings Estimate (INR)", report_data["monthly_savings_estimate"]])
    writer.writerow(["Savings Rate (%)", report_data["savings_rate_pct"]])
    writer.writerow([])
    writer.writerow(["Category-Wise Spending", "Amount (INR)", "Share (%)"])
    for row in report_data["category_spending"]:
        writer.writerow([row["category"], row["amount"], row["share_pct"]])
    writer.writerow([])
    writer.writerow(["Top 5 Expenses", "Date", "Category", "Amount (INR)"])
    for row in report_data["top_5_expenses"]:
        writer.writerow([row["merchant_or_recipient"], row["tx_date"], row["category"], row["amount"]])
    writer.writerow([])
    writer.writerow(["Top 5 Income Sources", "Date", "Category", "Amount (INR)"])
    for row in report_data["top_5_income_sources"]:
        writer.writerow([row["source"], row["tx_date"], row["category"], row["amount"]])
    return buf.getvalue().encode("utf-8")


def export_report_pdf(report_data: dict[str, Any], user_name: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"KHARCH KAROD Report {report_data['report_month']}")
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"<b>KHARCH KAROD</b> — Monthly Financial Report ({report_data['report_month']})", styles["Title"]))
    story.append(Paragraph(f"Prepared for: {user_name} | Know Your Money. Control Your Kharch.", styles["Normal"]))
    story.append(Spacer(1, 12))

    summary_rows = [
        ["Metric", "Value"],
        ["Total Income", f"INR {report_data['total_income']:,.2f}"],
        ["Total Expenses", f"INR {report_data['total_expenses']:,.2f}"],
        ["Net Cash Flow", f"INR {report_data['net_cash_flow']:,.2f}"],
        ["Estimated Monthly Savings", f"INR {report_data['monthly_savings_estimate']:,.2f} ({report_data['savings_rate_pct']}%)"],
        ["Transactions Analyzed", str(report_data["transaction_count"])],
    ]
    tbl = Table(summary_rows, colWidths=[240, 240])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#B91C1C")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 14))

    story.append(Paragraph("<b>Category-Wise Spending</b>", styles["Heading2"]))
    cat_rows = [["Category", "Amount (INR)", "Share %"]]
    for r in report_data["category_spending"]:
        cat_rows.append([r["category"], f"{r['amount']:,.2f}", f"{r['share_pct']}%"])
    if len(cat_rows) == 1:
        cat_rows.append(["No expenses recorded", "0.00", "0%"])
    cat_tbl = Table(cat_rows, colWidths=[220, 140, 120])
    cat_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1C1C20")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(cat_tbl)
    story.append(Spacer(1, 14))

    story.append(Paragraph("<b>Top 5 Expenses</b>", styles["Heading2"]))
    exp_rows = [["Date", "Merchant / Recipient", "Category", "Amount (INR)"]]
    for r in report_data["top_5_expenses"]:
        exp_rows.append([r["tx_date"], r["merchant_or_recipient"][:32], r["category"], f"{r['amount']:,.2f}"])
    if len(exp_rows) == 1:
        exp_rows.append(["-", "None", "-", "0.00"])
    exp_tbl = Table(exp_rows, colWidths=[80, 190, 110, 100])
    exp_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1C1C20")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(exp_tbl)

    doc.build(story)
    return buf.getvalue()
