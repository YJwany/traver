# `services/` — intended seams, not running services

**Nothing is deployed from this directory.** Every capability listed below is
implemented today inside the API application under `apps/api/src/travel_api/`.
This directory records the module boundaries that already exist *in the code*,
so a later extraction into an independently deployable worker is a move rather
than a redesign.

This file used to be ten separate per-directory READMEs. Several had gone stale
and described as future work things that were already built — `ai_engine` listed
the model adapter, tool registry and revision orchestration as "reserved" long
after all three shipped. One file per root is deliberately harder to let rot.

| Seam | Implemented today | Extraction trigger |
|---|---|---|
| Provider adapters | `providers/` (+ `providers/free/`) | A source needs its own scaling or credential boundary |
| Normalization | `services/normalization` logic within `providers/` and `services/aggregation.py` | Canonical entities are persisted independently of a trip |
| Entity resolution | `services/entity_resolution.py` | Matching needs a review queue or offline re-clustering |
| Decision | `services/decision_engine.py` | Candidate ranking outgrows in-process scoring |
| Budget | `services/budget_engine.py` | Financial reporting needs its own service of record |
| Itinerary optimization | `services/route_optimizer.py` | A real solver (OR-Tools) replaces the greedy heuristic |
| AI orchestration | `services/travel_ai.py`, `ai/`, `services/tool_registry.py` | Model traffic needs independent scaling or rate isolation |

## Why the code is co-located for now

The pipeline is Provider → Normalization → Entity Resolution → Enrichment →
Decision → Route/Budget → AI → API. Each stage shares the domain models in
`domain/models.py`, and every stage is currently cheap enough to run in one
process. Splitting now would mean duplicating those models or inventing a
serialization contract before the shapes have settled — cost without benefit.

The genuine reason to extract a stage is a *different operational profile*: the
AI stage is the first candidate, because it consumes paid model tokens and is the
one stage whose latency and cost a caller can influence directly.
