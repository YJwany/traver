# `packages/` — reserved, currently empty

No package here is built, published or imported. The Web app under `apps/web`
carries its own components and types today, and the API's canonical domain models
live in `apps/api/src/travel_api/domain/models.py`.

This file replaces three per-directory READMEs (`config`, `shared-types`, `ui`)
that each claimed to "reserve" a package. Three files asserting three empty
intentions is the appearance of a monorepo without the substance; one file
stating the position is honest.

## What would justify creating these packages

| Package | Trigger |
|---|---|
| `shared-types` | The OpenAPI contract is stable and generating TS types beats hand-maintaining them |
| `ui` | A second front-end (or a published component library) needs the same Travel Workspace pieces |
| `config` | Two or more packages need identical lint/TS/build settings |

Until a trigger is real, the code belongs where it is used. Adding a workspace
package now would mean maintaining build wiring, version coordination and
publish config for something with no consumer.
