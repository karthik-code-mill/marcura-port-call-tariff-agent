"""
Role-Based Access Control (RBAC) — Abstract Stub.

This is a showcase implementation demonstrating the access control model.
It has no persistence backend — in production, user roles would be resolved
from an identity provider (Azure AD, Auth0, etc.) and stored in a database.

Model:
  ADMIN       — full access across all countries
  COUNTRY_OPS — read + write access within their own country only
                (e.g. UAE ops cannot modify South Africa tariff data)
  READER      — read-only across all countries

Usage:
    from security.rbac import RBACPolicy, UserContext, Role

    user = UserContext(user_id="uae_ops_1", country="UAE", role=Role.COUNTRY_OPS)
    policy = RBACPolicy()
    policy.check_write(user, "South Africa")  # raises PermissionError
    policy.check_write(user, "UAE")           # allowed
"""

import logging
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)


class Role(Enum):
    ADMIN       = "admin"
    COUNTRY_OPS = "country_ops"
    READER      = "reader"


@dataclass
class UserContext:
    """Represents the calling user's identity and access scope."""
    user_id: str
    country: str   # the country this user belongs to / operates in
    role:    Role


class RBACPolicy:
    """
    Evaluate access decisions based on role and country ownership.

    All methods raise PermissionError on denial so callers get a clear,
    catchable signal rather than a silent boolean check they might forget.
    """

    def can_write(self, user: UserContext, target_country: str) -> bool:
        if user.role == Role.ADMIN:
            return True
        if user.role == Role.COUNTRY_OPS:
            return user.country.strip().lower() == target_country.strip().lower()
        return False  # READER

    def can_read(self, user: UserContext, target_country: str) -> bool:  # noqa: ARG002
        return True   # all roles may read

    def check_write(self, user: UserContext, target_country: str) -> None:
        """Raises PermissionError if the user cannot write to target_country data."""
        if not self.can_write(user, target_country):
            msg = (
                f"[RBAC] DENIED: user '{user.user_id}' (role={user.role.value}, "
                f"country='{user.country}') attempted to write to '{target_country}' data."
            )
            log.error(msg)
            raise PermissionError(msg)
        log.debug(
            f"[RBAC] ALLOWED write: user '{user.user_id}' → '{target_country}'"
        )

    def check_read(self, user: UserContext, target_country: str) -> None:
        """Raises PermissionError if the user cannot read target_country data (reserved)."""
        if not self.can_read(user, target_country):
            msg = f"[RBAC] DENIED read: user '{user.user_id}' → '{target_country}'"
            log.error(msg)
            raise PermissionError(msg)
