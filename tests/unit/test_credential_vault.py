"""Tests for the credential vault.

These are mostly adversarial on purpose. The vault's whole value is the claim
that a leaked database does not yield working credentials, and a claim like that
is worth exactly as much as the test that tries to break it. So the interesting
cases here are not "does save/load round-trip" but:

* the plaintext must not be findable anywhere in the stored bytes;
* one owner must not be able to read another owner's key;
* a ciphertext moved to a different owner/provider/name must fail rather than
  decrypt into the wrong tenant's hands;
* a modified ciphertext must raise rather than return a plausible wrong value;
* a wrong master key must raise rather than return garbage;
* a stored-but-unreadable value must not silently fall back to the operator's
  environment key, which would run a user's request on someone else's account.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from travel_api.db.session import Database
from travel_api.db.uow import UnitOfWork
from travel_api.services.credentials import (
    ENV_DEFAULTS,
    KEY_BYTES,
    KEY_SOURCE_ENV,
    KEY_SOURCE_FILE,
    SOURCE_ENV,
    SOURCE_STORED,
    CredentialCipher,
    CredentialResolver,
    CredentialVault,
    CredentialVaultError,
    MasterKeyError,
    VaultLockedError,
    generate_master_key,
    load_master_key,
    parse_master_key,
)

OWNER_A = "owner-a"
OWNER_B = "owner-b"
PROVIDER = "openai-compatible"
KEY = "ai_api_key"
SECRET = "sk-live-9f3a1c-DO-NOT-LEAK"


# -- fixtures --------------------------------------------------------------


def _cipher(key: str | None = None) -> CredentialCipher:
    return CredentialCipher(parse_master_key(key or generate_master_key()))


@pytest.fixture
def database(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'vault.db'}")
    db.create_all()
    yield db
    db.engine.dispose()


def _vault(database, cipher: CredentialCipher | None = None) -> CredentialVault:
    return CredentialVault(lambda: UnitOfWork(database.session_factory), cipher or _cipher())


class _StubSettings:
    """Stand-in for Settings, holding only the fields the resolver reads."""

    def __init__(self, **values) -> None:
        for attribute in ENV_DEFAULTS.values():
            setattr(self, attribute, values.get(attribute))
        for name, value in values.items():
            setattr(self, name, value)


# -- storage round-trip ----------------------------------------------------


def test_a_stored_credential_comes_back_exactly(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)
    assert vault.reveal(OWNER_A, PROVIDER, KEY) == SECRET


def test_the_plaintext_is_not_present_in_the_stored_bytes(database):
    """The point of the vault: a database dump must not contain the key.

    Checks the raw column rather than the API, because a vault that stored
    plaintext and decrypted it on read would pass a round-trip test.
    """
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)

    with UnitOfWork(database.session_factory) as uow:
        record = uow.credentials.get(OWNER_A, PROVIDER, KEY)

    assert record is not None
    assert SECRET.encode() not in record.ciphertext
    # Also check the printable fragment, in case of a partial/encoded leak.
    assert b"DO-NOT-LEAK" not in record.ciphertext
    assert SECRET not in record.ciphertext.decode("latin-1")


def test_storing_the_same_value_twice_gives_different_ciphertext(database):
    """A fresh nonce per seal, so equal keys are not detectable by comparison."""
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)
    with UnitOfWork(database.session_factory) as uow:
        first = uow.credentials.get(OWNER_A, PROVIDER, KEY).ciphertext

    vault.store(OWNER_A, PROVIDER, KEY, SECRET)
    with UnitOfWork(database.session_factory) as uow:
        second = uow.credentials.get(OWNER_A, PROVIDER, KEY).ciphertext

    assert first != second
    assert vault.reveal(OWNER_A, PROVIDER, KEY) == SECRET


def test_saving_again_replaces_instead_of_duplicating(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, "first-value")
    vault.store(OWNER_A, PROVIDER, KEY, "second-value")

    assert vault.reveal(OWNER_A, PROVIDER, KEY) == "second-value"
    with UnitOfWork(database.session_factory) as uow:
        rows = uow.credentials.list_for_owner(OWNER_A)
    assert len(rows) == 1


def test_reveal_returns_none_when_nothing_is_stored(database):
    """Absent is a normal state to fall back from, unlike a failed decrypt."""
    assert _vault(database).reveal(OWNER_A, PROVIDER, KEY) is None


def test_remove_deletes_only_that_credential(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)
    vault.store(OWNER_A, PROVIDER, "ai_model", "gpt-4o-mini")

    assert vault.remove(OWNER_A, PROVIDER, KEY) is True
    assert vault.reveal(OWNER_A, PROVIDER, KEY) is None
    assert vault.reveal(OWNER_A, PROVIDER, "ai_model") == "gpt-4o-mini"
    assert vault.remove(OWNER_A, PROVIDER, KEY) is False


# -- tenant isolation ------------------------------------------------------


def test_one_owner_cannot_read_another_owners_credential(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)
    assert vault.reveal(OWNER_B, PROVIDER, KEY) is None
    assert vault.configured_keys(OWNER_B, PROVIDER) == set()


def test_the_same_provider_key_for_two_owners_stays_separate(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, "key-for-a")
    vault.store(OWNER_B, PROVIDER, KEY, "key-for-b")

    assert vault.reveal(OWNER_A, PROVIDER, KEY) == "key-for-a"
    assert vault.reveal(OWNER_B, PROVIDER, KEY) == "key-for-b"


# -- tamper and swap detection --------------------------------------------


def test_a_ciphertext_moved_to_another_owner_fails_instead_of_decrypting(database):
    """The attack AAD exists to stop.

    Without binding owner/provider/name into the ciphertext, someone with write
    access to the database could copy owner A's row into owner B's and read A's
    key through B's account. Authentication must reject it.
    """
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)

    with UnitOfWork(database.session_factory) as uow:
        stolen = uow.credentials.get(OWNER_A, PROVIDER, KEY).ciphertext

    # Simulate the row being re-pointed at another owner, as a direct UPDATE
    # by someone with database access would.
    with UnitOfWork(database.session_factory) as uow:
        uow.credentials.upsert(
            owner_id=OWNER_B,
            provider_id=PROVIDER,
            credential_key=KEY,
            ciphertext=stolen,
        )

    with pytest.raises(CredentialVaultError, match="authentication"):
        vault.reveal(OWNER_B, PROVIDER, KEY)


def test_a_ciphertext_renamed_to_another_credential_fails(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, "ai_model", "gpt-4o-mini")

    with UnitOfWork(database.session_factory) as uow:
        moved = uow.credentials.get(OWNER_A, PROVIDER, "ai_model").ciphertext
        uow.credentials.upsert(
            owner_id=OWNER_A,
            provider_id=PROVIDER,
            credential_key="ai_api_key",
            ciphertext=moved,
        )

    with pytest.raises(CredentialVaultError, match="authentication"):
        vault.reveal(OWNER_A, PROVIDER, "ai_api_key")


def test_a_modified_ciphertext_is_rejected_rather_than_decrypted(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)

    with UnitOfWork(database.session_factory) as uow:
        record = uow.credentials.get(OWNER_A, PROVIDER, KEY)
        tampered = bytearray(record.ciphertext)
        tampered[-1] ^= 0x01
        uow.credentials.upsert(
            owner_id=OWNER_A,
            provider_id=PROVIDER,
            credential_key=KEY,
            ciphertext=bytes(tampered),
        )

    with pytest.raises(CredentialVaultError, match="authentication"):
        vault.reveal(OWNER_A, PROVIDER, KEY)


def test_a_truncated_record_is_rejected(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)

    with UnitOfWork(database.session_factory) as uow:
        uow.credentials.upsert(
            owner_id=OWNER_A,
            provider_id=PROVIDER,
            credential_key=KEY,
            ciphertext=b"\x00\x01\x02",
        )

    with pytest.raises(CredentialVaultError, match="truncated"):
        vault.reveal(OWNER_A, PROVIDER, KEY)


def test_the_wrong_master_key_raises_rather_than_returning_garbage(database):
    """Losing the key must look like an error, not like a corrupted credential."""
    _vault(database).store(OWNER_A, PROVIDER, KEY, SECRET)

    other_vault = _vault(database, _cipher(generate_master_key()))
    with pytest.raises(CredentialVaultError, match="master key is wrong"):
        other_vault.reveal(OWNER_A, PROVIDER, KEY)


def test_a_disabled_vault_refuses_to_store_or_reveal(database):
    vault = CredentialVault(lambda: UnitOfWork(database.session_factory), _cipher(), enabled=False)
    with pytest.raises(VaultLockedError):
        vault.store(OWNER_A, PROVIDER, KEY, SECRET)
    with pytest.raises(VaultLockedError):
        vault.reveal(OWNER_A, PROVIDER, KEY)


# -- input validation ------------------------------------------------------


def test_an_empty_value_is_rejected(database):
    """Storing "" would read as configured while sending an empty credential."""
    vault = _vault(database)
    with pytest.raises(CredentialVaultError, match="value is required"):
        vault.store(OWNER_A, PROVIDER, KEY, "   ")


@pytest.mark.parametrize(
    ("owner", "provider", "name"),
    [("", PROVIDER, KEY), (OWNER_A, "", KEY), (OWNER_A, PROVIDER, "  ")],
)
def test_blank_identifiers_are_rejected(database, owner, provider, name):
    vault = _vault(database)
    with pytest.raises(CredentialVaultError, match="required"):
        vault.store(owner, provider, name, SECRET)


# -- names are readable, values are not -----------------------------------


def test_configured_keys_returns_names_only(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)

    names = vault.configured_keys(OWNER_A, PROVIDER)
    assert names == {KEY}
    assert SECRET not in names

    grouped = vault.configured_by_provider(OWNER_A)
    assert grouped == {PROVIDER: {KEY}}


# -- master key handling ---------------------------------------------------


def test_a_generated_master_key_is_32_bytes_of_urlsafe_base64():
    raw = generate_master_key()
    assert len(parse_master_key(raw)) == KEY_BYTES
    assert base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


@pytest.mark.parametrize(
    "bad", ["", "   ", "not base64!!", base64.urlsafe_b64encode(b"short").decode()]
)
def test_a_malformed_master_key_is_rejected_with_an_explanation(bad):
    with pytest.raises(MasterKeyError):
        parse_master_key(bad)


def test_the_environment_key_wins_over_the_key_file(tmp_path):
    """The environment mode keeps the key off the filesystem entirely."""
    key_file = tmp_path / "unused.key"
    configured = generate_master_key()

    master = load_master_key(env_value=configured, key_file=key_file)

    assert master.source == KEY_SOURCE_ENV
    assert master.key == parse_master_key(configured)
    assert not key_file.exists(), "the key file must not be touched when env is set"


def test_a_key_file_is_created_once_and_reused(tmp_path):
    key_file = tmp_path / "nested" / "credential.key"

    first = load_master_key(env_value=None, key_file=key_file)
    second = load_master_key(env_value=None, key_file=key_file)

    assert first.source == KEY_SOURCE_FILE
    # Reuse is the whole point: a second key would make stored rows unreadable.
    assert first.key == second.key
    assert key_file.exists()


def test_the_key_file_holds_the_key_in_storable_form(tmp_path):
    key_file = tmp_path / "credential.key"
    master = load_master_key(env_value=None, key_file=key_file)
    assert parse_master_key(key_file.read_text(encoding="utf-8")) == master.key


def test_generation_can_be_refused_so_the_vault_reports_being_locked(tmp_path):
    with pytest.raises(VaultLockedError):
        load_master_key(env_value=None, key_file=tmp_path / "absent.key", allow_generate=False)


def test_a_credential_survives_a_restart_through_the_key_file(tmp_path):
    """End-to-end: seal with one process's key, open with the next process's."""
    key_file = tmp_path / "credential.key"
    database = Database(f"sqlite:///{tmp_path / 'restart.db'}")
    database.create_all()

    first = load_master_key(env_value=None, key_file=key_file)
    vault = CredentialVault(
        lambda: UnitOfWork(database.session_factory), CredentialCipher(first.key)
    )
    vault.store(OWNER_A, PROVIDER, KEY, SECRET)

    second = load_master_key(env_value=None, key_file=key_file)
    reopened = CredentialVault(
        lambda: UnitOfWork(database.session_factory), CredentialCipher(second.key)
    )
    assert reopened.reveal(OWNER_A, PROVIDER, KEY) == SECRET
    database.engine.dispose()


# -- resolution: stored beats environment ---------------------------------


def test_a_stored_value_overrides_the_environment(database):
    resolver = CredentialResolver(_vault(database), _StubSettings(ai_api_key="sk-from-environment"))
    resolver.vault.store(OWNER_A, PROVIDER, KEY, "sk-from-app")

    found = resolver.get(OWNER_A, PROVIDER, KEY)
    assert found is not None
    assert found.value == "sk-from-app"
    assert found.source == SOURCE_STORED


def test_the_environment_supplies_the_value_when_nothing_is_stored(database):
    resolver = CredentialResolver(_vault(database), _StubSettings(ai_api_key="sk-from-environment"))

    found = resolver.get(OWNER_A, PROVIDER, KEY)
    assert found is not None
    assert found.source == SOURCE_ENV
    assert found.value == "sk-from-environment"


def test_nothing_configured_resolves_to_none(database):
    resolver = CredentialResolver(_vault(database), _StubSettings())
    assert resolver.get(OWNER_A, PROVIDER, KEY) is None


def test_an_unreadable_stored_value_does_not_fall_back_to_the_environment(database):
    """The security-relevant case, and the reason ``get`` is not a try/except.

    If a user has saved a key and it cannot be decrypted, falling back to the
    operator's environment key would silently bill someone else's account for
    that user's requests. Surfacing the error is the only correct behaviour.
    """
    _vault(database).store(OWNER_A, PROVIDER, KEY, "sk-stored-by-user")

    resolver = CredentialResolver(
        _vault(database, _cipher(generate_master_key())),
        _StubSettings(ai_api_key="sk-operator-environment"),
    )

    with pytest.raises(CredentialVaultError):
        resolver.get(OWNER_A, PROVIDER, KEY)


def test_two_owners_resolve_their_own_models(database):
    """One deployment, several people, different models — the point of the vault."""
    vault = _vault(database)
    resolver = CredentialResolver(vault, _StubSettings(ai_api_key="sk-operator"))
    vault.store(OWNER_A, PROVIDER, "ai_model", "deepseek-chat")
    vault.store(OWNER_B, PROVIDER, "ai_model", "qwen-plus")

    assert resolver.value(OWNER_A, PROVIDER, "ai_model") == "deepseek-chat"
    assert resolver.value(OWNER_B, PROVIDER, "ai_model") == "qwen-plus"
    # And the un-overridden key still comes from the environment for both.
    assert resolver.value(OWNER_A, PROVIDER, KEY) == "sk-operator"


def test_configured_reports_only_stored_names(database):
    vault = _vault(database)
    vault.store(OWNER_A, PROVIDER, "ai_model", "deepseek-chat")
    resolver = CredentialResolver(vault, _StubSettings())

    assert resolver.configured(OWNER_A, PROVIDER) == {"ai_model": SOURCE_STORED}
    assert resolver.configured(OWNER_B, PROVIDER) == {}


# -- the env table cannot drift from the manifests ------------------------


def test_every_environment_default_names_a_real_settings_field():
    from travel_api.config import Settings

    settings = Settings()
    for (provider_id, credential_key), attribute in ENV_DEFAULTS.items():
        assert hasattr(settings, attribute), (
            f"{provider_id}/{credential_key} maps to Settings.{attribute}, which does not exist"
        )


def test_environment_defaults_point_at_declared_credentials():
    """A mapping for a credential no manifest declares is dead configuration."""
    from travel_api.providers.manifest import manifest_for

    for provider_id, credential_key in ENV_DEFAULTS:
        manifest = manifest_for(provider_id)
        assert manifest is not None, f"ENV_DEFAULTS names unknown provider {provider_id}"
        declared = {item.key for item in manifest.credentials}
        assert credential_key in declared, (
            f"ENV_DEFAULTS maps {provider_id}/{credential_key}, which the manifest does not declare"
        )


def test_the_ai_provider_can_be_fully_supplied_from_the_environment():
    """Keeps the pre-vault deployment style working: .env alone is still enough."""
    from travel_api.providers.manifest import manifest_for

    manifest = manifest_for(PROVIDER)
    assert manifest is not None
    for item in manifest.credentials:
        assert (PROVIDER, item.key) in ENV_DEFAULTS, (
            f"{item.key} has no environment default, so a .env-only deployment "
            "could not configure it"
        )


def test_the_vault_module_does_not_import_the_web_layer():
    """Keeps crypto usable from a CLI or a script without a running app."""
    source = Path("apps/api/src/travel_api/services/credentials.py").read_text(encoding="utf-8")
    assert "fastapi" not in source


# -- the key file must never be committable ---------------------------------


def test_the_default_key_file_is_gitignored():
    """The master key decrypts every stored credential, so committing it would
    hand out all of them at once.

    Asserted rather than assumed: the default path lives in ``Settings``, and a
    change there would otherwise silently stop matching ``.gitignore`` — which is
    exactly how a key ends up in a repository. Reads the *field default* rather
    than an instance, because ``tests/conftest.py`` redirects the instance value
    to a temporary directory so the suite does not leave a real key in the
    checkout; the shipped default is what must be ignored.
    """
    from travel_api.config import Settings

    default = Settings.model_fields["credential_key_file"].default
    default_name = Path(default).name
    lines = {
        line.strip()
        for line in Path(".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert default_name in lines, (
        f"the default credential key file {default_name!r} is not in .gitignore"
    )
    # The example file must ship the same default, or the documented path and the
    # ignored path could drift apart.
    example = Path(".env.example").read_text(encoding="utf-8")
    assert f"CREDENTIAL_KEY_FILE={default}" in example, (
        f".env.example does not document CREDENTIAL_KEY_FILE={default}"
    )


def test_git_does_not_track_the_key_file():
    """Belt and braces: if a key file exists, it must not be tracked by git."""
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", "-q", ".traver-credential-key"],
        check=False,
        capture_output=True,
        text=True,
    )
    # Exit code 0 means "ignored", which is what we want. Code 1 means it is NOT
    # ignored, which would be a real leak risk. A missing git binary (128) is
    # reported rather than silently treated as a pass.
    assert result.returncode == 0, (
        ".traver-credential-key is not matched by .gitignore, so a generated master "
        f"key could be committed (git exit {result.returncode})"
    )
