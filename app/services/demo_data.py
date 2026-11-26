from __future__ import annotations

from datetime import date, timedelta
from sqlalchemy.orm import Session

from .analytics import refresh_recurring_and_unusual_flags
from .categorizer import classify_transaction_deterministic, compute_fingerprint
from .insights import generate_and_save_insights
from ..models import BankAccount, Transaction, UploadedStatement
from .parser import detect_payment_method, extract_reference_number


def load_demo_dataset(db: Session, user_id: int) -> dict[str, int]:
    """Explicitly load clearly marked Demo Mode data (never called automatically)."""
    # Avoid duplicating demo data if already loaded
    existing_demo = db.query(BankAccount).filter(BankAccount.user_id == user_id, BankAccount.is_demo.is_(True)).first()
    if existing_demo:
        tx_count = db.query(Transaction).filter(Transaction.user_id == user_id, Transaction.is_demo.is_(True)).count()
        return {"accounts_created": 0, "transactions_created": tx_count}

    hdfc = BankAccount(
        user_id=user_id,
        bank_name="HDFC Bank",
        nickname="HDFC Salary Account (DEMO)",
        account_type="Savings",
        masked_number="XXXX4821",
        closing_balance=148250.50,
        is_demo=True,
    )
    icici = BankAccount(
        user_id=user_id,
        bank_name="ICICI Bank",
        nickname="ICICI Everyday Spend (DEMO)",
        account_type="Savings",
        masked_number="XXXX9014",
        closing_balance=34180.00,
        is_demo=True,
    )
    db.add_all([hdfc, icici])
    db.flush()

    today = date.today()
    cur_m_1 = today.replace(day=1)
    prev_m_last = cur_m_1 - timedelta(days=1)
    prev_m_1 = prev_m_last.replace(day=1)

    stmt1 = UploadedStatement(
        user_id=user_id,
        account_id=hdfc.id,
        file_name="DEMO_HDFC_Savings_Statement.csv",
        file_type="csv",
        file_size=14280,
        status="confirmed",
        detected_count=18,
        imported_count=18,
        opening_balance=92000.00,
        closing_balance=148250.50,
        period_start=prev_m_1,
        period_end=today,
        parser_notes="Demo Mode dataset loaded by user request.",
        is_demo=True,
    )
    stmt2 = UploadedStatement(
        user_id=user_id,
        account_id=icici.id,
        file_name="DEMO_ICICI_Statement.xlsx",
        file_type="xlsx",
        file_size=18400,
        status="confirmed",
        detected_count=10,
        imported_count=10,
        opening_balance=21500.00,
        closing_balance=34180.00,
        period_start=prev_m_1,
        period_end=today,
        parser_notes="Demo Mode dataset loaded by user request.",
        is_demo=True,
    )
    db.add_all([stmt1, stmt2])
    db.flush()

    def d_prev(day: int) -> date:
        return prev_m_1.replace(day=min(day, prev_m_last.day))

    def d_cur(day: int) -> date:
        return min(cur_m_1.replace(day=min(day, 28)), today)

    hdfc_rows = [
        (d_prev(1), "NEFT/SALARY/ACME TECH INDIA PVT LTD", 0.0, 95000.00, 187000.00),
        (d_prev(3), "UPI/DR/331244556611/NOBROKER RENT/HDFC", 24000.00, 0.0, 163000.00),
        (d_prev(5), "UPI/DR/331244556622/BESCOM ELECTRICITY BILL", 2150.00, 0.0, 160850.00),
        (d_prev(7), "UPI/DR/331244556633/NETFLIX INDIA SUBSCRIPTION", 649.00, 0.0, 160201.00),
        (d_prev(8), "UPI/DR/331244556644/SPOTIFY INDIA AUTOPAY", 119.00, 0.0, 160082.00),
        (d_prev(11), "UPI/DR/331244556655/SWIGGY FOOD ORDER", 680.00, 0.0, 159402.00),
        (d_prev(14), "UPI/DR/331244556666/RAHUL SHARMA/SBIN/Dinner split", 3000.00, 0.0, 156402.00),
        (d_prev(18), "IMPS/CR/331244556677/RAHUL SHARMA/Repayment", 0.0, 2000.00, 158402.00),
        (d_prev(22), "POS/HPCL FUEL STATION BENGALURU", 3200.00, 0.0, 155202.00),
        (d_cur(1), "NEFT/SALARY/ACME TECH INDIA PVT LTD", 0.0, 95000.00, 250202.00),
        (d_cur(2), "IMPS/DR/SELF TRANSFER TO ICICI XX9014", 20000.00, 0.0, 230202.00),
        (d_cur(3), "UPI/DR/441244556711/NOBROKER RENT/HDFC", 24000.00, 0.0, 206202.00),
        (d_cur(5), "UPI/DR/441244556722/BESCOM ELECTRICITY BILL", 2420.00, 0.0, 203782.00),
        (d_cur(7), "UPI/DR/441244556733/NETFLIX INDIA SUBSCRIPTION", 649.00, 0.0, 203133.00),
        (d_cur(8), "UPI/DR/441244556744/SPOTIFY INDIA AUTOPAY", 119.00, 0.0, 203014.00),
        (d_cur(10), "POS/APPLE INDIA STORE MACBOOK ACCESSORY", 42500.00, 0.0, 160514.00),
        (d_cur(12), "NEFT/CR/FREELANCE DESIGN INVOICE 104", 0.0, 18500.00, 179014.00),
        (d_cur(14), "CRED CLUB CREDIT CARD BILL PAYMENT", 30763.50, 0.0, 148250.50),
    ]

    icici_rows = [
        (d_prev(6), "UPI/DR/551244550001/ZOMATO ORDER", 540.00, 0.0, 20960.00),
        (d_prev(12), "UPI/DR/551244550002/BLINKIT GROCERIES", 1420.00, 0.0, 19540.00),
        (d_prev(16), "UPI/DR/551244550003/UBER TRIP", 480.00, 0.0, 19060.00),
        (d_prev(20), "UPI/DR/551244550004/AMAZON INDIA SHOPPING", 3890.00, 0.0, 15170.00),
        (d_cur(2), "IMPS/CR/SELF TRANSFER FROM HDFC XX4821", 0.0, 20000.00, 35170.00),
        (d_cur(4), "UPI/DR/661244550011/SWIGGY INSTAMART", 1850.00, 0.0, 33320.00),
        (d_cur(6), "UPI/DR/661244550012/ZOMATO DINNER", 920.00, 0.0, 32400.00),
        (d_cur(9), "UPI/DR/661244550013/RAHUL SHARMA/SBIN/Trip advance", 2000.00, 0.0, 30400.00),
        (d_cur(11), "UPI/CR/661244550014/AMAZON REFUND ORDER 402", 0.0, 1290.00, 31690.00),
        (d_cur(13), "UPI/CR/661244550015/PRIYA VERMA/HDFC/Concert ticket", 0.0, 2490.00, 34180.00),
    ]

    masks = [hdfc.masked_number, icici.masked_number]
    count = 0
    for acc, stmt, rows in [(hdfc, stmt1, hdfc_rows), (icici, stmt2, icici_rows)]:
        for tx_dt, desc, deb, cred, bal in rows:
            pm = detect_payment_method(desc)
            ref = extract_reference_number(desc)
            cls = classify_transaction_deterministic(desc, deb, cred, pm, masks)
            fp = compute_fingerprint(tx_dt.isoformat(), deb, cred, desc, ref)
            db.add(
                Transaction(
                    user_id=user_id,
                    account_id=acc.id,
                    statement_id=stmt.id,
                    tx_date=tx_dt,
                    description=desc,
                    merchant_or_recipient=cls["merchant_or_recipient"],
                    category=cls["category"],
                    tx_type=cls["tx_type"],
                    debit=deb,
                    credit=cred,
                    running_balance=bal,
                    payment_method=pm,
                    reference_number=ref,
                    is_internal_transfer=cls["is_internal_transfer"],
                    p2p_person=cls["p2p_person"],
                    fingerprint=fp,
                    is_demo=True,
                )
            )
            count += 1

    db.commit()
    refresh_recurring_and_unusual_flags(db, user_id)
    generate_and_save_insights(db, user_id)
    return {"accounts_created": 2, "transactions_created": count}
