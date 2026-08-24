"""Resolve the logged-in toolbox engineer and the scan report recipient.

When these pages are copied into https://lkq-toolbox.replit.app/, the host app
already owns login. This module reads that identity from Streamlit session
state (streamlit-authenticator and common custom keys) so every scan is
saved under the engineer who started it.

Report email defaults to hotline.support@lkqbelgium.be when the field is empty
or invalid.
"""

from __future__ import annotations

import re
from typing import Any, Optional

DEFAULT_REPORT_EMAIL = "hotline.support@lkqbelgium.be"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Keys used by streamlit-authenticator and typical custom Streamlit logins.
# Do not include this page's own widgets (engineer_name / report_email).
_ENGINEER_KEYS = (
    "username",
    "user_name",
    "name",
    "current_user",
    "logged_in_user",
    "auth_user",
    "user",
    "login_user",
    "kept_username",
)

_OWN_WIDGET_KEYS = {"engineer_name", "report_email", "engineer"}

_SKIP_VALUES = {
    "",
    "none",
    "null",
    "true",
    "false",
    "anonymous",
    "guest",
}

_NESTED_NAME_KEYS = (
    "username",
    "user_name",
    "name",
    "display_name",
    "full_name",
    "engineer",
    "email",
    "user",
)


def normalize_report_email(raw: Optional[str]) -> str:
    """Return a usable To: address; empty/invalid falls back to Hotline."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_REPORT_EMAIL
    first = text.split(",")[0].strip()
    if _EMAIL_RE.match(first):
        return first
    return DEFAULT_REPORT_EMAIL


def _clean_name(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text and text.lower() not in _SKIP_VALUES and "@" not in text[:1]:
            if "\n" in text or len(text) > 80:
                return ""
            return text
        # Allow emails as identity (toolbox may store engineer email).
        if text and _EMAIL_RE.match(text):
            return text
        return ""
    if isinstance(value, dict):
        for key in _NESTED_NAME_KEYS:
            found = _clean_name(value.get(key))
            if found:
                return found
        return ""
    return ""


def _from_st_user() -> str:
    try:
        import streamlit as st

        user = getattr(st, "user", None)
    except Exception:
        return ""
    if user is None:
        return ""
    try:
        if hasattr(user, "is_logged_in") and user.is_logged_in is False:
            return ""
    except Exception:
        pass
    for attr in ("user_name", "username", "name", "email"):
        try:
            found = _clean_name(getattr(user, attr, None))
        except Exception:
            found = ""
        if found:
            return found
    try:
        return _clean_name(dict(user))
    except Exception:
        return ""


def _from_query_params() -> str:
    try:
        import streamlit as st

        params = st.query_params
        for key in ("engineer", "username", "user"):
            raw = params.get(key)
            if isinstance(raw, list):
                raw = raw[0] if raw else ""
            found = _clean_name(raw)
            if found:
                return found
    except Exception:
        return ""
    return ""


def resolve_engineer(session: Any = None) -> str:
    """Best-effort engineer identity from the host Streamlit login.

    Returns an empty string when this repo is run standalone with no login.
    """
    if session is None:
        try:
            import streamlit as st

            session = st.session_state
        except Exception:
            session = {}

    # Prefer authenticator username (stable) over display name.
    try:
        if session.get("authentication_status"):
            for key in ("username", "name"):
                found = _clean_name(session.get(key))
                if found:
                    return found
    except Exception:
        pass

    for key in _ENGINEER_KEYS:
        try:
            found = _clean_name(session.get(key))
        except Exception:
            found = ""
        if found:
            return found

    found = _from_st_user()
    if found:
        return found

    try:
        for key, value in session.items():
            kl = str(key).lower()
            if kl in _OWN_WIDGET_KEYS:
                continue
            if any(
                token in kl
                for token in (
                    "password",
                    "pwd",
                    "token",
                    "secret",
                    "cookie",
                    "authentication",
                    "report_email",
                )
            ):
                continue
            if not any(token in kl for token in ("user", "login", "operator")):
                continue
            found = _clean_name(value)
            if found:
                return found
    except Exception:
        pass

    return _from_query_params()
