"""Shared test configuration.

One job here: keep the credential vault's key file out of the repository.

``Settings.credential_key_file`` defaults to ``.traver-credential-key`` in the
working directory, and the app creates it at startup when it is absent. That is
correct for a real deployment — the key belongs beside the data it protects — but
it meant running the suite left a real master key in the project root, because
plenty of tests build an app with no override. Ignored by git, but still a secret
materialising in the checkout as a side effect of running tests.

Pointing the default at a temporary directory for the whole session removes the
side effect without weakening what is tested: the vault tests that care about key
files pass explicit paths, and ``test_the_default_key_file_is_gitignored`` reads
the *field default* rather than an instance, so it still checks the real value.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_VAULT_DIR = Path(tempfile.mkdtemp(prefix="traver-test-keys-"))

# Set at import time, before any Settings() is constructed. ``setdefault`` so an
# explicit value from the environment still wins.
os.environ.setdefault("CREDENTIAL_KEY_FILE", str(_VAULT_DIR / "credential.key"))

atexit.register(shutil.rmtree, _VAULT_DIR, ignore_errors=True)
