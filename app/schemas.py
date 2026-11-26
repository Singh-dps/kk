from datetime import date, datetime
from typing import Any, Optional
from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]


class BankAccountCreate(BaseModel):
    bank_name: str = Field(min_length=2, max_length=120)
    nickname: str = Field(min_length=1, max_length=120)
    account_type: str = Field(default="Savings")
    account_number: str = Field(default="0000")
    closing_balance: Optional[float] = None


class BankAccountUpdate(BaseModel):
    bank_name: Optional[str] = None
    nickname: Optional[str] = None
    account_type: Optional[str] = None
    account_number: Optional[str] = None
    closing_balance: Optional[float] = None


class StagedTransactionItem(BaseModel):
    temp_id: str
    tx_date: str
    description: str
    merchant_or_recipient: Optional[str] = None
    category: str
    tx_type: str
    debit: float = 0.0
    credit: float = 0.0
    running_balance: Optional[float] = None
    payment_method: str = "OTHER"
    reference_number: Optional[str] = None
    is_internal_transfer: bool = False
    is_duplicate_flagged: bool = False
    duplicate_reason: Optional[str] = None
    duplicate_match_id: Optional[int] = None
    p2p_person: Optional[str] = None
    p2p_tag: Optional[str] = None
    include_in_import: bool = True
    confidence: float = 1.0


class ConfirmImportRequest(BaseModel):
    transactions: list[StagedTransactionItem]
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None


class TransactionUpdateRequest(BaseModel):
    tx_date: Optional[date] = None
    description: Optional[str] = None
    merchant_or_recipient: Optional[str] = None
    category: Optional[str] = None
    tx_type: Optional[str] = None
    debit: Optional[float] = None
    credit: Optional[float] = None
    running_balance: Optional[float] = None
    payment_method: Optional[str] = None
    reference_number: Optional[str] = None
    is_internal_transfer: Optional[bool] = None
    p2p_person: Optional[str] = None
    p2p_tag: Optional[str] = None
    notes: Optional[str] = None


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    account_id: Optional[int] = None


class PreferencesUpdateRequest(BaseModel):
    enable_ai_enrichment: Optional[bool] = None
    mask_account_numbers: Optional[bool] = None
    exclude_credit_card_payments: Optional[bool] = None
    monthly_budget_limit: Optional[float] = None
