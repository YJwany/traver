"""Tests for per-owner model resolution.

The behaviour worth testing here is not "does it build an adapter" but the two
distinctions the module exists to make:

* "no model configured" is normal and must stay silent, because the local planner
  takes over and the product is still usable;
* "configured but unusable" must surface, and must **not** be resolved by falling
  back to the operator's own key — that would run one person's requests on
  another account.
"""

from __future__ import annotations

import pytest
from travel_api.db.session import Database
from travel_api.db.uow import UnitOfWork
from travel_api.services.ai_contracts import AIResponse
from travel_api.services.credentials import (
    CredentialCipher,
    CredentialResolver,
    CredentialVault,
    generate_master_key,
    parse_master_key,
)
from travel_api.services.model_runtime import (
    KEY_API_KEY,
    KEY_BASE_URL,
    KEY_MODEL,
    MODEL_PROVIDER_ID,
    ModelRuntime,
    ModelUnavailableError,
)

OWNER = "owner-a"


class _StubSettings:
    def __init__(self, **values) -> None:
        for name, value in values.items():
            setattr(self, name, value)


@pytest.fixture
def runtime(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'model.db'}")
    database.create_all()
    vault = CredentialVault(
        lambda: UnitOfWork(database.session_factory),
        CredentialCipher(parse_master_key(generate_master_key())),
    )
    resolver = CredentialResolver(vault, _StubSettings(ai_base_url=None, ai_api_key=None))
    yield (
        ModelRuntime(
            resolver,
            default_base_url=None,
            default_model="default-model",
            default_timeout_seconds=5.0,
            default_max_tokens=64,
        ),
        vault,
    )
    database.engine.dispose()


class _RecordingAdapter:
    """Stands in for a real provider so the probe can be tested offline."""

    def __init__(self, *, reply: str = "ok", raises: Exception | None = None) -> None:
        self.reply = reply
        self.raises = raises
        self.calls = 0

    async def complete(self, request):  # noqa: ANN001 - mirrors AIProvider
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return AIResponse(
            request_id=request.request_id,
            text=self.reply,
            model="reported-model",
            latency_ms=12.5,
        )


# -- the ordinary states ---------------------------------------------------


def test_nothing_configured_resolves_to_the_local_planner(runtime):
    """Zero-config must be a supported state, not an error."""
    model_runtime, _vault = runtime
    resolved = model_runtime.resolve(OWNER)

    assert resolved.adapter is None
    assert resolved.configured is False
    assert resolved.source == "none"
    # And the factory used by the AI service must not raise for it either.
    assert model_runtime.adapter_for(OWNER) is None


def test_a_stored_key_produces_a_usable_model(runtime):
    model_runtime, vault = runtime
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL, "https://example.invalid/v1")
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, "sk-stored")

    resolved = model_runtime.resolve(OWNER)
    assert resolved.adapter is not None
    assert resolved.adapter.base_url == "https://example.invalid/v1"
    assert resolved.adapter.api_key == "sk-stored"
    # The model name falls back to the configured default.
    assert resolved.adapter.model == "default-model"
    assert resolved.source == "stored"


def test_a_stored_model_name_overrides_the_default(runtime):
    model_runtime, vault = runtime
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL, "https://example.invalid/v1")
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, "sk-stored")
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_MODEL, "deepseek-chat")

    assert model_runtime.resolve(OWNER).adapter.model == "deepseek-chat"


def test_the_environment_default_still_works_without_the_app(runtime):
    """A container operator can still configure one model for everyone via .env."""
    model_runtime, _vault = runtime
    model_runtime.resolver.settings = _StubSettings(
        ai_base_url="https://env.invalid/v1", ai_api_key="sk-env"
    )

    resolved = model_runtime.resolve(OWNER)
    assert resolved.adapter is not None
    assert resolved.adapter.api_key == "sk-env"
    assert resolved.source == "env"


def test_a_stored_key_overrides_the_environment_key(runtime):
    """The per-user key wins, which is what makes one deployment serve several people."""
    model_runtime, vault = runtime
    model_runtime.resolver.settings = _StubSettings(
        ai_base_url="https://env.invalid/v1", ai_api_key="sk-env"
    )
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, "sk-user")

    resolved = model_runtime.resolve(OWNER)
    assert resolved.adapter.api_key == "sk-user"
    # The base URL still comes from the environment here, and `sources` says so
    # per credential — a single flag would hide that the two differ.
    assert resolved.source == "stored"
    assert resolved.sources == {KEY_BASE_URL: "env", KEY_API_KEY: "stored"}


def test_a_base_url_without_a_key_is_a_valid_keyless_model(runtime):
    """Ollama and other local servers take no key, so this must build an adapter.

    Requiring a key here made the most common self-hosted model impossible to
    configure, which is the opposite of "bring your own model".
    """
    model_runtime, vault = runtime
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL, "http://127.0.0.1:11434/v1")

    resolved = model_runtime.resolve(OWNER)
    assert resolved.adapter is not None
    assert resolved.adapter.api_key == ""
    assert model_runtime.adapter_for(OWNER) is not None


def test_a_key_with_no_base_url_is_still_unconfigured(runtime):
    """A key with nowhere to send it is not a model, and not an error either."""
    model_runtime, vault = runtime
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, "sk-orphan")

    resolved = model_runtime.resolve(OWNER)
    assert resolved.adapter is None
    assert resolved.source == "none"
    assert model_runtime.adapter_for(OWNER) is None


def test_two_owners_get_their_own_models(runtime):
    model_runtime, vault = runtime
    for owner, model in ((OWNER, "deepseek-chat"), ("owner-b", "qwen-plus")):
        vault.store(owner, MODEL_PROVIDER_ID, KEY_BASE_URL, "https://example.invalid/v1")
        vault.store(owner, MODEL_PROVIDER_ID, KEY_API_KEY, f"sk-{owner}")
        vault.store(owner, MODEL_PROVIDER_ID, KEY_MODEL, model)

    assert model_runtime.resolve(OWNER).adapter.model == "deepseek-chat"
    assert model_runtime.resolve("owner-b").adapter.model == "qwen-plus"
    assert model_runtime.resolve(OWNER).adapter.api_key == f"sk-{OWNER}"


def test_one_owners_model_does_not_become_anothers(runtime):
    """The asymmetric case, which is the one that would leak.

    Both owners having a model proves nothing about isolation — a lookup that
    ignored the owner would still return *a* model for each. What matters is that
    configuring one owner leaves the other unconfigured, because an unset owner
    silently spending someone else's quota is the failure mode that matters.

    ``resolve`` never returns ``None``; an unconfigured owner resolves to
    ``source == "none"`` with no adapter, so that pair is what is asserted.
    """
    model_runtime, vault = runtime
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL, "https://example.invalid/v1")
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, f"sk-{OWNER}")

    assert model_runtime.resolve(OWNER).adapter is not None

    other = model_runtime.resolve("owner-b")
    assert other.adapter is None, "owner B must not inherit owner A's model"
    assert other.source == "none"
    assert other.sources == {}
    # And the credential is not even visible to the other owner.
    assert vault.reveal("owner-b", MODEL_PROVIDER_ID, KEY_API_KEY) is None
    assert vault.configured_keys("owner-b", MODEL_PROVIDER_ID) == set()


def test_a_change_takes_effect_without_a_restart(runtime):
    """The reason there is no cache: saving a key must work on the next message."""
    model_runtime, vault = runtime
    assert model_runtime.resolve(OWNER).adapter is None

    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL, "https://example.invalid/v1")
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, "sk-fresh")

    assert model_runtime.resolve(OWNER).adapter.api_key == "sk-fresh"


def test_removing_the_base_url_disables_the_model_without_a_restart(runtime):
    model_runtime, vault = runtime
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL, "https://example.invalid/v1")
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, "sk-temporary")
    assert model_runtime.resolve(OWNER).adapter is not None

    vault.remove(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL)
    assert model_runtime.resolve(OWNER).adapter is None


def test_removing_only_the_key_leaves_a_keyless_model(runtime):
    """Deleting a key must not silently disable a local model.

    Since a keyless server is a valid configuration, the honest result of removing
    the key while the base URL remains is a keyless adapter — not "no model".
    """
    model_runtime, vault = runtime
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL, "http://127.0.0.1:11434/v1")
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, "sk-temporary")

    vault.remove(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY)

    resolved = model_runtime.resolve(OWNER)
    assert resolved.adapter is not None
    assert resolved.adapter.api_key == ""


# -- the security-relevant distinction ------------------------------------


def test_an_unreadable_stored_key_does_not_fall_back_to_the_environment(runtime):
    """The mistake this module is shaped to prevent.

    If the user's saved key cannot be decrypted, using the operator's environment
    key would silently run that user's requests against another account. It has to
    be an error.
    """
    model_runtime, vault = runtime
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL, "https://example.invalid/v1")
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, "sk-user")
    model_runtime.resolver.settings = _StubSettings(
        ai_base_url="https://env.invalid/v1", ai_api_key="sk-operator"
    )

    # Simulate the master key having been rotated or lost.
    model_runtime.resolver.vault.cipher = CredentialCipher(parse_master_key(generate_master_key()))

    with pytest.raises(ModelUnavailableError, match="could not be read"):
        model_runtime.resolve(OWNER)


def test_the_factory_raises_for_a_broken_configuration(runtime):
    """The AI service needs to distinguish broken from absent in order to warn."""
    model_runtime, vault = runtime
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_BASE_URL, "https://example.invalid/v1")
    vault.store(OWNER, MODEL_PROVIDER_ID, KEY_API_KEY, "sk-user")
    model_runtime.resolver.vault.cipher = CredentialCipher(parse_master_key(generate_master_key()))

    with pytest.raises(ModelUnavailableError):
        model_runtime.adapter_for(OWNER)


def test_the_factory_does_not_raise_when_only_the_environment_is_half_set(runtime):
    """A base URL with no key is a keyless model, not a broken one."""
    model_runtime, _vault = runtime
    model_runtime.resolver.settings = _StubSettings(
        ai_base_url="https://env.invalid/v1", ai_api_key=None
    )
    adapter = model_runtime.adapter_for(OWNER)
    assert adapter is not None
    assert adapter.api_key == ""


# -- the test-connection probe -------------------------------------------


@pytest.mark.asyncio
async def test_probe_reports_success_from_a_real_call(runtime):
    """A test that only inspected the key's shape would be theatre."""
    model_runtime, _vault = runtime
    recording = _RecordingAdapter(reply="ok")
    model_runtime.resolve = lambda owner: type(  # noqa: ARG005
        "R", (), {"adapter": recording, "source": "stored"}
    )()

    probe = await model_runtime.probe(OWNER)

    assert probe.ok is True
    assert recording.calls == 1, "the probe must actually call the model"
    assert probe.model == "reported-model"
    assert probe.latency_ms == 12.5
    assert probe.reply == "ok"


@pytest.mark.asyncio
async def test_probe_reports_an_unconfigured_model_plainly(runtime):
    model_runtime, _vault = runtime
    probe = await model_runtime.probe(OWNER)

    assert probe.ok is False
    assert "no model configured" in probe.error
    assert "local deterministic planner" in probe.error


@pytest.mark.asyncio
async def test_probe_surfaces_the_providers_error(runtime):
    model_runtime, _vault = runtime
    failing = _RecordingAdapter(raises=RuntimeError("401 unauthorized"))
    model_runtime.resolve = lambda owner: type(  # noqa: ARG005
        "R", (), {"adapter": failing, "source": "stored"}
    )()

    probe = await model_runtime.probe(OWNER)

    assert probe.ok is False
    assert "401 unauthorized" in probe.error
    assert "RuntimeError" in probe.error
