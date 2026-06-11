"""Shared request helpers: the current user and role gating.

Feature routers depend on these, not on ``create_app``. Platform services (the user
store, registry, templates, jobs dir) are read from ``request.app.state``, so a feature
router stays decoupled from how the app is assembled.
"""

from __future__ import annotations

from collections.abc import Collection

from fastapi import Request

from ..auth import User, UserStore


class LoginRequired(Exception):
    """Raised when an action needs a signed-in user; handled by a redirect to /login."""


class Forbidden(Exception):
    """Raised when a signed-in user lacks the required role."""

    def __init__(self, message: str = "You don't have permission to do that."):
        super().__init__(message)
        self.message = message


def store(request: Request) -> UserStore:
    return request.app.state.store


def current_user(request: Request) -> User | None:
    uid = request.session.get("user_id")
    return store(request).get(uid) if uid else None


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def require_role(request: Request, roles: Collection[str]) -> User:
    user = require_user(request)
    if user.role not in roles:
        raise Forbidden(f"Your role ({user.role}) is not permitted to do that.")
    return user
