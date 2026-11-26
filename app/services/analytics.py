from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import statistics
from typing import Any
from sqlalchemy.orm import Session

from ..models import BankAccount, Transaction, UserPreference


def resolve_date_range(
    period: str | None,
    start_date: date | None = None,
    end_date: date | None = None,
    reference_today: date | None = None,
) -> tuple[date | None, date | None]:
    today = reference_today or date.today()
    p = (period or "all").lower()
    if p == "all":
        return None, None
    if p == "today":
        return today, today
    if p in {"7d", "last_7_days"}:
        return today - timedelta(days=6), today
    if p in {"this_month", "month"}:
        return today.replace(day=1), today
    if p == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1), last_prev
    if p in {"3m", "last_3_months"}:
        return (today.replace(day=1) - timedelta(days=62)).replace(day=1), today
    if p in {"6m", "last_6_months"}:
        return (today.replace(day=1) - timedelta(days=155)).replace(day=1), today
    if p in {"this_year", "year"}:
        return today.replace(month=1, day=1), today
    if p == "custom":
        return start_date, end_date
    return None, None


def _base_tx_query(
    db: Session,
    user_id: int,
    account_id: int | None = None,
    start_dt: date | None = None,
    end_dt: date | None = None,
):
    q = db.query(Transaction).filter(Transaction.user_id == user_id)
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if start_dt:
        q = q.filter(Transaction.tx_date >= start_dt)
    if end_dt:
        q = q.filter(Transaction.tx_date <= end_dt)
    return q.order_by(Transaction.tx_date.asc(), Transaction.id.asc())


def _is_excluded_from_income_expense(tx: Transaction, exclude_cc_payments: bool) -> bool:
    if tx.is_internal_transfer or tx.tx_type == "transfer":
        return True
    if exclude_cc_payments and tx.tx_type == "credit_card_payment":
        return True
    if tx.tx_type in {"loan_disbursement", "reversal"}:
        return True
    return False


def refresh_recurring_and_unusual_flags(db: Session, user_id: int) -> None:
    txs = (
        db.query(Transaction)
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.tx_date.asc())
        .all()
    )
    if not txs:
        return

    # Reset flags
    for tx in txs:
        tx.is_recurring = False
        tx.is_unusual = False

    # Recurring detection: same merchant/recipient appearing >=2 times across distinct months or regular intervals with similar amounts
    by_merchant: dict[str, list[Transaction]] = defaultdict(list)
    for tx in txs:
        if tx.debit > 0 and not tx.is_internal_transfer and tx.tx_type in {"expense", "credit_card_payment"}:
            key = (tx.merchant_or_recipient or tx.description[:25]).strip().upper()
            if key:
                by_merchant[key].append(tx)

    for _key, group in by_merchant.items():
        months = {t.tx_date.strftime("%Y-%m") for t in group}
        if len(group) >= 2 and len(months) >= 2:
            amounts = [t.debit for t in group]
            med = statistics.median(amounts)
            if med > 0 and all(abs(a - med) / med <= 0.20 for a in amounts):
                for t in group:
                    t.is_recurring = True
        elif any(t.category == "Subscriptions" for t in group) and len(group) >= 2:
            for t in group:
                t.is_recurring = True

    # Unusual expense detection: > mean + 2*std (with minimum threshold ₹2,000 and >=5 expenses)
    expense_amounts = [t.debit for t in txs if t.debit > 0 and t.tx_type == "expense" and not t.is_internal_transfer]
    if len(expense_amounts) >= 5:
        mean_val = statistics.mean(expense_amounts)
        stdev_val = statistics.stdev(expense_amounts)
        threshold = max(mean_val + 2.0 * stdev_val, statistics.median(expense_amounts) * 3.5, 2000.0)
        for t in txs:
            if t.debit >= threshold and t.tx_type == "expense" and not t.is_internal_transfer and t.category != "Rent":
                t.is_unusual = True

    db.commit()


def compute_dashboard_summary(
    db: Session,
    user_id: int,
    account_id: int | None = None,
    period: str = "all",
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
    exclude_cc = pref.exclude_credit_card_payments if pref else True

    start_dt, end_dt = resolve_date_range(period, start_date, end_date)
    txs = _base_tx_query(db, user_id, account_id, start_dt, end_dt).all()

    total_received = 0.0
    total_spent = 0.0
    total_refunds = 0.0
    highest_expense = 0.0
    highest_expense_tx: dict[str, Any] | None = None

    category_spending: dict[str, float] = defaultdict(float)
    category_income: dict[str, float] = defaultdict(float)
    monthly_map: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    daily_spending: dict[str, float] = defaultdict(float)
    payment_method_map: dict[str, float] = defaultdict(float)

    for tx in txs:
        month_key = tx.tx_date.strftime("%Y-%m")
        if tx.tx_type == "refund" or tx.category == "Refunds":
            total_refunds += tx.credit
            # Refunds reduce net expense or show separately; also count credit if non-transfer
            continue

        if _is_excluded_from_income_expense(tx, exclude_cc):
            continue

        if tx.credit > 0 and tx.tx_type == "income":
            total_received += tx.credit
            category_income[tx.category] += tx.credit
            monthly_map[month_key]["income"] += tx.credit

        if tx.debit > 0 and tx.tx_type == "expense":
            total_spent += tx.debit
            category_spending[tx.category] += tx.debit
            monthly_map[month_key]["expense"] += tx.debit
            daily_spending[tx.tx_date.isoformat()] += tx.debit
            payment_method_map[tx.payment_method] += tx.debit
            if tx.debit > highest_expense:
                highest_expense = tx.debit
                highest_expense_tx = {
                    "id": tx.id,
                    "tx_date": tx.tx_date.isoformat(),
                    "description": tx.description,
                    "merchant_or_recipient": tx.merchant_or_recipient,
                    "category": tx.category,
                    "amount": round(tx.debit, 2),
                }

    # Current closing balance from accounts (or latest running balance)
    acc_q = db.query(BankAccount).filter(BankAccount.user_id == user_id)
    if account_id:
        acc_q = acc_q.filter(BankAccount.id == account_id)
    accounts = acc_q.all()

    closing_balance = 0.0
    has_balance = False
    for acc in accounts:
        if acc.closing_balance is not None:
            closing_balance += acc.closing_balance
            has_balance = True
        else:
            latest_tx = (
                db.query(Transaction)
                .filter(Transaction.user_id == user_id, Transaction.account_id == acc.id, Transaction.running_balance.isnot(None))
                .order_by(Transaction.tx_date.desc(), Transaction.id.desc())
                .first()
            )
            if latest_tx and latest_tx.running_balance is not None:
                closing_balance += latest_tx.running_balance
                has_balance = True

    # Average daily spending over active date span in filtered transactions
    if txs:
        min_d = txs[0].tx_date
        max_d = txs[-1].tx_date
        span_days = max((max_d - min_d).days + 1, 1)
    elif start_dt and end_dt:
        span_days = max((end_dt - start_dt).days + 1, 1)
    else:
        span_days = 1
    avg_daily_spending = total_spent / span_days if txs else 0.0

    net_cash_flow = total_received - total_spent

    sorted_months = sorted(monthly_map.keys())
    monthly_series = [
        {
            "month": m,
            "income": round(monthly_map[m]["income"], 2),
            "expense": round(monthly_map[m]["expense"], 2),
            "net": round(monthly_map[m]["income"] - monthly_map[m]["expense"], 2),
        }
        for m in sorted_months
    ]

    expense_by_category = [
        {"category": k, "amount": round(v, 2)}
        for k, v in sorted(category_spending.items(), key=lambda x: x[1], reverse=True)
    ]
    income_by_category = [
        {"category": k, "amount": round(v, 2)}
        for k, v in sorted(category_income.items(), key=lambda x: x[1], reverse=True)
    ]
    daily_series = [
        {"date": d, "amount": round(daily_spending[d], 2)}
        for d in sorted(daily_spending.keys())
    ]
    payment_methods = [
        {"method": k, "amount": round(v, 2)}
        for k, v in sorted(payment_method_map.items(), key=lambda x: x[1], reverse=True)
    ]

    recent_transactions = [
        {
            "id": t.id,
            "tx_date": t.tx_date.isoformat(),
            "description": t.description,
            "merchant_or_recipient": t.merchant_or_recipient,
            "category": t.category,
            "tx_type": t.tx_type,
            "debit": round(t.debit, 2),
            "credit": round(t.credit, 2),
            "running_balance": t.running_balance,
            "payment_method": t.payment_method,
            "is_internal_transfer": t.is_internal_transfer,
        }
        for t in reversed(txs[-8:])
    ]

    return {
        "kpis": {
            "total_received": round(total_received, 2),
            "total_spent": round(total_spent, 2),
            "net_cash_flow": round(net_cash_flow, 2),
            "closing_balance": round(closing_balance, 2) if has_balance else None,
            "total_transactions": len(txs),
            "avg_daily_spending": round(avg_daily_spending, 2),
            "highest_single_expense": round(highest_expense, 2),
            "total_refunds": round(total_refunds, 2),
        },
        "highest_expense_tx": highest_expense_tx,
        "monthly_series": monthly_series,
        "expense_by_category": expense_by_category,
        "income_by_category": income_by_category,
        "daily_spending": daily_series,
        "payment_methods": payment_methods,
        "recent_transactions": recent_transactions,
        "period": period,
        "span_days": span_days,
    }


def compute_p2p_summary(
    db: Session,
    user_id: int,
    account_id: int | None = None,
) -> dict[str, Any]:
    q = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.p2p_person.isnot(None),
        Transaction.p2p_person != "",
    )
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    txs = q.order_by(Transaction.tx_date.desc()).all()

    people: dict[str, dict[str, Any]] = {}
    total_sent = 0.0
    total_received = 0.0

    for tx in txs:
        if tx.is_internal_transfer:
            continue
        name = tx.p2p_person.strip()
        if name not in people:
            people[name] = {
                "person": name,
                "sent": 0.0,
                "received": 0.0,
                "net_sent": 0.0,
                "tx_count": 0,
                "last_tx_date": tx.tx_date.isoformat(),
                "tags": set(),
                "transactions": [],
            }
        rec = people[name]
        rec["sent"] += tx.debit
        rec["received"] += tx.credit
        rec["tx_count"] += 1
        if tx.p2p_tag:
            rec["tags"].add(tx.p2p_tag)
        total_sent += tx.debit
        total_received += tx.credit
        rec["transactions"].append({
            "id": tx.id,
            "tx_date": tx.tx_date.isoformat(),
            "description": tx.description,
            "debit": round(tx.debit, 2),
            "credit": round(tx.credit, 2),
            "payment_method": tx.payment_method,
            "p2p_tag": tx.p2p_tag,
        })

    people_list = []
    for p in people.values():
        p["sent"] = round(p["sent"], 2)
        p["received"] = round(p["received"], 2)
        p["net_sent"] = round(p["sent"] - p["received"], 2)
        p["tags"] = sorted(p["tags"])
        people_list.append(p)

    most_frequent_recipients = sorted(
        [p for p in people_list if p["sent"] > 0],
        key=lambda x: (x["sent"], x["tx_count"]),
        reverse=True,
    )
    most_frequent_senders = sorted(
        [p for p in people_list if p["received"] > 0],
        key=lambda x: (x["received"], x["tx_count"]),
        reverse=True,
    )

    return {
        "total_sent": round(total_sent, 2),
        "total_received": round(total_received, 2),
        "net_p2p": round(total_sent - total_received, 2),
        "people": sorted(people_list, key=lambda x: x["sent"] + x["received"], reverse=True),
        "most_frequent_recipients": most_frequent_recipients[:5],
        "most_frequent_senders": most_frequent_senders[:5],
    }
