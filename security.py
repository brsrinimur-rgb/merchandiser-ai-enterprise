"""
Minimal shared-secret authentication.

Set the API_KEY environment variable on the backend (Render -> Environment).
Every protected endpoint then requires the header:

    X-API-Key: <the value of API_KEY>

If API_KEY is not set (e.g. local dev), auth is disabled and everything is
open -- this keeps run_all.bat working without extra setup.
"""
import os
from fastapi import Header, HTTPException

API_KEY = os.environ.get("API_KEY", "").strip()


def require_api_key(x_api_key: str | None = Header(None, alias="X-API-Key")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
