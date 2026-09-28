"""Print every hand-written copy of an API vocabulary in the web, and whether it is guarded.

Run this after adding an enum to the API, or a table/union to the web. It answers one
question: which vocabularies exist on both sides, and which of them has nothing comparing
the two?

The contract test (`tests/unit/test_web_api_contract.py`) is what actually fails the build.
This script is how the list in that file was found — eight vocabularies, only three of which
were guarded before the sweep — so it is kept as the way to find the ninth.

    python scripts/probe_shared_vocabularies.py

It is a text scan and it says so. It can miss a vocabulary spelled indirectly and it can
report a file that merely mentions a value in a comment. Both are acceptable for a tool whose
job is to point a human at candidates; neither is acceptable for a test, which is why the
test asserts on specific named pairs rather than on this script's output.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
API_SRC = REPO / "apps" / "api" / "src" / "travel_api"
WEB = REPO / "apps" / "web"
CONTRACT_TEST = REPO / "tests" / "unit" / "test_web_api_contract.py"

#: `class X(StrEnum):` (and the mixed-in `str, Enum` form) up to the next top-level class.
_ENUM = re.compile(r"^class (\w+)\((?:StrEnum|str, Enum)\):\n(.*?)(?=\nclass |\Z)", re.M | re.S)
_MEMBER = re.compile(r'^\s{4}[A-Z][A-Z0-9_]*\s*=\s*"([^"]+)"', re.M)


def api_enums() -> dict[str, set[str]]:
    enums: dict[str, set[str]] = {}
    for path in sorted(API_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name, body in _ENUM.findall(text):
            values = set(_MEMBER.findall(body))
            if values:
                enums[name] = values
    return enums


def web_sources() -> dict[Path, str]:
    sources: dict[Path, str] = {}
    for pattern in ("*.ts", "*.tsx"):
        for path in WEB.rglob(pattern):
            if "node_modules" in path.parts or ".next" in path.parts:
                continue
            sources[path] = path.read_text(encoding="utf-8", errors="replace")
    return sources


def main() -> int:
    enums = api_enums()
    sources = web_sources()
    contract = CONTRACT_TEST.read_text(encoding="utf-8")

    print(f"{len(enums)} API enums with string values, {len(sources)} web sources\n")

    unguarded: list[str] = []
    for name, values in sorted(enums.items()):
        hits = []
        for path, text in sources.items():
            found = {value for value in values if f'"{value}"' in text}
            # Half the members is the threshold: a single coincidence (a word that happens to
            # be a value) should not nominate a file, but a table missing one member should.
            if len(found) >= max(2, len(values) // 2):
                hits.append((path.relative_to(WEB), values - found))
        if not hits:
            continue

        # The test names the enum when it compares it, so its name appearing in the file is
        # the signal that something checks it.
        guarded = name in contract
        if not guarded:
            unguarded.append(name)
        status = "guarded" if guarded else "NO CONTRACT CHECK"
        print(f"{name}  ({len(values)} values)  {status}")
        for path, missing in hits:
            note = f"   missing here: {sorted(missing)}" if missing else ""
            print(f"    {path}{note}")
        print()

    print("=" * 72)
    if unguarded:
        print(f"{len(unguarded)} vocabulary/vocabularies are copied into the web and unchecked:")
        for name in unguarded:
            print(f"  - {name}")
        print(
            "\nAdd a check to tests/unit/test_web_api_contract.py, or state why one is not needed."
        )
        return 1

    print("every shared vocabulary has a contract check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
