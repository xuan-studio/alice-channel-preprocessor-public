from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import delete, select

from .config import Settings
from .models import PlatformSession, PlatformUser, utcnow


_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("平台密码至少需要 10 个字符。")
    return _hasher.hash(password)


def generate_temporary_password(length: int = 18) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "Tmp-" + "".join(secrets.choice(alphabet) for _ in range(max(10, length)))


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return bool(_hasher.verify(stored_hash, password))
    except (VerifyMismatchError, ValueError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bootstrap_admin(session, settings: Settings) -> PlatformUser:
    existing = session.scalar(select(PlatformUser).where(PlatformUser.username == settings.bootstrap_username))
    if existing:
        if existing.role != "super_admin":
            existing.role = "super_admin"
        existing.active = True
        existing.must_change_password = False
        return existing
    user = PlatformUser(
        username=settings.bootstrap_username,
        display_name="Administrator",
        password_hash=hash_password(settings.bootstrap_password),
        role="super_admin",
        active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def create_login_session(session, user: PlatformUser, settings: Settings) -> tuple[str, PlatformSession]:
    raw_token = secrets.token_urlsafe(32)
    record = PlatformSession(
        user_id=user.id,
        token_hash=token_hash(raw_token),
        csrf_token=secrets.token_urlsafe(24),
        expires_at=utcnow() + timedelta(days=settings.session_days),
    )
    user.last_login_at = utcnow()
    session.add(record)
    session.flush()
    return raw_token, record


def resolve_login_session(session, raw_token: str | None) -> PlatformSession | None:
    if not raw_token:
        return None
    record = session.scalar(
        select(PlatformSession).where(PlatformSession.token_hash == token_hash(raw_token))
    )
    if not record or record.expires_at.replace(tzinfo=record.expires_at.tzinfo or utcnow().tzinfo) <= utcnow():
        return None
    if not record.user.active:
        return None
    return record


def logout_session(session, raw_token: str | None) -> None:
    if raw_token:
        session.execute(delete(PlatformSession).where(PlatformSession.token_hash == token_hash(raw_token)))
