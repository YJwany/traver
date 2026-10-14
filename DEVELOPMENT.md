# Development Notes

Traver is a provider based travel planning workspace. The API normalizes facts from
configured sources, applies deterministic planning and budget rules, and exposes the
result through a versioned FastAPI API. The Next.js application consumes that API and
shows the source, freshness, and uncertainty attached to each result.

## Architecture

- `apps/api/src/travel_api/domain` contains the domain models and planning vocabulary.
- `apps/api/src/travel_api/providers` contains provider protocols, registration, health,
  and source specific adapters.
- `apps/api/src/travel_api/services` contains aggregation, freshness, routing, budgeting,
  authentication, AI orchestration, and persistence-facing workflows.
- `db/migrations` is the Alembic migration chain for the SQLAlchemy models.
- `apps/web` is the Next.js client and keeps its API contract types beside the client
  helpers and component tests.
- `tests/unit` checks pure rules and adapters; `tests/integration` exercises the API,
  migrations, ownership boundaries, and provider composition.

The application is useful without an AI service or paid provider. Fixture-backed and
deterministic paths make local development and automated tests repeatable. Optional
providers are enabled only when their own credentials are configured.

## Local Setup

The API requires Python 3.12 or newer. The web application requires Node.js 20 or newer.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m travel_api
```

Run the web application in a second terminal:

```powershell
cd apps/web
npm ci
npm run dev
```

SQLite is suitable for local work. PostgreSQL is supported by the Docker Compose setup.
Copy `.env.example` to `.env` and set only the providers needed for the current task.
Secrets and the credential-vault key stay outside version control.

## Offline Verification

The normal verification path does not call external services:

```powershell
python -m pytest -q
ruff check .
ruff format --check .
python -m compileall -q apps/api/src

cd apps/web
npm run typecheck
npm run lint
npm test
npm run build
```

Migration changes are checked separately:

```powershell
$env:DATABASE_URL = "sqlite:///./local-migration.db"
alembic upgrade head
alembic downgrade base
alembic upgrade head
```

The live provider tests are opt-in. They are not part of the offline check because they
consume upstream quotas and depend on service availability.

## Provider and Credential Rules

Every provider reports the capabilities it can actually serve, its attribution, and the
time information supplied by the source. Missing source timestamps remain unknown rather
than being replaced with the request time. A provider that needs credentials contributes
no capability until credentials are present for the current owner.

The API stores its own bearer keys as hashes. User supplied upstream keys are encrypted
at rest because the application must replay them to the upstream provider. The key file
and database files created during local runs are ignored by Git.

## Current Limitations

- Real AMap and RollingGo responses require provider credentials and are covered locally
  by deterministic transports; the live response-shape probes are opt-in.
- The in-process planning job registry is suitable for a single worker. A multi-worker
  deployment needs a shared job store.
- Browser storage is used for the local API key entry flow. A public multi-user deployment
  should replace it with an httpOnly session managed by the server.
- Multi-city planning and internationalization are future product work rather than hidden
  claims in the current capability catalog.

## Maintenance Checklist

For a behavior change, update the domain or provider contract first, add a regression test,
update the API or web contract when needed, and document any externally visible change.
For a migration, run the upgrade/downgrade/upgrade sequence and the schema drift check.
Before a release, run the offline verification commands from a clean checkout and confirm
that generated databases, credentials, build output, and local workspace notes are absent
from `git status`.
