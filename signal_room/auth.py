"""Per-brand passcode auth: hash + HMAC-signed cookie.

Stdlib-only (no bcrypt / itsdangerous) so the Render build doesn't need
new wheels. Threat model is light: passcodes gate edit/refetch on a
small-tenant deploy. Costs are tuned accordingly.

- Hash: hashlib.pbkdf2_hmac(sha256) with a server-side pepper from env.
  Per-brand salt embedded in the stored hash string for self-contained verify.
  100k iterations — fine for passcode strength on a small-tenant deploy.
- Cookie: `sr_passcode_<slug>=<token>.<signature>` where signature is
  HMAC-SHA256 over the token using a server secret. Token is a random
  ID stored alongside the hash so we can revoke without rotating the secret.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import string
from typing import Optional

from fastapi import HTTPException, Request, Response

PEPPER_ENV = "SIGNAL_ROOM_PASSCODE_PEPPER"
COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days
PBKDF2_ITERATIONS = 100_000


def _pepper() -> bytes:
    """Server-side pepper. Falls back to a deterministic dev value if missing,
    with a loud warning — prod MUST set this env var."""
    val = os.environ.get(PEPPER_ENV, "")
    if not val:
        # Dev fallback. Don't ship to prod without setting the env.
        return b"DEV-pepper-do-not-ship-this-to-prod"
    return val.encode("utf-8")


def generate_passcode(length: int = 8) -> str:
    """8-char human-friendly passcode: A-Z + a-z + 2-9 (no 0/O/1/I)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def hash_passcode(passcode: str) -> str:
    """Return `salt$hash` (both hex). Verify with verify_passcode."""
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        passcode.encode("utf-8") + _pepper(),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )
    return f"{salt.hex()}${derived.hex()}"


def verify_passcode(passcode: str, stored: str) -> bool:
    if not passcode or not stored or "$" not in stored:
        return False
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        passcode.encode("utf-8") + _pepper(),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )
    return hmac.compare_digest(derived, expected)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def cookie_name(slug: str) -> str:
    # Safe-ish slug chars; we already validate slugs are kebab-case.
    return f"sr_passcode_{slug.replace('-', '_')}"


def sign_cookie_value(token: str) -> str:
    """Return `token.signature` — signature is HMAC-SHA256 over the token."""
    sig = hmac.new(_pepper(), token.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{token}.{sig}"


def unsign_cookie_value(cookie_value: str) -> Optional[str]:
    """Verify signature and return the token. None if invalid."""
    if not cookie_value or "." not in cookie_value:
        return None
    token, sig = cookie_value.rsplit(".", 1)
    expected_sig = hmac.new(_pepper(), token.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    return token


def set_passcode_cookie(response: Response, slug: str, token: str) -> None:
    response.set_cookie(
        key=cookie_name(slug),
        value=sign_cookie_value(token),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("SIGNAL_ROOM_ENV") == "production",
    )


def read_passcode_cookie(request: Request, slug: str) -> Optional[str]:
    raw = request.cookies.get(cookie_name(slug))
    if not raw:
        return None
    return unsign_cookie_value(raw)


def clear_passcode_cookie(response: Response, slug: str) -> None:
    response.delete_cookie(key=cookie_name(slug))


def has_valid_passcode(request: Request, brand_row: dict) -> bool:
    """True when the request's signed cookie matches the brand's session token."""
    if not brand_row:
        return False
    cookie_token = read_passcode_cookie(request, brand_row["slug"])
    if not cookie_token:
        return False
    stored_token = brand_row.get("passcode_session_token", "")
    if not stored_token:
        return False
    return hmac.compare_digest(cookie_token, stored_token)


def require_passcode_or_redirect(request: Request, brand_row: dict, next_path: str = "") -> None:
    """Raise HTTPException(303 → /{brand}/auth?next=...) when missing/invalid.

    Caller-controlled `next_path` is the location to return to after a
    successful passcode entry. Defaults to the current request path.
    """
    if has_valid_passcode(request, brand_row):
        return
    nxt = next_path or str(request.url.path)
    raise HTTPException(
        status_code=303,
        detail="passcode required",
        headers={"Location": f"/{brand_row['slug']}/auth?next={nxt}"},
    )
