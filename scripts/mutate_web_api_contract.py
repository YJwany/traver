"""Mutation check: does the web/API contract test actually catch the pace mismatch?

Restores the exact defect that shipped — the form offering "packed" while the API's Pace
enum is relaxed/balanced/full — and asserts the test fails. A contract test that passes
both before and after the bug is not a contract test.

Encoded in Python rather than PowerShell: a Get-Content/Set-Content round trip reads UTF-8
as the ANSI codepage here, which silently mangles every Chinese character in the file.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(r"C:\Users\27875\Desktop\Traver")
TARGET = ROOT / "apps" / "web" / "app" / "components" / "plan-form.tsx"
TEST = "tests/unit/test_web_api_contract.py"


def run_test() -> tuple[int, str]:
    # A stripped-down environment breaks Winsock in the child (`WinError 10106`), so the
    # full environment is inherited and only these two are overridden.
    env = dict(__import__("os").environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(ROOT / "apps" / "api" / "src")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--no-header", "-p", "no:randomly"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main() -> None:
    original = TARGET.read_text(encoding="utf-8")
    backup = pathlib.Path(tempfile.mkdtemp()) / "plan-form.tsx"
    shutil.copy2(TARGET, backup)

    try:
        # Baseline: the fixed file must pass.
        code, output = run_test()
        print(f"baseline (fixed): exit {code}")
        assert code == 0, f"the contract test fails on the fixed file:\n{output[-800:]}"

        mutations = {
            "third pace reverted to 'packed'": (
                'value: "full", label: "紧凑"',
                'value: "packed", label: "紧凑"',
            ),
            "a pace removed entirely": (
                '  { value: "full", label: "紧凑", hint: "尽量多走" },\n',
                "",
            ),
            "travel mode value drifted from the enum": (
                'value: "transit"',
                'value: "bus"',
            ),
            "travel mode no longer submitted": (
                "profile.travel_mode = values.travel_mode",
                "profile.travelModeIgnored = values.travel_mode",
            ),
        }

        failures = 0
        for label, (old, new) in mutations.items():
            if old not in original:
                print(f"  SKIP  {label}: anchor not found")
                failures += 1
                continue
            TARGET.write_text(original.replace(old, new, 1), encoding="utf-8")
            code, output = run_test()
            caught = code != 0
            print(f"  {'caught' if caught else 'MISSED'}  {label} (exit {code})")
            if not caught:
                failures += 1
            assert "packed" not in TARGET.read_text(encoding="utf-8") or caught

        assert failures == 0, f"{failures} mutation(s) not caught"
        print("\nOK: every mutation was caught")
    finally:
        shutil.copy2(backup, TARGET)
        restored = TARGET.read_text(encoding="utf-8")
        assert restored == original, "restore failed — the file changed"
        print("restored")


if __name__ == "__main__":
    main()
