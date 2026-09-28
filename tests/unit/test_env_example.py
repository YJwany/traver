"""`.env.example` must stay in sync with `Settings`.

Written as a test rather than a one-off script because this drifted silently
before: settings were added to `Settings` while the example file kept
advertising the old set, so a reader could neither discover the new options nor
trust the documented ones. Both directions are asserted — an undocumented
setting is as much a defect as a documented one that no longer exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from travel_api.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _documented_keys() -> list[str]:
    keys: list[str] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0].strip())
    return keys


def test_env_example_exists():
    assert ENV_EXAMPLE.is_file(), f"missing {ENV_EXAMPLE}"


def test_every_setting_is_documented():
    """An undocumented setting cannot be discovered by an operator."""
    documented = {key.lower() for key in _documented_keys()}
    missing = sorted(name for name in Settings.model_fields if name.lower() not in documented)
    assert not missing, f"settings missing from .env.example: {[n.upper() for n in missing]}"


def test_every_documented_key_is_a_real_setting():
    """A documented key that does nothing actively misleads."""
    fields = {name.lower() for name in Settings.model_fields}
    unexpected = sorted(key for key in _documented_keys() if key.lower() not in fields)
    assert not unexpected, f".env.example documents unknown settings: {unexpected}"


def test_no_duplicate_keys():
    keys = [key.lower() for key in _documented_keys()]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicates, f"duplicate keys in .env.example: {duplicates}"


def test_no_secrets_are_committed_in_the_example():
    """The example file must never carry a real credential.

    Matched on a trailing `_KEY` / `_SECRET` / `_PASSWORD` / `_TOKEN` segment, not
    a substring: `AI_MAX_TOKENS` contains "TOKEN" but holds a number, so a loose
    pattern reports a false positive and trains people to ignore the check.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    pattern = re.compile(r"^([A-Z0-9_]*(?:_KEY|_SECRET|_PASSWORD|_TOKEN))=(.*)$", re.M)
    checked = 0
    for match in pattern.finditer(text):
        name, value = match.group(1), match.group(2).strip()
        checked += 1
        assert value == "" or value.lower() in {"changeme", "example"}, (
            f"{name} looks like a real secret in .env.example"
        )
    assert checked > 0, "the secret check matched no keys, so it proves nothing"


def test_auth_can_be_enabled_from_the_documented_keys_alone():
    """The documented keys must be sufficient to configure the secure mode.

    Guards against a future setting that is required for security but absent
    from the example, which would leave operators unable to enable auth by
    following the file.
    """
    documented = {key.lower() for key in _documented_keys()}
    for required in ("auth_enabled", "auth_bootstrap_key", "auth_bootstrap_owner"):
        assert required in documented, f"{required.upper()} is not documented"


@pytest.mark.parametrize(
    "line",
    [
        "AUTH_ENABLED=false",
        "AUTH_BOOTSTRAP_KEY=",
        "CORS_ALLOW_ORIGINS=",
    ],
)
def test_safe_defaults_are_shipped(line):
    """The example's defaults must be safe for a fresh clone.

    `AUTH_ENABLED=false` is the documented local-development default and is what
    /api/v1/health reports, so it is intentional rather than an oversight.
    """
    assert line in ENV_EXAMPLE.read_text(encoding="utf-8")
