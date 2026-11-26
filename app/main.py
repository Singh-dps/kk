from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import re
import uuid
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from .config import ALLOWED_ORIGINS, IS_VERCEL, MAX_UPLOAD_SIZE_BYTES, UPLOAD_DIR
from .database import Base, engine, get_db
from .models import (
    AIInsight,
    BankAccount,
    Category,
    ChatMessage,
    MonthlyReport,
    Transaction,
    UploadedStatement,
    User,
    UserPreference,
)
from .schemas import (
    AuthResponse,
    BankAccountCreate,
    BankAccountUpdate,
    ChatRequest,
    ConfirmImportRequest,
    LoginRequest,
    PreferencesUpdateRequest,
    SignupRequest,
    TransactionUpdateRequest,
)
from .security import (
    create_access_token,
    get_current_user,
    hash_password,
    mask_account_number,
    verify_password,
)
from .services.analytics import (
    compute_dashboard_summary,
    compute_p2p_summary,
    refresh_recurring_and_unusual_flags,
    resolve_date_range,
)
from .services.assistant import answer_financial_question
from .services.categorizer import (
    classify_transaction_deterministic,
    compute_fingerprint,
    detect_duplicates_and_transfers,
    enrich_ambiguous_with_gemini,
)
from .services.constants import ALL_CATEGORIES, EXPENSE_CATEGORIES, INCOME_CATEGORIES, INDIAN_BANKS
from .services.demo_data import load_demo_dataset
from .services.insights import generate_and_save_insights
from .services.parser import parse_statement_file
from .services.reports import build_monthly_report, export_report_csv, export_report_pdf

Base.metadata.create_all(bind=engine)


def _ensure_schema_columns():
    try:
        insp = inspect(engine)
        if "uploaded_statements" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("uploaded_statements")}
            if "file_blob" not in cols:
                col_type = "BYTEA" if not str(engine.url).startswith("sqlite") else "BLOB"
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE uploaded_statements ADD COLUMN file_blob {col_type}"))
    except Exception:
        pass


_ensure_schema_columns()

app = FastAPI(
    title="KHARCH KAROD API",
    description="AI-Powered Bank Statement Analyzer & Personal Finance Automation",
    version="1.0.0",
)

_origin_list = [o.strip().rstrip("/") for o in ALLOWED_ORIGINS.split(",") if o.strip() and o.strip() != "*"]
if _origin_list:
    _cors_origins = list(dict.fromkeys(_origin_list + ["http://localhost:5173", "http://127.0.0.1:5173"]))
    _cors_regex = r"https://.*\.vercel\.app"
else:
    _cors_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    _cors_regex = r"https://.*\.vercel\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins if ALLOWED_ORIGINS.strip() != "*" else ["*"],
    allow_origin_regex=None if ALLOWED_ORIGINS.strip() == "*" else _cors_regex,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_EXTENSIONS = {".pdf", ".csv", ".xlsx", ".xls"}


def _seed_user_defaults(db: Session, user: User) -> None:
    pref = UserPreference(user_id=user.id)
    db.add(pref)
    for cname in INCOME_CATEGORIES:
        db.add(Category(user_id=user.id, name=cname, kind="income", is_default=True))
    for cname in EXPENSE_CATEGORIES:
        db.add(Category(user_id=user.id, name=cname, kind="expense", is_default=True))
    db.commit()


def _serialize_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "created_at": user.created_at.isoformat(),
    }


def _serialize_account(acc: BankAccount, db: Session) -> dict[str, Any]:
    tx_count = db.query(Transaction).filter(Transaction.account_id == acc.id).count()
    stmt_count = db.query(UploadedStatement).filter(UploadedStatement.account_id == acc.id).count()
    return {
        "id": acc.id,
        "bank_name": acc.bank_name,
        "nickname": acc.nickname,
        "account_type": acc.account_type,
        "masked_number": acc.masked_number,
        "closing_balance": acc.closing_balance,
        "is_demo": acc.is_demo,
        "transaction_count": tx_count,
        "statement_count": stmt_count,
        "created_at": acc.created_at.isoformat(),
    }


def _serialize_statement(st: UploadedStatement) -> dict[str, Any]:
    return {
        "id": st.id,
        "account_id": st.account_id,
        "account_name": st.account.nickname if st.account else None,
        "bank_name": st.account.bank_name if st.account else None,
        "file_name": st.file_name,
        "file_type": st.file_type,
        "file_size": st.file_size,
        "upload_date": st.upload_date.isoformat(),
        "status": st.status,
        "detected_count": st.detected_count,
        "imported_count": st.imported_count,
        "opening_balance": st.opening_balance,
        "closing_balance": st.closing_balance,
        "period_start": st.period_start.isoformat() if st.period_start else None,
        "period_end": st.period_end.isoformat() if st.period_end else None,
        "parser_notes": st.parser_notes,
        "is_demo": st.is_demo,
    }


def _serialize_tx(t: Transaction) -> dict[str, Any]:
    return {
        "id": t.id,
        "account_id": t.account_id,
        "account_name": t.account.nickname if t.account else None,
        "statement_id": t.statement_id,
        "tx_date": t.tx_date.isoformat(),
        "description": t.description,
        "merchant_or_recipient": t.merchant_or_recipient,
        "category": t.category,
        "tx_type": t.tx_type,
        "debit": round(t.debit, 2),
        "credit": round(t.credit, 2),
        "running_balance": t.running_balance,
        "payment_method": t.payment_method,
        "reference_number": t.reference_number,
        "is_internal_transfer": t.is_internal_transfer,
        "is_duplicate_flagged": t.is_duplicate_flagged,
        "is_recurring": t.is_recurring,
        "is_unusual": t.is_unusual,
        "p2p_person": t.p2p_person,
        "p2p_tag": t.p2p_tag,
        "notes": t.notes,
        "is_demo": t.is_demo,
    }


@app.get("/")
def root_health():
    return {"status": "ok", "app": "KHARCH KAROD", "docs": "/docs"}


@app.get("/api/health")
def health_check():
    return {"status": "ok", "app": "KHARCH KAROD", "vercel": IS_VERCEL}


# ---------------- AUTH ----------------
@app.post("/api/auth/signup", response_model=AuthResponse)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    email = payload.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email is already registered")
    user = User(
        full_name=payload.full_name.strip(),
        email=email,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    _seed_user_defaults(db, user)
    token = create_access_token(user.id, user.email)
    return {"access_token": token, "token_type": "bearer", "user": _serialize_user(user)}


@app.post("/api/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    email = payload.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token(user.id, user.email)
    return {"access_token": token, "token_type": "bearer", "user": _serialize_user(user)}


@app.get("/api/auth/me")
def get_me(current_user: User = Depends(get_current_user)):
    return _serialize_user(current_user)


# ---------------- BANK ACCOUNTS ----------------
@app.get("/api/banks")
def list_supported_banks():
    return {"banks": INDIAN_BANKS}


@app.get("/api/accounts")
def list_accounts(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    accounts = (
        db.query(BankAccount)
        .filter(BankAccount.user_id == current_user.id)
        .order_by(BankAccount.created_at.desc())
        .all()
    )
    return {"accounts": [_serialize_account(a, db) for a in accounts]}


@app.post("/api/accounts")
def create_account(
    payload: BankAccountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    acc = BankAccount(
        user_id=current_user.id,
        bank_name=payload.bank_name.strip(),
        nickname=payload.nickname.strip(),
        account_type=payload.account_type.strip(),
        masked_number=mask_account_number(payload.account_number),
        closing_balance=payload.closing_balance,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return _serialize_account(acc, db)


@app.put("/api/accounts/{account_id}")
def update_account(
    account_id: int,
    payload: BankAccountUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    acc = db.query(BankAccount).filter(BankAccount.id == account_id, BankAccount.user_id == current_user.id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Bank account not found")
    if payload.bank_name is not None:
        acc.bank_name = payload.bank_name.strip()
    if payload.nickname is not None:
        acc.nickname = payload.nickname.strip()
    if payload.account_type is not None:
        acc.account_type = payload.account_type.strip()
    if payload.account_number is not None:
        acc.masked_number = mask_account_number(payload.account_number)
    if payload.closing_balance is not None:
        acc.closing_balance = payload.closing_balance
    db.commit()
    db.refresh(acc)
    return _serialize_account(acc, db)


@app.delete("/api/accounts/{account_id}")
def delete_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    acc = db.query(BankAccount).filter(BankAccount.id == account_id, BankAccount.user_id == current_user.id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="Bank account not found")
    for st in acc.statements:
        if st.storage_path:
            p = Path(st.storage_path)
            if p.exists():
                p.unlink(missing_ok=True)
    db.delete(acc)
    db.commit()
    refresh_recurring_and_unusual_flags(db, current_user.id)
    generate_and_save_insights(db, current_user.id)
    return {"deleted": True}


# ---------------- STATEMENT UPLOAD & ANALYSIS ----------------
@app.get("/api/statements")
def list_statements(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    stmts = (
        db.query(UploadedStatement)
        .filter(UploadedStatement.user_id == current_user.id)
        .order_by(UploadedStatement.upload_date.desc())
        .all()
    )
    return {"statements": [_serialize_statement(s) for s in stmts]}


@app.post("/api/statements/upload")
async def upload_statements(
    files: list[UploadFile] = File(...),
    account_id: int | None = Form(None),
    bank_name: str = Form("Primary Bank"),
    account_nickname: str = Form("Main Account"),
    account_type: str = Form("Savings"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not files:
        raise HTTPException(status_code=400, detail="Please select at least one statement file")

    # Resolve or auto-create target bank account
    if account_id:
        acc = db.query(BankAccount).filter(BankAccount.id == account_id, BankAccount.user_id == current_user.id).first()
        if not acc:
            raise HTTPException(status_code=404, detail="Selected bank account not found")
    else:
        acc = BankAccount(
            user_id=current_user.id,
            bank_name=bank_name.strip() or "Primary Bank",
            nickname=account_nickname.strip() or "Main Account",
            account_type=account_type.strip() or "Savings",
            masked_number="XXXX0000",
        )
        db.add(acc)
        db.flush()

    created: list[dict[str, Any]] = []
    user_dir = UPLOAD_DIR / f"user_{current_user.id}"
    user_dir.mkdir(parents=True, exist_ok=True)

    for up in files:
        orig_name = Path(up.filename or "statement").name
        ext = Path(orig_name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '{ext}' for {orig_name}. Allowed: PDF, CSV, XLSX, XLS.",
            )

        content = await up.read()
        if len(content) == 0:
            raise HTTPException(status_code=400, detail=f"Uploaded file '{orig_name}' is empty.")
        if len(content) > MAX_UPLOAD_SIZE_BYTES:
            raise HTTPException(status_code=400, detail=f"File '{orig_name}' exceeds 25 MB limit.")

        safe_storage_name = f"{uuid.uuid4().hex}{ext}"
        dest = user_dir / safe_storage_name
        dest.write_bytes(content)

        stmt = UploadedStatement(
            user_id=current_user.id,
            account_id=acc.id,
            file_name=orig_name,
            file_type=ext.lstrip("."),
            file_size=len(content),
            storage_path=str(dest),
            file_blob=content if len(content) <= 4_500_000 else None,
            status="uploaded",
            detected_count=0,
            imported_count=0,
        )
        db.add(stmt)
        db.flush()
        created.append(_serialize_statement(stmt))

    db.commit()
    return {"uploaded": created, "account": _serialize_account(acc, db)}


@app.post("/api/statements/{statement_id}/analyze")
def analyze_statement(
    statement_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = (
        db.query(UploadedStatement)
        .filter(UploadedStatement.id == statement_id, UploadedStatement.user_id == current_user.id)
        .first()
    )
    if not stmt:
        raise HTTPException(status_code=404, detail="Statement not found")

    parse_path = Path(stmt.storage_path) if stmt.storage_path else None
    if not parse_path or not parse_path.exists():
        if stmt.file_blob:
            ext = f".{stmt.file_type.lstrip('.')}"
            tmp_dir = UPLOAD_DIR / f"user_{current_user.id}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            parse_path = tmp_dir / f"{uuid.uuid4().hex}{ext}"
            parse_path.write_bytes(stmt.file_blob)
            stmt.storage_path = str(parse_path)
        else:
            raise HTTPException(status_code=400, detail="Source statement file no longer available on server")

    parse_res = parse_statement_file(parse_path)

    user_accounts = db.query(BankAccount).filter(BankAccount.user_id == current_user.id).all()
    user_masks = [a.masked_number for a in user_accounts]
    user_acc_ids = [a.id for a in user_accounts]

    # Update account bank/mask if currently generic
    if stmt.account:
        if parse_res.detected_bank and stmt.account.bank_name in {"Primary Bank", "Other Bank"}:
            stmt.account.bank_name = parse_res.detected_bank
        if parse_res.detected_masked_account and stmt.account.masked_number == "XXXX0000":
            stmt.account.masked_number = parse_res.detected_masked_account

    staged: list[dict[str, Any]] = []
    for idx, ptx in enumerate(parse_res.transactions):
        cls = classify_transaction_deterministic(
            ptx.description,
            ptx.debit,
            ptx.credit,
            ptx.payment_method,
            user_masks,
        )
        staged.append(
            {
                "temp_id": f"tx_{stmt.id}_{idx}",
                "tx_date": ptx.tx_date,
                "description": ptx.description,
                "merchant_or_recipient": cls["merchant_or_recipient"],
                "category": cls["category"],
                "tx_type": cls["tx_type"],
                "debit": ptx.debit,
                "credit": ptx.credit,
                "running_balance": ptx.running_balance,
                "payment_method": ptx.payment_method,
                "reference_number": ptx.reference_number,
                "is_internal_transfer": cls["is_internal_transfer"],
                "is_duplicate_flagged": False,
                "duplicate_reason": None,
                "duplicate_match_id": None,
                "p2p_person": cls["p2p_person"],
                "p2p_tag": None,
                "include_in_import": True,
                "confidence": ptx.confidence,
            }
        )

    pref = db.query(UserPreference).filter(UserPreference.user_id == current_user.id).first()
    if staged and (not pref or pref.enable_ai_enrichment):
        staged = enrich_ambiguous_with_gemini(staged)

    existing_txs = db.query(Transaction).filter(Transaction.user_id == current_user.id).all()
    staged = detect_duplicates_and_transfers(staged, existing_txs, user_acc_ids, stmt.account_id)

    stmt.detected_count = len(staged)
    stmt.opening_balance = parse_res.opening_balance
    stmt.closing_balance = parse_res.closing_balance
    if staged:
        dates = sorted(x["tx_date"] for x in staged)
        stmt.period_start = date.fromisoformat(dates[0])
        stmt.period_end = date.fromisoformat(dates[-1])
        stmt.status = "needs_review" if parse_res.confidence < 0.7 or parse_res.warnings else "analyzed"
    else:
        stmt.status = "needs_review"

    notes = list(parse_res.warnings)
    notes.append(f"Parser engine: {parse_res.method_used} (confidence {int(parse_res.confidence * 100)}%)")
    stmt.parser_notes = " | ".join(notes)
    stmt.preview_json = json.dumps(staged)
    db.commit()
    db.refresh(stmt)

    return {
        "statement": _serialize_statement(stmt),
        "preview_transactions": staged,
        "warnings": parse_res.warnings,
        "confidence": parse_res.confidence,
        "method_used": parse_res.method_used,
    }


@app.get("/api/statements/{statement_id}")
def get_statement_detail(
    statement_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = (
        db.query(UploadedStatement)
        .filter(UploadedStatement.id == statement_id, UploadedStatement.user_id == current_user.id)
        .first()
    )
    if not stmt:
        raise HTTPException(status_code=404, detail="Statement not found")

    preview = json.loads(stmt.preview_json) if stmt.preview_json else []
    imported = [
        _serialize_tx(t)
        for t in db.query(Transaction)
        .filter(Transaction.statement_id == stmt.id, Transaction.user_id == current_user.id)
        .order_by(Transaction.tx_date.asc(), Transaction.id.asc())
        .all()
    ]
    return {
        "statement": _serialize_statement(stmt),
        "preview_transactions": preview,
        "imported_transactions": imported,
    }


@app.post("/api/statements/{statement_id}/confirm")
def confirm_statement_import(
    statement_id: int,
    payload: ConfirmImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = (
        db.query(UploadedStatement)
        .filter(UploadedStatement.id == statement_id, UploadedStatement.user_id == current_user.id)
        .first()
    )
    if not stmt:
        raise HTTPException(status_code=404, detail="Statement not found")

    # Remove previously imported rows for this statement if re-confirming
    db.query(Transaction).filter(Transaction.statement_id == stmt.id, Transaction.user_id == current_user.id).delete()

    imported_count = 0
    latest_balance = payload.closing_balance if payload.closing_balance is not None else stmt.closing_balance
    latest_date: date | None = None

    for item in payload.transactions:
        if not item.include_in_import:
            continue
        tx_dt = date.fromisoformat(item.tx_date)
        if latest_date is None or tx_dt >= latest_date:
            latest_date = tx_dt
            if item.running_balance is not None and payload.closing_balance is None:
                latest_balance = item.running_balance

        fp = compute_fingerprint(item.tx_date, item.debit, item.credit, item.description, item.reference_number)
        tx = Transaction(
            user_id=current_user.id,
            account_id=stmt.account_id,
            statement_id=stmt.id,
            tx_date=tx_dt,
            description=item.description.strip(),
            merchant_or_recipient=(item.merchant_or_recipient or "").strip() or None,
            category=item.category,
            tx_type=item.tx_type,
            debit=round(max(item.debit, 0.0), 2),
            credit=round(max(item.credit, 0.0), 2),
            running_balance=item.running_balance,
            payment_method=item.payment_method or "OTHER",
            reference_number=item.reference_number,
            is_internal_transfer=item.is_internal_transfer or item.tx_type == "transfer",
            is_duplicate_flagged=item.is_duplicate_flagged,
            p2p_person=(item.p2p_person or "").strip() or None,
            p2p_tag=item.p2p_tag,
            fingerprint=fp,
        )
        db.add(tx)
        imported_count += 1

    stmt.status = "confirmed"
    stmt.imported_count = imported_count
    if payload.opening_balance is not None:
        stmt.opening_balance = payload.opening_balance
    if latest_balance is not None:
        stmt.closing_balance = latest_balance
        if stmt.account:
            stmt.account.closing_balance = latest_balance
    stmt.preview_json = None
    stmt.file_blob = None

    # Clean up temporary raw uploaded file after confirmed import if desired, or keep private
    db.commit()
    refresh_recurring_and_unusual_flags(db, current_user.id)
    generate_and_save_insights(db, current_user.id)

    return {"imported_count": imported_count, "statement": _serialize_statement(stmt)}


@app.delete("/api/statements/{statement_id}")
def delete_statement(
    statement_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = (
        db.query(UploadedStatement)
        .filter(UploadedStatement.id == statement_id, UploadedStatement.user_id == current_user.id)
        .first()
    )
    if not stmt:
        raise HTTPException(status_code=404, detail="Statement not found")

    if stmt.storage_path:
        Path(stmt.storage_path).unlink(missing_ok=True)
    db.delete(stmt)
    db.commit()
    refresh_recurring_and_unusual_flags(db, current_user.id)
    generate_and_save_insights(db, current_user.id)
    return {"deleted": True}


# ---------------- DASHBOARD & ANALYTICS ----------------
@app.get("/api/dashboard")
def get_dashboard(
    account_id: int | None = None,
    period: str = "all",
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return compute_dashboard_summary(db, current_user.id, account_id, period, start_date, end_date)


@app.get("/api/analytics/income")
def get_income_analysis(
    account_id: int | None = None,
    period: str = "all",
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    summary = compute_dashboard_summary(db, current_user.id, account_id, period, start_date, end_date)
    s_dt, e_dt = resolve_date_range(period, start_date, end_date)
    q = db.query(Transaction).filter(
        Transaction.user_id == current_user.id,
        Transaction.credit > 0,
        Transaction.is_internal_transfer.is_(False),
    )
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if s_dt:
        q = q.filter(Transaction.tx_date >= s_dt)
    if e_dt:
        q = q.filter(Transaction.tx_date <= e_dt)
    txs = q.order_by(Transaction.credit.desc()).all()
    return {
        "total_received": summary["kpis"]["total_received"],
        "total_refunds": summary["kpis"]["total_refunds"],
        "income_by_category": summary["income_by_category"],
        "monthly_series": summary["monthly_series"],
        "top_income_transactions": [_serialize_tx(t) for t in txs[:15]],
    }


@app.get("/api/analytics/expenses")
def get_expense_analysis(
    account_id: int | None = None,
    period: str = "all",
    start_date: date | None = None,
    end_date: date | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    summary = compute_dashboard_summary(db, current_user.id, account_id, period, start_date, end_date)
    s_dt, e_dt = resolve_date_range(period, start_date, end_date)
    q = db.query(Transaction).filter(
        Transaction.user_id == current_user.id,
        Transaction.debit > 0,
        Transaction.tx_type == "expense",
        Transaction.is_internal_transfer.is_(False),
    )
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if s_dt:
        q = q.filter(Transaction.tx_date >= s_dt)
    if e_dt:
        q = q.filter(Transaction.tx_date <= e_dt)
    txs = q.order_by(Transaction.debit.desc()).all()
    return {
        "total_spent": summary["kpis"]["total_spent"],
        "avg_daily_spending": summary["kpis"]["avg_daily_spending"],
        "highest_single_expense": summary["kpis"]["highest_single_expense"],
        "expense_by_category": summary["expense_by_category"],
        "daily_spending": summary["daily_spending"],
        "payment_methods": summary["payment_methods"],
        "monthly_series": summary["monthly_series"],
        "top_expense_transactions": [_serialize_tx(t) for t in txs[:15]],
    }


@app.get("/api/p2p")
def get_p2p_analysis(
    account_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return compute_p2p_summary(db, current_user.id, account_id)


# ---------------- TRANSACTIONS ----------------
@app.get("/api/categories")
def get_categories(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return {
        "income": INCOME_CATEGORIES,
        "expense": EXPENSE_CATEGORIES,
        "all": ALL_CATEGORIES,
    }


@app.get("/api/transactions")
def list_transactions(
    search: str | None = None,
    category: str | None = None,
    tx_type: str | None = None,
    account_id: int | None = None,
    payment_method: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    sort_by: str = "tx_date",
    sort_dir: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Transaction).filter(Transaction.user_id == current_user.id)
    if account_id:
        q = q.filter(Transaction.account_id == account_id)
    if category and category != "all":
        q = q.filter(Transaction.category == category)
    if tx_type and tx_type != "all":
        q = q.filter(Transaction.tx_type == tx_type)
    if payment_method and payment_method != "all":
        q = q.filter(Transaction.payment_method == payment_method)
    if start_date:
        q = q.filter(Transaction.tx_date >= start_date)
    if end_date:
        q = q.filter(Transaction.tx_date <= end_date)
    if search and search.strip():
        pat = f"%{search.strip()}%"
        q = q.filter(
            (Transaction.description.ilike(pat))
            | (Transaction.merchant_or_recipient.ilike(pat))
            | (Transaction.reference_number.ilike(pat))
            | (Transaction.p2p_person.ilike(pat))
            | (Transaction.category.ilike(pat))
        )

    sort_col_map = {
        "tx_date": Transaction.tx_date,
        "debit": Transaction.debit,
        "credit": Transaction.credit,
        "category": Transaction.category,
        "merchant_or_recipient": Transaction.merchant_or_recipient,
        "running_balance": Transaction.running_balance,
    }
    col = sort_col_map.get(sort_by, Transaction.tx_date)
    q = q.order_by(col.asc() if sort_dir == "asc" else col.desc(), Transaction.id.desc())

    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [_serialize_tx(t) for t in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max((total + page_size - 1) // page_size, 1),
    }


@app.put("/api/transactions/{tx_id}")
def update_transaction(
    tx_id: int,
    payload: TransactionUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.user_id == current_user.id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    for field, val in payload.model_dump(exclude_unset=True).items():
        setattr(tx, field, val)
    if payload.tx_type == "transfer":
        tx.is_internal_transfer = True
    elif payload.tx_type in {"income", "expense"} and payload.is_internal_transfer is None:
        tx.is_internal_transfer = False

    db.commit()
    db.refresh(tx)
    refresh_recurring_and_unusual_flags(db, current_user.id)
    generate_and_save_insights(db, current_user.id)
    return _serialize_tx(tx)


@app.delete("/api/transactions/{tx_id}")
def delete_transaction(
    tx_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.user_id == current_user.id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    db.delete(tx)
    db.commit()
    refresh_recurring_and_unusual_flags(db, current_user.id)
    generate_and_save_insights(db, current_user.id)
    return {"deleted": True}


# ---------------- KHARCH AI ASSISTANT ----------------
@app.get("/api/assistant/history")
def get_chat_history(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == current_user.id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .all()
    )
    return {
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "supporting_transactions": json.loads(m.supporting_json) if m.supporting_json else [],
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ]
    }


@app.post("/api/assistant/ask")
def ask_kharch_ai(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user_msg = ChatMessage(user_id=current_user.id, role="user", content=payload.question.strip())
    db.add(user_msg)
    result = answer_financial_question(db, current_user.id, payload.question, payload.account_id)
    ai_msg = ChatMessage(
        user_id=current_user.id,
        role="assistant",
        content=result["answer"],
        supporting_json=json.dumps(result["supporting_transactions"]),
    )
    db.add(ai_msg)
    db.commit()
    db.refresh(ai_msg)
    return {
        "id": ai_msg.id,
        "role": "assistant",
        "content": ai_msg.content,
        "supporting_transactions": result["supporting_transactions"],
        "created_at": ai_msg.created_at.isoformat(),
    }


@app.delete("/api/assistant/history")
def clear_chat_history(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db.query(ChatMessage).filter(ChatMessage.user_id == current_user.id).delete()
    db.commit()
    return {"cleared": True}


# ---------------- INSIGHTS ----------------
@app.get("/api/insights")
def get_insights(account_id: int | None = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    q = db.query(AIInsight).filter(AIInsight.user_id == current_user.id)
    if account_id:
        q = q.filter(AIInsight.account_id == account_id)
    else:
        q = q.filter(AIInsight.account_id.is_(None))
    rows = q.order_by(AIInsight.id.asc()).all()
    if not rows:
        return {"insights": generate_and_save_insights(db, current_user.id, account_id)}
    return {
        "insights": [
            {
                "id": r.id,
                "insight_type": r.insight_type,
                "severity": r.severity,
                "title": r.title,
                "summary": r.summary,
                "metric_value": r.metric_value,
            }
            for r in rows
        ]
    }


@app.post("/api/insights/refresh")
def refresh_insights(account_id: int | None = None, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    refresh_recurring_and_unusual_flags(db, current_user.id)
    return {"insights": generate_and_save_insights(db, current_user.id, account_id)}


# ---------------- MONTHLY REPORTS ----------------
@app.get("/api/reports/months")
def get_available_report_months(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    txs = db.query(Transaction.tx_date).filter(Transaction.user_id == current_user.id).all()
    months = sorted({t[0].strftime("%Y-%m") for t in txs}, reverse=True)
    if not months:
        months = [date.today().strftime("%Y-%m")]
    return {"months": months}


@app.get("/api/reports/{report_month}")
def get_monthly_report(
    report_month: str,
    account_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not re.match(r"^\d{4}-\d{2}$", report_month):
        raise HTTPException(status_code=400, detail="Month must be YYYY-MM")
    return build_monthly_report(db, current_user.id, report_month, account_id)


@app.get("/api/reports/{report_month}/export/csv")
def download_report_csv(
    report_month: str,
    account_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not re.match(r"^\d{4}-\d{2}$", report_month):
        raise HTTPException(status_code=400, detail="Month must be YYYY-MM")
    data = build_monthly_report(db, current_user.id, report_month, account_id)
    csv_bytes = export_report_csv(data)
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="kharch_karod_report_{report_month}.csv"'},
    )


@app.get("/api/reports/{report_month}/export/pdf")
def download_report_pdf(
    report_month: str,
    account_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not re.match(r"^\d{4}-\d{2}$", report_month):
        raise HTTPException(status_code=400, detail="Month must be YYYY-MM")
    data = build_monthly_report(db, current_user.id, report_month, account_id)
    pdf_bytes = export_report_pdf(data, current_user.full_name)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="kharch_karod_report_{report_month}.pdf"'},
    )


# ---------------- SETTINGS, PRIVACY & DEMO MODE ----------------
@app.get("/api/preferences")
def get_preferences(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    pref = db.query(UserPreference).filter(UserPreference.user_id == current_user.id).first()
    if not pref:
        pref = UserPreference(user_id=current_user.id)
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return {
        "currency": pref.currency,
        "enable_ai_enrichment": pref.enable_ai_enrichment,
        "mask_account_numbers": pref.mask_account_numbers,
        "exclude_credit_card_payments": pref.exclude_credit_card_payments,
        "monthly_budget_limit": pref.monthly_budget_limit,
    }


@app.put("/api/preferences")
def update_preferences(
    payload: PreferencesUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pref = db.query(UserPreference).filter(UserPreference.user_id == current_user.id).first()
    if not pref:
        pref = UserPreference(user_id=current_user.id)
        db.add(pref)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(pref, k, v)
    db.commit()
    db.refresh(pref)
    return {
        "currency": pref.currency,
        "enable_ai_enrichment": pref.enable_ai_enrichment,
        "mask_account_numbers": pref.mask_account_numbers,
        "exclude_credit_card_payments": pref.exclude_credit_card_payments,
        "monthly_budget_limit": pref.monthly_budget_limit,
    }


@app.post("/api/demo/load")
def api_load_demo(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return load_demo_dataset(db, current_user.id)


@app.delete("/api/demo/clear")
def api_clear_demo(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    db.query(BankAccount).filter(BankAccount.user_id == current_user.id, BankAccount.is_demo.is_(True)).delete()
    db.commit()
    refresh_recurring_and_unusual_flags(db, current_user.id)
    generate_and_save_insights(db, current_user.id)
    return {"cleared": True}


@app.delete("/api/account/purge-data")
def purge_all_financial_data(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    stmts = db.query(UploadedStatement).filter(UploadedStatement.user_id == current_user.id).all()
    for st in stmts:
        if st.storage_path:
            Path(st.storage_path).unlink(missing_ok=True)
    db.query(Transaction).filter(Transaction.user_id == current_user.id).delete()
    db.query(UploadedStatement).filter(UploadedStatement.user_id == current_user.id).delete()
    db.query(BankAccount).filter(BankAccount.user_id == current_user.id).delete()
    db.query(AIInsight).filter(AIInsight.user_id == current_user.id).delete()
    db.query(MonthlyReport).filter(MonthlyReport.user_id == current_user.id).delete()
    db.query(ChatMessage).filter(ChatMessage.user_id == current_user.id).delete()
    db.commit()
    return {"purged": True}
