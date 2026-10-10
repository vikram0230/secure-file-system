import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_service.db.models import User
from api_service.db.session import get_db

API_KEY_PREFIX = "sfs_"


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="invalid or missing API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    scheme, _, api_key = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not api_key.startswith(API_KEY_PREFIX):
        raise _unauthorized()

    user = await db.scalar(select(User).where(User.api_key_hash == hash_api_key(api_key.strip())))
    if user is None:
        raise _unauthorized()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
