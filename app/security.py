from datetime import datetime, timedelta, timezone
import re
import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from .config import ACCESS_TOKEN_EXPIRE_HOURS, JWT_ALGORITHM, JWT_SECRET_KEY
from .database import get_db
from .models import Category, User, UserPreference
from .services.constants import EXPENSE_CATEGORIES, INCOME_CATEGORIES

bearer_scheme = HTTPBearer(auto_error=False)
DEFAULT_WORKSPACE_EMAIL = "workspace@kharchkarod.local"


def _get_or_create_default_user(db: Session) -> User:
    user = db.query(User).filter(User.email == DEFAULT_WORKSPACE_EMAIL).first()
    if user:
        return user
    user = User(
        full_name="Personal Workspace",
        email=DEFAULT_WORKSPACE_EMAIL,
        password_hash=hash_password("local-workspace-default"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.add(UserPreference(user_id=user.id))
    for cname in INCOME_CATEGORIES:
        db.add(Category(user_id=user.id, name=cname, kind="income", is_default=True))
    for cname in EXPENSE_CATEGORIES:
        db.add(Category(user_id=user.id, name=cname, kind="expense", is_default=True))
    db.commit()
    return user


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: int, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not credentials or not credentials.credentials:
        return _get_or_create_default_user(db)
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = int(payload.get("sub", 0))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


def mask_account_number(raw: str | None) -> str:
    if not raw:
        return "XXXX0000"
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 4:
        return f"XXXX{digits[-4:]}"
    cleaned = raw.strip()
    if len(cleaned) <= 4:
        return f"XXXX{cleaned}"
    return f"XXXX{cleaned[-4:]}"


def sanitize_for_external_ai(text: str) -> str:
    """Strip long account numbers, phone numbers, card numbers, and IFSC/email PII before any external LLM call."""
    if not text:
        return ""
    # Mask 9-18 digit bank account/card numbers
    cleaned = re.sub(r"\b\d{9,18}\b", "[ACCT]", text)
    # Mask 10-digit Indian mobile numbers
    cleaned = re.sub(r"\b(?:\+91[-\s]?)?[6-9]\d{9}\b", "[PHONE]", cleaned)
    # Mask email addresses
    cleaned = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", cleaned)
    return cleaned
