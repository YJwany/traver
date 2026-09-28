# `db/seeds/` — intentionally empty

No seed scripts exist, and none are needed yet.

The previous note here said seeds would arrive "after persistence repositories
are introduced". That condition is already met — `db/repositories.py`,
`db/uow.py` and four Alembic migrations all exist — so the note was stale.

## Why there is nothing to seed

- **Deterministic test data** comes from `providers/mock.py` and
  `providers/fixture.py`, wired per-test through `Settings`. It never touches a
  shared database, so tests cannot contaminate each other or a developer's data.
- **Real data** comes from the free keyless providers at request time and is
  persisted per trip. There is no reference dataset to preload: Overpass,
  Open-Meteo, OSRM and Wikipedia are queried live, and the `holidays` calendar is
  bundled with its library.

Seeding would therefore duplicate a source of truth that already exists, and add
a path where a developer's database disagrees with what the test suite verifies.

## If seeds become necessary

A seed script earns its place when Traver needs data that no provider supplies
and that must exist before the first request — for example a curated city list to
drive the UI before any trip is created. If that happens, add it here as a
migration-adjacent script that is idempotent, and give it a test asserting that
running it twice changes nothing.
