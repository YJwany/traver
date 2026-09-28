"""Per-owner model resolution.

Two things changed at once when credentials became storable per owner, and both
have to be reflected here:

1. **The model is no longer a process-wide singleton.** A single deployment can
   serve several people, each with their own provider and key, so "the adapter"
   cannot be built once at startup. It is resolved per request from that owner's
   effective credentials.
2. **Changing the model must not require a restart.** Because resolution happens
   per request, saving a key in the settings page takes effect on the next
   message. There is deliberately no cache: one AES-GCM open plus a primary-key
   lookup is far cheaper than an HTTP call, and a cache is exactly how "I saved
   it but it still uses the old key" bugs get introduced.

Distinguishing the two failure modes is the important part of this module.
"Nothing configured" is a normal, supported state — the local deterministic
planner takes over and the trip is still planned, which is the whole point of
progressive enhancement. "Configured but unusable" is an error the user needs to
hear about, and it must **not** be resolved by quietly borrowing the operator's
own key, which would run one person's requests against another account.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from travel_api.ai import OpenAICompatibleAdapter
from travel_api.services.ai_contracts import AIProvider, AIRequest
from travel_api.services.credentials import (
    SOURCE_STORED,
    CredentialResolver,
    CredentialVaultError,
    EffectiveCredential,
)

#: Manifest id for the OpenAI-compatible model source. Declared in
#: ``providers/manifest.py``; kept as a constant here so the two cannot drift by
#: typo without a test noticing.
MODEL_PROVIDER_ID = "openai-compatible"

KEY_BASE_URL = "ai_base_url"
KEY_API_KEY = "ai_api_key"
KEY_MODEL = "ai_model"


def _combined_source(sources: dict[str, str]) -> str:
    """Summarise per-credential origins in one word.

    "stored" as soon as anything came from the app, because that is the fact a
    user cares about: their own configuration is in play. Per-credential detail is
    in ``ResolvedModel.sources`` for when the difference matters.
    """
    if any(origin == SOURCE_STORED for origin in sources.values()):
        return SOURCE_STORED
    return "env" if sources else "none"


class ModelUnavailableError(Exception):
    """A model *is* configured for this owner, but cannot be used.

    Carries a message intended for the person who configured it, because the
    difference between "you have not set a model up" and "the key you saved
    cannot be read" is the difference between working as designed and silently
    losing the user's data.
    """


@dataclass(frozen=True)
class ResolvedModel:
    """The outcome of resolving an owner's model configuration."""

    adapter: AIProvider | None
    source: str | None = None
    #: Per-credential origin, e.g. ``{"ai_base_url": "env", "ai_api_key": "stored"}``.
    #: Kept because "the environment is supplying your key" and "you set it in the
    #: app" look identical in a single flag, and that is exactly the confusion
    #: that makes someone edit a setting that has no effect.
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return self.adapter is not None


@dataclass(frozen=True)
class ModelProbe:
    """Result of actually calling the model, for the settings page's "test" button."""

    ok: bool
    model: str = ""
    latency_ms: float | None = None
    reply: str | None = None
    error: str | None = None


class ModelRuntime:
    """Builds a model adapter from an owner's effective credentials.

    ``defaults`` is the ``Settings`` object, which is what makes the environment
    still work as a fallback an operator can bake into an image. Resolution goes
    through :class:`CredentialResolver`, so a value saved in the app wins over
    the environment for that owner only.
    """

    def __init__(
        self,
        resolver: CredentialResolver,
        *,
        default_base_url: str | None,
        default_model: str,
        default_timeout_seconds: float = 30.0,
        default_max_tokens: int = 1200,
    ) -> None:
        self.resolver = resolver
        self.default_base_url = default_base_url
        self.default_model = default_model
        self.default_timeout_seconds = default_timeout_seconds
        self.default_max_tokens = default_max_tokens

    def _effective(self, owner_id: str) -> dict[str, EffectiveCredential]:
        """The credentials that are actually in effect for this owner.

        Raises :class:`ModelUnavailableError` when a stored value exists but
        cannot be read. The ``CredentialResolver`` deliberately does not fall back
        to the environment in that case, and the reason is propagated rather than
        downgraded to "unconfigured" — otherwise a user whose key broke would
        silently start spending the operator's quota.
        """
        found: dict[str, EffectiveCredential] = {}
        try:
            for key in (KEY_BASE_URL, KEY_API_KEY, KEY_MODEL):
                credential = self.resolver.get(owner_id, MODEL_PROVIDER_ID, key)
                if credential is not None:
                    found[key] = credential
        except CredentialVaultError as exc:
            raise ModelUnavailableError(
                f"the saved model credential could not be read, so no model is in use: {exc}"
            ) from exc
        return found

    def resolve(self, owner_id: str | None) -> ResolvedModel:
        """Return the model for this owner, or a reason there is none.

        ``owner_id`` is ``None`` when authentication is switched off; the vault is
        keyed by owner, so that case reads the environment only.
        """
        owner = owner_id or ""
        effective = self._effective(owner)
        sources = {key: value.source for key, value in effective.items()}

        base_url = effective.get(KEY_BASE_URL)
        if base_url is None:
            # No base URL means no model: there is nothing to guess, and the local
            # planner handles it. Not a warning state.
            return ResolvedModel(None, source="none", sources=sources)

        api_key = effective.get(KEY_API_KEY)
        model = effective.get(KEY_MODEL)
        return ResolvedModel(
            OpenAICompatibleAdapter(
                base_url=base_url.value,
                # An empty key is legitimate: local OpenAI-compatible servers are
                # keyless. Requiring one made the most common self-hosted model
                # impossible to use.
                api_key=api_key.value if api_key else "",
                model=model.value if model else self.default_model,
                timeout_seconds=self.default_timeout_seconds,
                max_tokens=self.default_max_tokens,
            ),
            source=_combined_source(sources),
            sources=sources,
        )

    def adapter_for(self, owner_id: str | None) -> AIProvider | None:
        """Factory used by the AI service.

        Returns ``None`` for the ordinary unconfigured case. A stored credential
        that cannot be read raises from ``resolve`` and propagates, so the service
        warns the user instead of pretending everything is fine.
        """
        return self.resolve(owner_id).adapter

    async def probe(self, owner_id: str | None) -> ModelProbe:
        """Actually call the model once, so "test connection" means something.

        A test that only checked a key's shape would be theatre. This spends one
        small request and reports what came back, including the latency and the
        model the provider says it used.
        """
        try:
            resolved = self.resolve(owner_id)
        except ModelUnavailableError as exc:
            return ModelProbe(ok=False, error=str(exc))

        if resolved.adapter is None:
            return ModelProbe(
                ok=False,
                error=(
                    "no model configured; the local deterministic planner is in use, "
                    "so trips are still planned without one"
                ),
            )

        try:
            response = await resolved.adapter.complete(
                AIRequest(
                    system_instruction=(
                        "Reply with the single word: ok. Do not add anything else."
                    ),
                    user_input="ping",
                    max_tokens=16,
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported to the user verbatim
            return ModelProbe(ok=False, error=f"{type(exc).__name__}: {exc}")

        return ModelProbe(
            ok=True,
            model=response.model or "",
            latency_ms=response.latency_ms,
            reply=(response.text or "").strip()[:200] or None,
        )


__all__ = [
    "KEY_API_KEY",
    "KEY_BASE_URL",
    "KEY_MODEL",
    "MODEL_PROVIDER_ID",
    "ModelProbe",
    "ModelRuntime",
    "ModelUnavailableError",
    "ResolvedModel",
]
