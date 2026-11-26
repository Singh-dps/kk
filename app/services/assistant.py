from __future__ import annotations

from collections import defaultdict
from datetime import date
import json
import re
from typing import Any
from sqlalchemy.orm import Session

from ..config import GEMINI_API_KEY
from ..models import Transaction, UserPreference
from ..security import sanitize_for_external_ai
from .constants import EXPENSE_CATEGORIES, INCOME_CATEGORIES


def _serialize_tx(t: Transaction) -> dict[str, Any]:
    return {
        "id": t.id,
        "tx_date": t.tx_date.isoformat(),
        "description": t.description,
        "merchant_or_recipient": t.merchant_or_recipient,
        "category": t.category,
        "tx_type": t.tx_type,
        "debit": round(t.debit, 2),
        "credit": round(t.credit, 2),
        "payment_method": t.payment_method,
    }


def answer_financial_question(
    db: Session,
    user_id: int,
    question: str,
    account_id: int | None = None,
) -> dict[str, Any]:
    pref = db.query(UserPreference).filter(UserPreference.user_id == user_id).first()
    exclude_cc = pref.exclude_credit_card_payments if pref else True
    enable_ai = pref.enable_ai_enrichment if pref else True

    q = db.query(Transaction).filter(Transaction.user_id == user_id)
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    all_txs = q.order_by(Transaction.tx_date.desc(), Transaction.id.desc()).all()

    if not all_txs:
        return {
            "answer": "You haven't imported any bank statements yet. Upload a PDF, CSV, or Excel statement so I can analyze your real transactions!",
            "supporting_transactions": [],
        }

    # Filter helper
    def valid_exp(t: Transaction) -> bool:
        return t.debit > 0 and t.tx_type == "expense" and not t.is_internal_transfer and not (exclude_cc and t.tx_type == "credit_card_payment")

    def valid_inc(t: Transaction) -> bool:
        return t.credit > 0 and t.tx_type == "income" and not t.is_internal_transfer

    ql = question.lower().strip()
    # Determine available months in user's records
    months = sorted({t.tx_date.strftime("%Y-%m") for t in all_txs})
    today_month = date.today().strftime("%Y-%m")
    active_month = today_month if today_month in months else months[-1]
    prev_month = months[-2] if len(months) >= 2 else None

    scope_txs = all_txs
    scope_label = "across all uploaded statements"
    if "this month" in ql:
        scope_txs = [t for t in all_txs if t.tx_date.strftime("%Y-%m") == active_month]
        scope_label = f"in {active_month}"
    elif "last month" in ql and prev_month:
        scope_txs = [t for t in all_txs if t.tx_date.strftime("%Y-%m") == prev_month]
        scope_label = f"in {prev_month}"

    # Check if user asked about a specific category
    matched_cat = None
    for cat in EXPENSE_CATEGORIES + INCOME_CATEGORIES:
        if cat.lower() in ql:
            matched_cat = cat
            break
    if not matched_cat and ("food" in ql or "dining" in ql or "swiggy" in ql or "zomato" in ql):
        matched_cat = "Food"

    supporting: list[dict[str, Any]] = []
    answer = ""

    # Intent routing backed strictly by DB numbers
    if "top 10" in ql or ("top" in ql and "expense" in ql):
        exps = sorted([t for t in scope_txs if valid_exp(t)], key=lambda x: x.debit, reverse=True)[:10]
        total_top = sum(t.debit for t in exps)
        lines = [f"{i+1}. {t.tx_date.isoformat()} — {t.merchant_or_recipient or t.description[:35]} ({t.category}): ₹{t.debit:,.2f}" for i, t in enumerate(exps)]
        answer = f"Here are your top {len(exps)} expenses {scope_label} (totaling ₹{total_top:,.2f}):\n" + "\n".join(lines)
        supporting = [_serialize_tx(t) for t in exps]

    elif "compare" in ql and ("month" in ql or "last" in ql):
        if len(months) < 2:
            m = months[0]
            inc = sum(t.credit for t in all_txs if valid_inc(t))
            exp = sum(t.debit for t in all_txs if valid_exp(t))
            answer = f"Currently only one month ({m}) is present in your records: Income ₹{inc:,.2f}, Spending ₹{exp:,.2f}, Net ₹{inc-exp:,.2f}. Upload another month's statement to compare."
        else:
            m1, m2 = months[-1], months[-2]
            t1 = [t for t in all_txs if t.tx_date.strftime("%Y-%m") == m1]
            t2 = [t for t in all_txs if t.tx_date.strftime("%Y-%m") == m2]
            inc1, exp1 = sum(t.credit for t in t1 if valid_inc(t)), sum(t.debit for t in t1 if valid_exp(t))
            inc2, exp2 = sum(t.credit for t in t2 if valid_inc(t)), sum(t.debit for t in t2 if valid_exp(t))
            diff_exp = exp1 - exp2
            pct_exp = (diff_exp / exp2 * 100) if exp2 > 0 else 0.0
            answer = (
                f"Comparison between **{m1}** and **{m2}**:\n"
                f"• Income: ₹{inc1:,.2f} ({m1}) vs ₹{inc2:,.2f} ({m2})\n"
                f"• Spending: ₹{exp1:,.2f} ({m1}) vs ₹{exp2:,.2f} ({m2}) — {pct_exp:+.1f}% change\n"
                f"• Net Cash Flow: ₹{inc1 - exp1:,.2f} ({m1}) vs ₹{inc2 - exp2:,.2f} ({m2})"
            )
            supporting = [_serialize_tx(t) for t in sorted([x for x in t1 if valid_exp(x)], key=lambda x: x.debit, reverse=True)[:5]]

    elif "recurring" in ql or "subscription" in ql:
        rec = [t for t in all_txs if (t.is_recurring or t.category == "Subscriptions") and t.debit > 0]
        if not rec:
            answer = "No recurring payments or subscriptions were detected in your imported transactions yet."
        else:
            by_m: dict[str, list[Transaction]] = defaultdict(list)
            for t in rec:
                by_m[t.merchant_or_recipient or t.description[:30]].append(t)
            lines = [f"• {m}: {len(ts)} payments, avg ₹{sum(x.debit for x in ts)/len(ts):,.2f} (total ₹{sum(x.debit for x in ts):,.2f})" for m, ts in sorted(by_m.items(), key=lambda kv: sum(x.debit for x in kv[1]), reverse=True)]
            answer = f"Found {len(by_m)} recurring payments/subscriptions:\n" + "\n".join(lines)
            supporting = [_serialize_tx(t) for t in rec[:10]]

    elif "who" in ql and ("transfer" in ql or "sent" in ql or "most" in ql):
        p2p = [t for t in scope_txs if t.p2p_person and t.debit > 0 and not t.is_internal_transfer]
        if not p2p:
            answer = f"No person-to-person transfers were identified {scope_label}."
        else:
            totals: dict[str, float] = defaultdict(float)
            counts: dict[str, int] = defaultdict(int)
            for t in p2p:
                totals[t.p2p_person] += t.debit
                counts[t.p2p_person] += 1
            ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)
            top_person, top_amt = ranked[0]
            lines = [f"• {p}: ₹{amt:,.2f} ({counts[p]} txns)" for p, amt in ranked[:5]]
            answer = f"You transferred the most money to **{top_person}** (₹{top_amt:,.2f} across {counts[top_person]} transactions) {scope_label}.\nTop recipients:\n" + "\n".join(lines)
            supporting = [_serialize_tx(t) for t in p2p if t.p2p_person == top_person][:8]

    elif "biggest" in ql or "largest" in ql or "highest" in ql:
        exps = [t for t in scope_txs if valid_exp(t)]
        if not exps:
            answer = f"No expense transactions found {scope_label}."
        else:
            big = max(exps, key=lambda x: x.debit)
            answer = f"Your biggest single expense {scope_label} was **₹{big.debit:,.2f}** on {big.tx_date.isoformat()} for **{big.merchant_or_recipient or big.description}** ({big.category})."
            supporting = [_serialize_tx(big)]

    elif matched_cat:
        cat_txs = [t for t in scope_txs if t.category.lower() == matched_cat.lower() and not t.is_internal_transfer]
        if matched_cat == "Food":
            # Include both Food and Restaurants if user asks broadly about food, while clarifying breakdown
            food_only = [t for t in scope_txs if t.category == "Food" and valid_exp(t)]
            rest_only = [t for t in scope_txs if t.category == "Restaurants" and valid_exp(t)]
            f_sum = sum(t.debit for t in food_only)
            r_sum = sum(t.debit for t in rest_only)
            answer = f"You spent **₹{f_sum:,.2f}** on Food ({len(food_only)} txns) and **₹{r_sum:,.2f}** on Restaurants ({len(rest_only)} txns) {scope_label} — combined **₹{f_sum + r_sum:,.2f}**."
            supporting = [_serialize_tx(t) for t in sorted(food_only + rest_only, key=lambda x: x.debit, reverse=True)[:10]]
        else:
            tot = sum(t.debit if t.debit > 0 else t.credit for t in cat_txs)
            answer = f"Total for **{matched_cat}** {scope_label}: **₹{tot:,.2f}** across {len(cat_txs)} transactions."
            supporting = [_serialize_tx(t) for t in cat_txs[:10]]

    elif "where did most" in ql or "most of my money" in ql or "breakdown" in ql:
        exps = [t for t in scope_txs if valid_exp(t)]
        cat_totals: dict[str, float] = defaultdict(float)
        for t in exps:
            cat_totals[t.category] += t.debit
        total_e = sum(cat_totals.values())
        ranked = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
        lines = [f"• {c}: ₹{v:,.2f} ({(v/total_e*100):.1f}%)" for c, v in ranked[:6]]
        answer = f"Your total spending {scope_label} is **₹{total_e:,.2f}**. Top categories:\n" + "\n".join(lines)
        if ranked:
            top_c = ranked[0][0]
            supporting = [_serialize_tx(t) for t in exps if t.category == top_c][:8]

    elif "reduce" in ql or "save more" in ql or "advice" in ql:
        exps = [t for t in scope_txs if valid_exp(t)]
        cat_totals: dict[str, float] = defaultdict(float)
        for t in exps:
            cat_totals[t.category] += t.debit
        ranked = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
        discretionary = [x for x in ranked if x[0] in {"Restaurants", "Food", "Shopping", "Entertainment", "Subscriptions", "Travel"}]
        rec_txs = [t for t in scope_txs if t.is_recurring and t.debit > 0]
        tips = []
        if discretionary:
            c, v = discretionary[0]
            tips.append(f"1. **{c}** is your largest discretionary spend at ₹{v:,.2f} — trimming it by 15% saves ₹{v*0.15:,.0f}.")
        if len(discretionary) > 1:
            c2, v2 = discretionary[1]
            tips.append(f"2. **{c2}** accounts for ₹{v2:,.2f}; setting a monthly cap could free ₹{v2*0.15:,.0f}.")
        if rec_txs:
            tips.append(f"3. Audit your {len(rec_txs)} recurring payments/subscriptions (₹{sum(t.debit for t in rec_txs):,.2f} total).")
        if not tips and ranked:
            tips.append(f"Your biggest category is {ranked[0][0]} (₹{ranked[0][1]:,.2f}). Review top transactions there for savings opportunities.")
        answer = f"Based on your actual transactions {scope_label}, here are concrete ways to reduce expenses:\n" + "\n".join(tips)
        supporting = [_serialize_tx(t) for t in sorted(exps, key=lambda x: x.debit, reverse=True)[:6]]

    elif "more than i earn" in ql or "save" in ql or "received" in ql or "earn" in ql or "spend" in ql or "spent" in ql:
        inc = sum(t.credit for t in scope_txs if valid_inc(t))
        exp = sum(t.debit for t in scope_txs if valid_exp(t))
        net = inc - exp
        rate = (net / inc * 100) if inc > 0 else 0.0
        if "more than i earn" in ql:
            verdict = "Yes, you are spending more than you earn" if exp > inc else "No, you are living within your income"
            answer = f"{verdict} {scope_label}: Income **₹{inc:,.2f}** vs Spending **₹{exp:,.2f}** (Net: ₹{net:,.2f})."
        elif "save" in ql:
            answer = f"Your net savings {scope_label} is **₹{net:,.2f}** ({rate:.1f}% savings rate) — derived from ₹{inc:,.2f} received minus ₹{exp:,.2f} spent."
        elif "receive" in ql or "earn" in ql:
            inc_txs = [t for t in scope_txs if valid_inc(t)]
            answer = f"You received **₹{inc:,.2f}** across {len(inc_txs)} income transactions {scope_label}."
            supporting = [_serialize_tx(t) for t in inc_txs[:8]]
        else:
            exp_txs = [t for t in scope_txs if valid_exp(t)]
            answer = f"You spent **₹{exp:,.2f}** across {len(exp_txs)} expense transactions {scope_label}."
            supporting = [_serialize_tx(t) for t in sorted(exp_txs, key=lambda x: x.debit, reverse=True)[:8]]

    else:
        # General query: search matching descriptions/merchants or provide sanitized summary
        tokens = [w for w in re.findall(r"[a-zA-Z0-9]+", ql) if len(w) >= 3 and w not in {"what", "how", "much", "did", "was", "my", "the", "for", "show", "tell", "about", "with", "this", "last", "month"}]
        matched = []
        if tokens:
            for t in scope_txs:
                hay = f"{t.description} {t.merchant_or_recipient or ''} {t.category} {t.p2p_person or ''}".lower()
                if any(tok in hay for tok in tokens):
                    matched.append(t)
        if matched:
            tot_d = sum(t.debit for t in matched)
            tot_c = sum(t.credit for t in matched)
            answer = f"Found {len(matched)} matching transactions {scope_label} (Debits: ₹{tot_d:,.2f}, Credits: ₹{tot_c:,.2f})."
            supporting = [_serialize_tx(t) for t in matched[:10]]
        else:
            inc = sum(t.credit for t in scope_txs if valid_inc(t))
            exp = sum(t.debit for t in scope_txs if valid_exp(t))
            answer = (
                f"Summary {scope_label}: Received **₹{inc:,.2f}**, Spent **₹{exp:,.2f}**, Net **₹{inc - exp:,.2f}** across {len(scope_txs)} transactions. "
                "Ask about specific categories (Food, Shopping, Travel), top 10 expenses, recurring subscriptions, or P2P transfers!"
            )
            supporting = [_serialize_tx(t) for t in scope_txs[:6]]

    # Optional Gemini phrasing enhancement on strictly aggregated/sanitized stats (never raw PII)
    if enable_ai and GEMINI_API_KEY and supporting:
        try:
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY)
            safe_facts = sanitize_for_external_ai(answer)
            prompt = (
                "You are KHARCH AI, a concise personal finance assistant for Indian users. "
                "Rewrite/refine the verified factual answer below in a helpful, crisp tone without altering ANY numbers or inventing any data.\n"
                f"Question: {sanitize_for_external_ai(question)}\n"
                f"Verified facts:\n{safe_facts}"
            )
            resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            if resp.text and len(resp.text.strip()) > 10:
                answer = resp.text.strip()
        except Exception:
            pass

    return {
        "answer": answer,
        "supporting_transactions": supporting,
    }
