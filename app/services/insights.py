from __future__ import annotations

from collections import defaultdict
import json
from typing import Any
from sqlalchemy.orm import Session

from ..models import AIInsight, Transaction, UserPreference


def generate_and_save_insights(db: Session, user_id: int, account_id: int | None = None) -> list[dict[str, Any]]:
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
    exclude_cc = pref.exclude_credit_card_payments if pref else True

    q = db.query(Transaction).filter(Transaction.user_id == user_id)
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    txs = q.order_by(Transaction.tx_date.asc()).all()

    # Remove old insights for this scope
    del_q = db.query(AIInsight).filter(AIInsight.user_id == user_id)
    if account_id is None:
        del_q = del_q.filter(AIInsight.account_id.is_(None))
    else:
        del_q = del_q.filter(AIInsight.account_id == account_id)
    del_q.delete(synchronize_session=False)

    if not txs:
        db.commit()
        return []

    expenses = [
        t for t in txs
        if t.debit > 0
        and not t.is_internal_transfer
        and t.tx_type == "expense"
        and not (exclude_cc and t.tx_type == "credit_card_payment")
    ]
    incomes = [t for t in txs if t.credit > 0 and t.tx_type == "income" and not t.is_internal_transfer]

    insights: list[AIInsight] = []

    def add_insight(itype: str, severity: str, title: str, summary: str, metric: str | None = None, meta: dict | None = None):
        insights.append(
            AIInsight(
                user_id=user_id,
                account_id=account_id,
                insight_type=itype,
                severity=severity,
                title=title,
                summary=summary,
                metric_value=metric,
                metadata_json=json.dumps(meta) if meta else None,
            )
        )

    if expenses:
        # 1. Highest spending category
        cat_totals: dict[str, float] = defaultdict(float)
        cat_counts: dict[str, int] = defaultdict(int)
        for t in expenses:
            cat_totals[t.category] += t.debit
            cat_counts[t.category] += 1
        top_cat, top_cat_amt = max(cat_totals.items(), key=lambda x: x[1])
        total_exp = sum(cat_totals.values())
        share = (top_cat_amt / total_exp * 100) if total_exp else 0
        add_insight(
            "highest_category",
            "info",
            f"Top Expense Category: {top_cat}",
            f"You spent ₹{top_cat_amt:,.0f} on {top_cat} across {cat_counts[top_cat]} transactions ({share:.1f}% of total spending).",
            f"₹{top_cat_amt:,.0f}",
        )

        # 2. Most expensive day
        day_totals: dict[str, float] = defaultdict(float)
        for t in expenses:
            day_totals[t.tx_date.isoformat()] += t.debit
        top_day, top_day_amt = max(day_totals.items(), key=lambda x: x[1])
        add_insight(
            "most_expensive_day",
            "warning",
            "Most Expensive Day",
            f"Your highest single-day spend was ₹{top_day_amt:,.0f} on {top_day}.",
            f"₹{top_day_amt:,.0f}",
        )

        # 3. Most frequent merchant
        merch_counts: dict[str, int] = defaultdict(int)
        merch_totals: dict[str, float] = defaultdict(float)
        for t in expenses:
            m = t.merchant_or_recipient or t.description[:30]
            merch_counts[m] += 1
            merch_totals[m] += t.debit
        top_merch, top_merch_cnt = max(merch_counts.items(), key=lambda x: (x[1], merch_totals[x[0]]))
        if top_merch_cnt >= 2:
            add_insight(
                "frequent_merchant",
                "info",
                f"Most Frequent Merchant: {top_merch}",
                f"You made {top_merch_cnt} payments to {top_merch} totaling ₹{merch_totals[top_merch]:,.0f}.",
                f"{top_merch_cnt} txns",
            )

        # 4. Biggest individual expense
        biggest = max(expenses, key=lambda t: t.debit)
        add_insight(
            "biggest_expense",
            "warning",
            "Biggest Single Expense",
            f"Your largest expense was ₹{biggest.debit:,.0f} at {biggest.merchant_or_recipient or biggest.description[:40]} on {biggest.tx_date.isoformat()} ({biggest.category}).",
            f"₹{biggest.debit:,.0f}",
        )

        # 5. Unusual transactions
        unusual = [t for t in expenses if t.is_unusual]
        if unusual:
            top_u = max(unusual, key=lambda t: t.debit)
            add_insight(
                "unusual_transactions",
                "alert",
                f"{len(unusual)} Unusual Transaction{'s' if len(unusual) > 1 else ''} Detected",
                f"Notable outlier: ₹{top_u.debit:,.0f} for {top_u.merchant_or_recipient or top_u.description[:40]} on {top_u.tx_date.isoformat()}, significantly above your typical spend.",
                f"{len(unusual)} flagged",
            )

        # 6. Repeated subscriptions / recurring payments
        recurring = [t for t in txs if t.is_recurring and t.debit > 0]
        if recurring:
            rec_merchants = sorted({t.merchant_or_recipient or t.description[:25] for t in recurring})
            rec_total = sum(t.debit for t in recurring)
            add_insight(
                "recurring_subscriptions",
                "info",
                "Recurring Payments & Subscriptions",
                f"Detected {len(rec_merchants)} recurring commitments ({', '.join(rec_merchants[:5])}) totaling ₹{rec_total:,.0f}.",
                f"{len(rec_merchants)} active",
            )

        # 7. Monthly comparison & changes in spending habits
        monthly_exp: dict[str, float] = defaultdict(float)
        monthly_cat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for t in expenses:
            mk = t.tx_date.strftime("%Y-%m")
            monthly_exp[mk] += t.debit
            monthly_cat[mk][t.category] += t.debit

        months = sorted(monthly_exp.keys())
        if len(months) >= 2:
            cur_m, prev_m = months[-1], months[-2]
            cur_v, prev_v = monthly_exp[cur_m], monthly_exp[prev_m]
            if prev_v > 0:
                pct = ((cur_v - prev_v) / prev_v) * 100
                direction = "higher" if pct > 0 else "lower"
                sev = "warning" if pct > 10 else "positive"
                add_insight(
                    "monthly_spending_change",
                    sev,
                    f"Month-over-Month Spend ({cur_m} vs {prev_m})",
                    f"Total spending in {cur_m} (₹{cur_v:,.0f}) is {abs(pct):.1f}% {direction} than {prev_m} (₹{prev_v:,.0f}).",
                    f"{pct:+.1f}%",
                )

            # Specific category spike
            best_spike = None
            for cat, c_amt in monthly_cat[cur_m].items():
                p_amt = monthly_cat[prev_m].get(cat, 0.0)
                if p_amt >= 500 and c_amt > p_amt:
                    sp_pct = ((c_amt - p_amt) / p_amt) * 100
                    if not best_spike or sp_pct > best_spike[0]:
                        best_spike = (sp_pct, cat, c_amt, p_amt)
            if best_spike and best_spike[0] >= 15:
                sp_pct, cat, c_amt, p_amt = best_spike
                add_insight(
                    "category_habit_shift",
                    "warning",
                    f"{cat} Spend Increased",
                    f"You spent ₹{c_amt:,.0f} on {cat} in {cur_m}, which is {sp_pct:.0f}% higher than {prev_m} (₹{p_amt:,.0f}).",
                    f"+{sp_pct:.0f}%",
                )

    # 8. Income vs expenditure trend
    if incomes and expenses:
        tot_inc = sum(t.credit for t in incomes)
        tot_exp = sum(t.debit for t in expenses)
        savings = tot_inc - tot_exp
        rate = (savings / tot_inc * 100) if tot_inc > 0 else 0.0
        if savings >= 0:
            add_insight(
                "income_vs_expense",
                "positive",
                "Healthy Cash Flow Surplus",
                f"You received ₹{tot_inc:,.0f} and spent ₹{tot_exp:,.0f}, retaining ₹{savings:,.0f} ({rate:.1f}% savings rate).",
                f"{rate:.1f}% saved",
            )
        else:
            add_insight(
                "income_vs_expense",
                "alert",
                "Spending Exceeds Income",
                f"Your expenses (₹{tot_exp:,.0f}) exceeded your recorded income (₹{tot_inc:,.0f}) by ₹{abs(savings):,.0f}.",
                f"-₹{abs(savings):,.0f}",
            )

    for ins in insights:
        db.add(ins)
    db.commit()

    return [
        {
            "id": ins.id,
            "insight_type": ins.insight_type,
            "severity": ins.severity,
            "title": ins.title,
            "summary": ins.summary,
            "metric_value": ins.metric_value,
        }
        for ins in insights
    ]
