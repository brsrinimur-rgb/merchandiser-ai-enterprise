"""Microsoft Entra ID client-credential authentication for D365."""
from __future__ import annotations

import msal

from .config import D365Settings


class D365AuthenticationError(RuntimeError):
    pass


class D365TokenProvider:
    def __init__(self, settings: D365Settings):
        self.settings = settings
        self._app = msal.ConfidentialClientApplication(
            client_id=settings.client_id,
            authority=settings.authority,
            client_credential=settings.client_secret,
        )

    def get_access_token(self) -> str:
        result = self._app.acquire_token_silent(self.settings.scope, account=None)
        if not result:
            result = self._app.acquire_token_for_client(scopes=self.settings.scope)
        token = result.get("access_token")
        if not token:
            message = result.get("error_description") or result.get("error") or "Token acquisition failed"
            raise D365AuthenticationError(message)
        return token
