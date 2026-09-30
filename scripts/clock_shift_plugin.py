"""Run the suite with the clock shifted, to find tests that only pass today.

Why this exists
---------------
`test_free_providers.py` had an assertion that could only be green for three days:

    assert reference.data_age_seconds < 3 * 86400

The fixture's snapshot is a fixed instant (``2026-09-27T16:13:33Z``) and ``data_age_seconds`` is
measured back from ``now``, so the assertion was about *when the suite ran*, not about what the
code did. It went red at 3.09 days. A second expiry was hidden behind it in the same test: a
*ratio* between two durations-before-now decays toward 1 and would have failed about nine days
later.

Neither was caught by reading the test, because both look like ordinary bounds. They are a family:
**a fixture holds an absolute instant and an assertion compares it against the moving present.**
The only reliable way to find the rest of the family is to move the present and see what breaks.

What it does
------------
`pytest_collection_modifyitems` runs after every test module is imported, so by then the module
globals are settled. It replaces the ``datetime`` and ``date`` names in every already-imported
module with subclasses whose ``now()``/``utcnow()``/``today()`` are offset. Both the tests and
``travel_api`` are patched, so the program and the assertions agree on what "now" is — what changes
is only the distance from the fixture's hardcoded instants.

Usage::

    CLOCK_SHIFT_DAYS=730 python -m pytest tests/ -q -p clock_shift_plugin

A failure under a shift is not automatically a bug: a test may legitimately construct
``now - 10 minutes`` and assert the result. Those keep passing, because both sides move together.
What fails is a test whose fixture is pinned to a calendar date — which is exactly the set worth
looking at.

Only forward shifts are meaningful
---------------------------------
Use a **positive** ``CLOCK_SHIFT_DAYS``. The forward direction is the only one that can happen,
and it is the one that finds real expiries: the two bombs above had fuses of 3 days and
2027-05-16.

A backwards shift reports failures that are artefacts, and they are worth recognising so nobody
chases them:

  * ``data_age_seconds`` clamps at zero (*"a source reporting a time slightly in our future must
    read as 'just now'"*), so a fixture dated after the shifted now reads as age 0 and any
    invariant relating the snapshot to the elements shifts by exactly the offset.
  * A fixture that pins a past instant to assert staleness — ``expires_at=datetime(2020, 1, 1)``
    — stops being in the past, so ``is_stale`` correctly becomes ``False``.

Both say "the clock went backwards", which cannot happen. Measured: ``-365`` and ``-3650`` fail
these; ``+730`` and ``+3650`` pass clean.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta

_SHIFT = timedelta(days=float(os.environ.get("CLOCK_SHIFT_DAYS", "0")))

#: The real classes, captured before anything is replaced, so the shims call the true clock.
_REAL_DATETIME = datetime
_REAL_DATE = date


class ShiftedDatetime(_REAL_DATETIME):
    """A `datetime` whose notion of now is offset, and which is otherwise the real thing.

    A subclass rather than a fake object, so `isinstance` checks, arithmetic, `isoformat()`,
    comparison and Pydantic's validation all keep working — the tests must fail for the reason
    being investigated, not because a stub leaked into a model field.
    """

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return _REAL_DATETIME.now(tz) + _SHIFT

    @classmethod
    def utcnow(cls):
        return _REAL_DATETIME.utcnow() + _SHIFT


class ShiftedDate(_REAL_DATE):
    """A `date` whose `today()` is offset, for the same reason."""

    @classmethod
    def today(cls):
        return _REAL_DATE.today() + _SHIFT


def _patchable(module: object) -> bool:
    name = getattr(module, "__name__", "")
    return name.startswith(("travel_api", "test_", "conftest"))


def pytest_collection_modifyitems(config, items) -> None:  # noqa: ARG001, ANN001
    """Offset the clock for every module collected so far.

    Late on purpose: test modules import at collection time, and patching earlier would miss
    whoever imported `datetime` afterwards.
    """
    if not _SHIFT:
        return

    patched = 0
    for module in list(sys.modules.values()):
        if module is None or not _patchable(module):
            continue
        # Only replace the *class* binding (`from datetime import datetime`). A module that did
        # `import datetime` holds the module here, and swapping that for a class would break
        # `datetime.datetime(...)` in ways that have nothing to do with this check.
        if getattr(module, "datetime", None) is _REAL_DATETIME:
            module.datetime = ShiftedDatetime  # type: ignore[attr-defined]
            patched += 1
        if getattr(module, "date", None) is _REAL_DATE:
            module.date = ShiftedDate  # type: ignore[attr-defined]

    print(
        f"\n[clock_shift] now = real now {_SHIFT.days:+d} days; patched {patched} module(s)\n",
        file=sys.stderr,
    )
