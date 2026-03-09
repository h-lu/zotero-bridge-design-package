from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings
from app.errors import BridgeError

bearer_scheme = HTTPBearer(auto_error=False)
CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def require_bearer_auth(
    credentials: CredentialsDep,
    settings: SettingsDep,
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise BridgeError(
            code="UNAUTHORIZED",
            message="Missing bearer token",
            status_code=401,
        )

    if not hmac.compare_digest(credentials.credentials, settings.bridge_api_key):
        raise BridgeError(
            code="UNAUTHORIZED",
            message="Invalid bearer token",
            status_code=401,
        )

    return credentials.credentials
