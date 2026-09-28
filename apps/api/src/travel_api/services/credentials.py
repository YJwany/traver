"""Reversible, per-owner storage for the operator's own third-party credentials.

Why this is not the ``api_keys`` table
--------------------------------------

Traver's own bearer keys are verified, never replayed, so only a SHA-256 digest
is stored and a database leak yields nothing usable. A *user's* credential for
Amap or an AI provider is the opposite case: Traver has to send it upstream on
every call, so it must be recoverable, and therefore must be encrypted rather
than hashed. Two different problems, two different mechanisms, kept in two
different tables on purpose.

What the encryption defends against
-----------------------------------

Credentials are sealed with AES-256-GCM under a master key that is **not** in the
database, so:

* a leaked database — a backup dump, a copied ``.db``/``.sqlite`` file, a
  read-only SQL injection, an operator browsing tables, a screenshot of one —
  yields ciphertext that cannot be decrypted without the master key;
* tampering is detected rather than decrypted into garbage: GCM authenticates,
  so a modified ciphertext raises instead of producing a plausible wrong value;
* swapping a row is detected too. The owner, provider and credential name are
  bound in as associated data, so copying owner A's ciphertext into owner B's row
  fails authentication instead of handing A's key to B.

What it does **not** defend against — stated plainly, because claiming otherwise
would be the kind of exaggeration this project is trying to avoid:

* an attacker who can read the whole filesystem **when the key file is in use**,
  because the auto-generated key sits beside the data it protects. Setting
  ``CREDENTIAL_ENCRYPTION_KEY`` removes the key from the filesystem, which is why
  it is the recommended mode;
* code execution inside the API process, which can simply ask the vault to
  decrypt — no at-rest scheme can prevent this;
* a compromised host, a malicious dependency, or a key that leaks through the
  environment of a misconfigured deployment.

This is defence in depth for data at rest, not protection against a compromised
server. The vault reports which key source it is using so the difference is
visible instead of assumed.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: AES-256 needs exactly 32 bytes.
KEY_BYTES = 32
#: 96 bits, the size AES-GCM is specified for. Random per encryption.
NONCE_BYTES = 12
#: Environment variable holding the master key, checked before the key file.
ENV_KEY_VARIABLE = "TRAVER_CREDENTIAL_KEY"
#: Bumped if the key derivation or storage format ever changes, so old rows stay
#: readable instead of being silently mis-decrypted.
CURRENT_KEY_VERSION = 1

KEY_SOURCE_ENV = "env"
KEY_SOURCE_FILE = "file"


class CredentialVaultError(Exception):
    """Base class for vault failures."""


class VaultLockedError(CredentialVaultError):
    """The vault cannot operate: no usable master key and generation is off."""


class MasterKeyError(CredentialVaultError):
    """The configured master key is malformed.

    Raised rather than quietly generating a replacement: from the user's side a
    silent replacement is indistinguishable from losing every stored credential.
    """


@dataclass(frozen=True)
class MasterKey:
    """The key material plus where it came from, so the source can be reported."""

    key: bytes
    source: str
    #: False when a POSIX mode could not be tightened (e.g. on Windows, where
    #: ``os.chmod`` does not set ACLs). Recorded rather than swallowed so the
    #: settings page does not imply a protection that is not in effect.
    permissions_restricted: bool = True


def generate_master_key() -> str:
    """Mint a master key in the storable format (urlsafe base64 of 32 bytes)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def parse_master_key(raw: str) -> bytes:
    """Decode a stored/configured master key, rejecting anything malformed."""
    value = (raw or "").strip()
    if not value:
        raise MasterKeyError("credential master key is empty")
    try:
        # Some environments mangle url-safe base64 into standard base64.
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise MasterKeyError(
            "credential master key is not valid urlsafe base64; generate one with "
            '`python -c "from travel_api.services.credentials import generate_master_key; '
            'print(generate_master_key())"`'
        ) from exc
    if len(decoded) != KEY_BYTES:
        raise MasterKeyError(
            f"credential master key must decode to {KEY_BYTES} bytes, got {len(decoded)}"
        )
    return decoded


def _restrict_permissions(path: Path) -> bool:
    """Best-effort owner-only permissions. Returns whether it took effect."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        return False
    # On Windows os.chmod cannot express an ACL, so it reports success while
    # other local accounts may still read the file. Say so instead of implying
    # POSIX-grade protection.
    return os.name != "nt"


def load_master_key(
    *,
    env_value: str | None,
    key_file: Path,
    allow_generate: bool = True,
) -> MasterKey:
    """Resolve the master key from the environment, then the key file.

    The environment wins because it is the stronger mode: the key then lives only
    in the process environment and is absent from the filesystem and from any
    database backup taken alongside it.
    """
    if env_value and env_value.strip():
        return MasterKey(parse_master_key(env_value), KEY_SOURCE_ENV)

    if key_file.exists():
        return MasterKey(
            parse_master_key(key_file.read_text(encoding="utf-8")),
            KEY_SOURCE_FILE,
            permissions_restricted=_restrict_permissions(key_file),
        )

    if not allow_generate:
        raise VaultLockedError(
            f"no credential master key: set {ENV_KEY_VARIABLE} or create {key_file}"
        )

    key_b64 = generate_master_key()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions from the start where the platform
    # supports it, so the key is never briefly world-readable.
    try:
        handle = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another worker won the race; use what it wrote rather than overwriting.
        return MasterKey(
            parse_master_key(key_file.read_text(encoding="utf-8")),
            KEY_SOURCE_FILE,
            permissions_restricted=_restrict_permissions(key_file),
        )
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(key_b64)
    return MasterKey(
        parse_master_key(key_b64),
        KEY_SOURCE_FILE,
        permissions_restricted=_restrict_permissions(key_file),
    )


def credential_aad(owner_id: str, provider_id: str, credential_key: str, key_version: int) -> bytes:
    """Associated data binding a ciphertext to its exact row.

    JSON rather than a delimiter join: ``owner_id`` is free-form, so any
    separator could also occur inside it, and two different rows colliding on the
    same AAD is precisely the swap this is meant to prevent. JSON escaping makes
    the encoding unambiguous.

    Note this is authentication, not encryption of the binding, and the values
    are already stored in clear text in their own columns. The point is that a
    ciphertext only decrypts for the row it was written for.
    """
    return json.dumps(
        [owner_id, provider_id, credential_key, key_version],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


class CredentialCipher:
    """Seals and opens individual credential values."""

    def __init__(self, master_key: bytes, key_version: int = CURRENT_KEY_VERSION) -> None:
        if len(master_key) != KEY_BYTES:
            raise MasterKeyError(f"master key must be {KEY_BYTES} bytes")
        self._aead = AESGCM(master_key)
        self.key_version = key_version

    def seal(
        self, plaintext: str, *, owner_id: str, provider_id: str, credential_key: str
    ) -> bytes:
        """Encrypt one value. A fresh random nonce is generated per call."""
        aad = credential_aad(owner_id, provider_id, credential_key, self.key_version)
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad)

    def open(self, blob: bytes, *, owner_id: str, provider_id: str, credential_key: str) -> str:
        """Decrypt one value, raising if it was tampered with or moved.

        There is no fallback and no "best effort" path: a value that fails
        authentication must be an error, because returning something instead
        would mean silently sending a wrong credential upstream.
        """
        if len(blob) <= NONCE_BYTES:
            raise CredentialVaultError("stored credential is truncated")
        aad = credential_aad(owner_id, provider_id, credential_key, self.key_version)
        nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, ciphertext, aad).decode("utf-8")
        except InvalidTag as exc:
            raise CredentialVaultError(
                "stored credential failed authentication: the master key is wrong, or the "
                "record was modified or moved to a different owner/provider/name"
            ) from exc
        except UnicodeDecodeError as exc:  # pragma: no cover - defensive
            raise CredentialVaultError("stored credential is not valid UTF-8") from exc


class CredentialVault:
    """Sealed, owner-scoped storage for a user's own third-party credentials.

    The plaintext only ever leaves this class through :meth:`reveal`, which the
    provider factories call. Every read filters on ``owner_id``, so one tenant
    cannot reach another's key even if a caller passes a stale or forged
    provider id.
    """

    def __init__(
        self,
        uow_factory,
        cipher: CredentialCipher | None,
        *,
        enabled: bool = True,
        locked_reason: str | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.cipher = cipher
        self.enabled = enabled
        self.locked_reason = locked_reason

    def _require_enabled(self) -> CredentialCipher:
        if not self.enabled or self.cipher is None:
            raise VaultLockedError(
                self.locked_reason
                or "the credential vault is locked; stored credentials cannot be read or written"
            )
        return self.cipher

    @staticmethod
    def _clean(value: str, field: str) -> str:
        text = (value or "").strip()
        if not text:
            raise CredentialVaultError(f"{field} is required")
        if len(text) > 120:
            raise CredentialVaultError(f"{field} is too long")
        return text

    def store(self, owner_id: str, provider_id: str, credential_key: str, value: str) -> None:
        """Seal and persist one credential, replacing any previous value."""
        cipher = self._require_enabled()
        owner = self._clean(owner_id, "owner_id")
        provider = self._clean(provider_id, "provider_id")
        name = self._clean(credential_key, "credential_key")
        if not (value or "").strip():
            # Storing an empty value would read as "configured" while sending an
            # empty credential upstream; deletion is the way to unset one.
            raise CredentialVaultError("credential value is required; use delete to unset it")

        sealed = cipher.seal(value, owner_id=owner, provider_id=provider, credential_key=name)
        with self.uow_factory() as uow:
            uow.credentials.upsert(
                owner_id=owner,
                provider_id=provider,
                credential_key=name,
                ciphertext=sealed,
                key_version=cipher.key_version,
            )

    def reveal(self, owner_id: str, provider_id: str, credential_key: str) -> str | None:
        """Decrypt one credential, or ``None`` when nothing is stored.

        Raises :class:`CredentialVaultError` when a row exists but does not
        authenticate. That distinction matters: "not configured" is a normal
        state to fall back from, whereas a failed authentication means the key or
        the record is wrong and must not be silently treated as absent.
        """
        cipher = self._require_enabled()
        with self.uow_factory() as uow:
            record = uow.credentials.get(owner_id, provider_id, credential_key)
            if record is None:
                return None
            return cipher.open(
                record.ciphertext,
                owner_id=record.owner_id,
                provider_id=record.provider_id,
                credential_key=record.credential_key,
            )

    def remove(self, owner_id: str, provider_id: str, credential_key: str) -> bool:
        with self.uow_factory() as uow:
            return uow.credentials.delete(owner_id, provider_id, credential_key)

    def configured_keys(self, owner_id: str, provider_id: str) -> set[str]:
        """Names of the credentials stored for one source. Never values."""
        with self.uow_factory() as uow:
            return {
                record.credential_key
                for record in uow.credentials.list_for_owner(owner_id)
                if record.provider_id == provider_id
            }

    def configured_by_provider(self, owner_id: str) -> dict[str, set[str]]:
        """Every stored credential name for an owner, grouped by source."""
        with self.uow_factory() as uow:
            grouped: dict[str, set[str]] = {}
            for record in uow.credentials.list_for_owner(owner_id):
                grouped.setdefault(record.provider_id, set()).add(record.credential_key)
            return grouped


@dataclass(frozen=True)
class EffectiveCredential:
    """A credential that is actually in effect, and where it came from."""

    value: str
    source: str

    @property
    def from_stored(self) -> bool:
        return self.source == SOURCE_STORED


#: Where a credential came from. Reported to the UI so "the environment is
#: supplying this" and "you set this in the app" are distinguishable — the two
#: have different scopes, and a value that looks configured but is an operator
#: default is a misunderstanding waiting to happen.
SOURCE_STORED = "stored"
SOURCE_ENV = "env"

#: ``(provider_id, credential_key)`` -> the ``Settings`` attribute holding the
#: environment default. One table, so "where can this credential come from"
#: cannot drift between the resolver, the catalogue and the docs.
ENV_DEFAULTS: dict[tuple[str, str], str] = {
    ("openai-compatible", "ai_base_url"): "ai_base_url",
    ("openai-compatible", "ai_model"): "ai_model",
    ("openai-compatible", "ai_api_key"): "ai_api_key",
}


class CredentialResolver:
    """Effective credentials for one owner: stored value wins over environment.

    This is what makes the settings page work without a restart. The environment
    stays the fallback an operator can ship in a container image, and anything a
    user saves in the app overrides it for that owner only — so one deployment can
    serve several people with different models and different keys.
    """

    def __init__(self, vault: CredentialVault, settings) -> None:
        self.vault = vault
        self.settings = settings

    def env_value(self, provider_id: str, credential_key: str) -> str | None:
        """The environment's value, only when it *explicitly* supplied one.

        Only explicitly-supplied values count. ``Settings.ai_model`` defaults to
        ``"gpt-4o-mini"``, so a fresh install has a value for it that nobody chose;
        reporting that as "configured from the environment" would be a plain
        falsehood in the settings UI — the exact kind of quiet overstatement this
        project is trying to avoid. ``model_fields_set`` separates "supplied" from
        "defaulted", which is the distinction that matters here.
        """
        attribute = ENV_DEFAULTS.get((provider_id, credential_key))
        if attribute is None:
            return None
        if not self._explicitly_set(attribute):
            return None
        raw = getattr(self.settings, attribute, None)
        if raw is None:
            return None
        text = str(raw).strip()
        return text or None

    def _explicitly_set(self, attribute: str) -> bool:
        """Whether a settings field was supplied rather than defaulted.

        A plain object without pydantic's bookkeeping is treated as fully
        explicit, which is the honest reading of a stub: if it carries the
        attribute, the caller put it there.
        """
        fields_set = getattr(self.settings, "model_fields_set", None)
        if fields_set is None:
            return True
        return attribute in fields_set

    def get(
        self, owner_id: str, provider_id: str, credential_key: str
    ) -> EffectiveCredential | None:
        """Resolve one credential, or ``None`` when neither source has a value.

        A stored value is attempted first and its failure is *not* swallowed: if
        an owner has saved a key and it cannot be decrypted, silently falling back
        to the operator's environment key would run the user's requests against
        someone else's account. Better to surface the error.
        """
        stored = self.vault.reveal(owner_id, provider_id, credential_key)
        if stored:
            return EffectiveCredential(stored, SOURCE_STORED)
        env = self.env_value(provider_id, credential_key)
        if env:
            return EffectiveCredential(env, SOURCE_ENV)
        return None

    def value(self, owner_id: str, provider_id: str, credential_key: str) -> str | None:
        found = self.get(owner_id, provider_id, credential_key)
        return found.value if found else None

    def configured(self, owner_id: str, provider_id: str) -> dict[str, str]:
        """Which of a source's credentials are in effect, and from where.

        Returned so the catalogue can report "configured" without a second,
        separately-maintained definition of what counts as configured.
        """
        stored = self.vault.configured_by_provider(owner_id).get(provider_id, set())
        return {key: SOURCE_STORED for key in stored}


def configured_credentials(resolver: CredentialResolver, owner_id: str) -> dict[str, set[str]]:
    """Every credential in effect for one owner, grouped by source. Names only.

    The union of what the owner stored and what the environment supplies, because
    both are genuinely in effect and the settings page has to show that. Kept here
    rather than in the route so the catalogue and the settings form cannot end up
    with two different opinions about what "configured" means.
    """
    grouped: dict[str, set[str]] = {
        provider: set(keys)
        for provider, keys in resolver.vault.configured_by_provider(owner_id).items()
    }
    for provider_id, credential_key in ENV_DEFAULTS:
        if resolver.env_value(provider_id, credential_key):
            grouped.setdefault(provider_id, set()).add(credential_key)
    return grouped


__all__ = [
    "CURRENT_KEY_VERSION",
    "ENV_DEFAULTS",
    "ENV_KEY_VARIABLE",
    "KEY_BYTES",
    "KEY_SOURCE_ENV",
    "KEY_SOURCE_FILE",
    "NONCE_BYTES",
    "SOURCE_ENV",
    "SOURCE_STORED",
    "CredentialCipher",
    "CredentialResolver",
    "CredentialVault",
    "CredentialVaultError",
    "EffectiveCredential",
    "MasterKey",
    "MasterKeyError",
    "VaultLockedError",
    "configured_credentials",
    "credential_aad",
    "generate_master_key",
    "load_master_key",
    "parse_master_key",
]
