"""A crash-safe runner for the mutation harnesses in this directory.

Every harness here overwrites real source files, runs a test suite, and restores them. That is
fine until something goes wrong between the write and the restore, and then it is not fine at
all — this has already cost real time twice:

* A harness crashed on a malformed ``MUTATIONS`` tuple (three elements where four were expected).
  The mutation it had just applied stayed on disk. The *next* run read the file, took the mutated
  text as "the original", and — since the baseline check compares against whatever it read — its
  restore step wrote the mutation back as if it were the fix. Nothing in the output said so. The
  failure was eventually noticed only because the fixes were also visible in `git status`.
* An earlier harness reported a mutation as "NOT CAUGHT" when its own target-test list was the
  thing that was wrong, so a working guard looked broken.

So this module does four things, and each one corresponds to a way that happened:

1. **Validates the case list before touching any file.** A malformed list is a bug in the harness,
   and it must not be able to leave a source file modified.
2. **Keeps a backup beside the original** and restores from *that*, not from whatever is currently
   on disk. If a previous run died, the backup is the truth and the file is the suspect — the
   reverse of the assumption that broke things.
3. **Refuses to claim a result it cannot justify.** The baseline must pass before anything is
   mutated, and a case whose ``old`` text does not appear exactly once is reported as unapplied
   rather than silently skipped or, worse, reported as caught.
4. **Restores in a ``finally``**, so an exception, a ``KeyboardInterrupt`` or a failing test still
   leaves the tree as it was.

Usage::

    from _mutation_harness import Paths, Case, run_harness

    if __name__ == "__main__":
        raise SystemExit(run_harness(
            files=(SOME_FILE, OTHER_FILE),
            cases=[Case("M1 ...", SOME_FILE, old, new), ...],
            test_targets="tests/unit/test_x.py",
        ))
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_SRC = ROOT / "apps/api/src"
BACKUP_SUFFIX = ".mutation-backup"


@dataclass(frozen=True)
class Case:
    """One mutation: a literal replacement in one file.

    ``tests`` overrides the harness-wide target for this case. That exists because a case is
    usually aimed at a *specific producer*, and running an unrelated suite lets a different test
    fail for a different reason — which looks exactly like the mutation being caught. Where a case
    has a target, it is a claim about which suite is expected to notice.
    """

    name: str
    target: pathlib.Path
    old: str
    new: str
    tests: str | None = None

    def __post_init__(self) -> None:
        # Shape first. This is the check whose absence let a bad case list leak a mutation.
        for field, value in (("name", self.name), ("old", self.old), ("new", self.new)):
            if not isinstance(value, str):
                raise TypeError(f"{self.name!r} has a non-string {field}: {value!r}")
        if self.tests is not None and not isinstance(self.tests, str):
            raise TypeError(f"{self.name!r} has a non-string tests: {self.tests!r}")
        if not self.name.strip():
            raise ValueError("a case must have a name")
        if not self.target.is_absolute():
            raise ValueError(f"{self.name!r} target must be an absolute path")
        if self.old == self.new:
            raise ValueError(f"{self.name!r} does not change anything")


class Paths:
    """A fixed set of source files, each with a sidecar backup.

    The backup is written once, on entry, and every restore reads it. That ordering is the whole
    point: ``restore`` must not trust the file it is about to overwrite, because after a crash
    that file is the corrupt one.
    """

    def __init__(self, files: tuple[pathlib.Path, ...]) -> None:
        self.files = files
        self._backups: dict[pathlib.Path, pathlib.Path] = {}

    @staticmethod
    def _backup_path(path: pathlib.Path) -> pathlib.Path:
        return path.with_name(path.name + BACKUP_SUFFIX)

    def __enter__(self) -> Paths:
        for path in self.files:
            if not path.exists():
                raise FileNotFoundError(f"{path} does not exist; the harness target is wrong")
            backup = self._backup_path(path)
            if backup.exists():
                # A previous run died. Its mutation is on disk and its backup is the real file.
                print(
                    f"note: {backup.name} survived a previous run — restoring from the backup "
                    f"rather than trusting {path.name}",
                    file=sys.stderr,
                )
                path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            self._backups[path] = backup
        return self

    def restore(self) -> None:
        for path, backup in self._backups.items():
            path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")

    def __exit__(self, *exc_info: object) -> None:
        self.restore()
        for backup in self._backups.values():
            backup.unlink(missing_ok=True)


def _pytest(targets: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *targets.split(), "-q", "--no-header", "-p", "no:logging"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(API_SRC), "PYTHONUTF8": "1"},
    )
    return proc.returncode, proc.stdout[-1200:]


def run_harness(
    *,
    files: tuple[pathlib.Path, ...],
    cases: list[Case],
    test_targets: str,
) -> int:
    """Run every case; return 0 only if the baseline passed and every mutation was caught."""
    if not cases:
        print("no cases to run — a harness that checks nothing reports success", file=sys.stderr)
        return 1
    for case in cases:
        if case.target not in files:
            print(
                f"case {case.name!r} targets {case.target.name}, which is not in the file list; "
                "its mutation would never be restored",
                file=sys.stderr,
            )
            return 1

    with Paths(files) as paths:
        # Check the baseline for every distinct target set, not just the harness-wide one: a case
        # with its own `tests` makes a claim about *that* suite, and a suite that already fails
        # would make the case look caught.
        baselines = {case.tests or test_targets for case in cases}
        for targets in sorted(baselines):
            code, out = _pytest(targets)
            if code != 0:
                print(f"BASELINE FAILS for {targets} — the tests do not pass unmutated:\n{out}")
                return 1
        print(
            f"baseline: {len(cases)} case(s) over {len(baselines)} suite(s), all pass unmutated\n"
        )

        uncaught: list[str] = []
        for case in cases:
            # One `Paths` for the whole run, restored *before* each case rather than entered
            # afresh. Nesting two of them was the first version of this, and it broke in a way
            # worth recording: the inner `__exit__` deleted the backup the outer one still needed,
            # so the run died with a `FileNotFoundError` on a file it had just restored. The
            # crash-safety mechanism became the crash.
            paths.restore()
            text = case.target.read_text(encoding="utf-8")
            found = text.count(case.old)
            if found != 1:
                print(f"{'NOT APPLIED':11} {case.name}  ({found} matches, expected 1)")
                uncaught.append(case.name)
                continue
            case.target.write_text(text.replace(case.old, case.new), encoding="utf-8")
            code, _ = _pytest(case.tests or test_targets)
            print(f"{'caught' if code != 0 else 'NOT CAUGHT':11} {case.name}")
            if code == 0:
                uncaught.append(case.name)

    print()
    if uncaught:
        print(f"{len(uncaught)} case(s) not accounted for:")
        for name in uncaught:
            print(f"  - {name}")
        print(
            "\nBefore assuming the guard is broken, check that this harness's test_targets "
            "actually reaches the code the case mutates."
        )
        return 1
    print(f"all {len(cases)} mutations caught")
    return 0
