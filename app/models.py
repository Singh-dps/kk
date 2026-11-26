from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from .database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String(120), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    accounts = relationship("BankAccount", back_populates="user", cascade="all, delete-orphan")
    statements = relationship("UploadedStatement", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")
    categories = relationship("Category", back_populates="user", cascade="all, delete-orphan")
    insights = relationship("AIInsight", back_populates="user", cascade="all, delete-orphan")
    reports = relationship("MonthlyReport", back_populates="user", cascade="all, delete-orphan")
    preferences = relationship("UserPreference", back_populates="user", uselist=False, cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="user", cascade="all, delete-orphan")


class BankAccount(Base):
    __tablename__ = "bank_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    bank_name = Column(String(120), nullable=False)
    nickname = Column(String(120), nullable=False)
    account_type = Column(String(40), nullable=False, default="Savings")  # Savings, Current, Credit Card, Wallet
    masked_number = Column(String(24), nullable=False, default="XXXX0000")
    closing_balance = Column(Float, nullable=True)
    is_demo = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("User", back_populates="accounts")
    statements = relationship("UploadedStatement", back_populates="account", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="account", cascade="all, delete-orphan")


class UploadedStatement(Base):
    __tablename__ = "uploaded_statements"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("bank_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(20), nullable=False)
    file_size = Column(Integer, nullable=False)
    storage_path = Column(String(500), nullable=True)  # Encrypted/temp local path, never publicly exposed
    file_blob = Column(LargeBinary, nullable=True)  # Enables stateless upload->analyze across serverless containers
    upload_date = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    status = Column(String(40), nullable=False, default="uploaded")  # uploaded, analyzed, needs_review, confirmed, failed
    detected_count = Column(Integer, default=0, nullable=False)
    imported_count = Column(Integer, default=0, nullable=False)
    opening_balance = Column(Float, nullable=True)
    closing_balance = Column(Float, nullable=True)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    parser_notes = Column(Text, nullable=True)
    preview_json = Column(Text, nullable=True)  # Temporary staged transactions before user confirms import
    is_demo = Column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="statements")
    account = relationship("BankAccount", back_populates="statements")
    transactions = relationship("Transaction", back_populates="statement", cascade="all, delete-orphan")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("bank_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    statement_id = Column(Integer, ForeignKey("uploaded_statements.id", ondelete="CASCADE"), nullable=True, index=True)

    tx_date = Column(Date, nullable=False, index=True)
    description = Column(Text, nullable=False)
    merchant_or_recipient = Column(String(200), nullable=True, index=True)
    category = Column(String(80), nullable=False, index=True)
    tx_type = Column(String(30), nullable=False, index=True)  # income, expense, transfer, refund, reversal, credit_card_payment, loan_disbursement
    debit = Column(Float, default=0.0, nullable=False)
    credit = Column(Float, default=0.0, nullable=False)
    running_balance = Column(Float, nullable=True)
    payment_method = Column(String(40), nullable=False, default="OTHER")
    reference_number = Column(String(120), nullable=True, index=True)

    is_internal_transfer = Column(Boolean, default=False, nullable=False)
    is_duplicate_flagged = Column(Boolean, default=False, nullable=False)
    is_recurring = Column(Boolean, default=False, nullable=False)
    is_unusual = Column(Boolean, default=False, nullable=False)

    p2p_person = Column(String(150), nullable=True, index=True)
    p2p_tag = Column(String(40), nullable=True)  # personal_transfer, loan, repayment, gift, None
    notes = Column(Text, nullable=True)
    fingerprint = Column(String(128), nullable=True, index=True)
    is_demo = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("User", back_populates="transactions")
    account = relationship("BankAccount", back_populates="transactions")
    statement = relationship("UploadedStatement", back_populates="transactions")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("user_id", "name", "kind", name="uq_user_category"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String(80), nullable=False)
    kind = Column(String(20), nullable=False)  # income, expense
    is_default = Column(Boolean, default=True, nullable=False)

    user = relationship("User", back_populates="categories")


class AIInsight(Base):
    __tablename__ = "ai_insights"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, nullable=True, index=True)
    insight_type = Column(String(60), nullable=False)
    severity = Column(String(20), nullable=False, default="info")  # info, warning, positive, alert
    title = Column(String(200), nullable=False)
    summary = Column(Text, nullable=False)
    metric_value = Column(String(120), nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("User", back_populates="insights")


class MonthlyReport(Base):
    __tablename__ = "monthly_reports"
    __table_args__ = (UniqueConstraint("user_id", "report_month", "account_scope", name="uq_user_month_scope"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    report_month = Column(String(7), nullable=False)  # YYYY-MM
    account_scope = Column(String(40), nullable=False, default="all")
    total_income = Column(Float, default=0.0, nullable=False)
    total_expenses = Column(Float, default=0.0, nullable=False)
    net_cash_flow = Column(Float, default=0.0, nullable=False)
    report_json = Column(Text, nullable=False)
    generated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("User", back_populates="reports")


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    currency = Column(String(10), default="INR", nullable=False)
    enable_ai_enrichment = Column(Boolean, default=True, nullable=False)
    mask_account_numbers = Column(Boolean, default=True, nullable=False)
    exclude_credit_card_payments = Column(Boolean, default=True, nullable=False)
    monthly_budget_limit = Column(Float, nullable=True)

    user = relationship("User", back_populates="preferences")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # user, assistant
    content = Column(Text, nullable=False)
    supporting_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("User", back_populates="chat_messages")
