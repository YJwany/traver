"""Bearer API-key authentication and owner scoping.

Design notes, because the choices here are deliberate:

* **No passwords, no registration.** Each consumer gets a minted key. This is the
  smallest thing that provides real tenant isolation, and it avoids inventing a
  password-reset flow that would need email delivery to be meaningful.
* **Only a hash is stored.** A leaked database must not yield usable keys. The
  key is compared by hashing the presented value and looking up the digest, so
  the comparison is a database lookup rather than a string compare and does not
  leak length or content through timing.
* **Keys are never logged.** The presented credential is only ever hashed; the
  display prefix is what appears in logs and listings.
* **Disabled by default only in the sense that it can be switched off.** See
  ``Settings.auth_enabled``: when off (local development), a single synthetic
  owner is used so the app stays usable without key management, and that fact is
  reported by ``/api/v1/health`` so it cannot be mistaken for a secured deploy.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from travel_api.db.repositories import ApiKeyRepository

#: Prefix so a leaked string can be recognised as a Traver key by scanners.
KEY_PREFIX = "trv_"
#: 32 bytes of urandom -> 43 chars of urlsafe base64. Far beyond guessing.
KEY_BYTES = 32
#: Characters retained in clear text for human identification.
DISPLAY_PREFIX_LENGTH = 12

SCOPE_READ = "trip:read"
SCOPE_WRITE = "trip:write"
ALL_SCOPES = (SCOPE_READ, SCOPE_WRITE)


class AuthenticationError(Exception):
    """Raised when a credential is missing, malformed, revoked or unknown."""


class AuthorizationError(Exception):
    """Raised when a valid credential lacks the scope an operation needs."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    owner_id: str
    key_id: str | None = None
    label: str = ""
    scopes: tuple[str, ...] = field(default_factory=tuple)

    def can(self, scope: str) -> bool:
        # An empty scope list means "all scopes", which is what a minted key
        # without an explicit restriction should allow.
        return not self.scopes or scope in self.scopes

    @property
    def is_anonymous(self) -> bool:
        """True for the synthetic owner used when auth is switched off."""
        return self.key_id is None


def generate_api_key() -> str:
    """Mint a new key. Returned once and never recoverable afterwards."""
    return KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)


def hash_api_key(key: str) -> str:
    """Hash a key for storage and lookup.

    A plain SHA-256 is appropriate here (unlike for passwords) because the input
    is 256 bits of entropy from ``secrets``: there is no dictionary to attack, so
    a slow KDF would only add latency to every authenticated request.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def display_prefix(key: str) -> str:
    return key[:DISPLAY_PREFIX_LENGTH]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuthService:
    """Resolves presented credentials to a :class:`Principal`."""

    def __init__(self, uow_factory, *, enabled: bool, anonymous_owner: str = "local-dev") -> None:
        self.uow_factory = uow_factory
        self.enabled = enabled
        self.anonymous_owner = anonymous_owner

    # -- credential resolution --------------------------------------------

    def authenticate(self, presented: str | None) -> Principal:
        """Resolve an ``Authorization: Bearer <key>`` value.

        Raises :class:`AuthenticationError` rather than returning ``None`` so a
        caller cannot accidentally treat a failed lookup as "no restriction".
        """
        if not self.enabled:
            # Development mode: one shared owner, explicitly flagged as such.
            return Principal(owner_id=self.anonymous_owner)

        if not presented:
            raise AuthenticationError("missing bearer token")
        key = presented.strip()
        if not key:
            raise AuthenticationError("empty bearer token")
        if not key.startswith(KEY_PREFIX):
            # Reject early on shape so obviously-wrong credentials (a JWT, a
            # session cookie pasted by mistake) get a clear error.
            raise AuthenticationError("malformed API key")

        digest = hash_api_key(key)
        with self.uow_factory() as uow:
            record = uow.api_keys.get_by_hash(digest)
            if record is None:
                raise AuthenticationError("unknown API key")
            if not record.active or record.revoked_at is not None:
                raise AuthenticationError("API key revoked")
            uow.api_keys.touch_last_used(record.id, utc_now())
            return Principal(
                owner_id=record.owner_id,
                key_id=str(record.id),
                label=record.label,
                scopes=tuple(record.scopes or ()),
            )

    # -- key lifecycle ----------------------------------------------------

    def mint(
        self,
        owner_id: str,
        *,
        label: str = "",
        scopes: tuple[str, ...] = (),
    ) -> tuple[str, str]:
        """Create a key for ``owner_id``. Returns ``(plaintext, key_id)``.

        The plaintext is returned exactly once; only its hash is persisted.
        """
        owner = (owner_id or "").strip()
        if not owner:
            raise ValueError("owner_id is required")
        unknown = set(scopes) - set(ALL_SCOPES)
        if unknown:
            raise ValueError(f"unknown scope(s): {sorted(unknown)}")

        key = generate_api_key()
        with self.uow_factory() as uow:
            record = uow.api_keys.create(
                key_hash=hash_api_key(key),
                key_prefix=display_prefix(key),
                owner_id=owner,
                label=label,
                scopes=list(scopes),
                created_at=utc_now(),
            )
            return key, str(record.id)

    def revoke(self, key_id) -> bool:
        """Revoke a key by id.

        Accepts a ``UUID`` or its string form. ``mint`` returns ``str(record.id)``
        because that is what a CLI or API caller prints and pastes back, so
        normalising here keeps the round-trip working — passing the string
        straight to the ORM raised "'str' object has no attribute 'hex'".
        """
        try:
            identifier = key_id if isinstance(key_id, UUID) else UUID(str(key_id))
        except (TypeError, ValueError):
            return False
        with self.uow_factory() as uow:
            return uow.api_keys.revoke(identifier, utc_now())

    def list_keys(self, owner_id: str | None = None) -> list[dict]:
        with self.uow_factory() as uow:
            records = uow.api_keys.list(owner_id)
            return [
                {
                    "id": str(record.id),
                    "key_prefix": record.key_prefix,
                    "owner_id": record.owner_id,
                    "label": record.label,
                    "scopes": list(record.scopes or []),
                    "active": record.active,
                    "created_at": record.created_at.isoformat() if record.created_at else None,
                    "last_used_at": record.last_used_at.isoformat()
                    if record.last_used_at
                    else None,
                    "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
                }
                for record in records
            ]

    def count_active(self) -> int:
        with self.uow_factory() as uow:
            return uow.api_keys.count_active()


__all__ = [
    "ALL_SCOPES",
    "KEY_PREFIX",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "ApiKeyRepository",
    "AuthService",
    "AuthenticationError",
    "AuthorizationError",
    "Principal",
    "display_prefix",
    "generate_api_key",
    "hash_api_key",
]
