"""
Auth utilities — JWT + API key verification.
In local dev mode (JWT_SECRET=dev_secret_change_in_prod) all routes pass.
"""
from __future__ import annotations

import os
from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

JWT_SECRET    = os.getenv("JWT_SECRET", "dev_secret_change_in_prod")
JWT_ALGORITHM = "HS256"
DEV_MODE      = JWT_SECRET == "dev_secret_change_in_prod"

security = HTTPBearer(auto_error=False)


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> dict:
    if DEV_MODE:
        return {"sub": "dev_user", "role": "admin"}
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def verify_api_key(x_api_key: str | None = None) -> bool:
    if DEV_MODE:
        return True
    expected = os.getenv("API_KEY", "")
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return True


async def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security)) -> dict:
    return verify_token(credentials)
