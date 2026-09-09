## What and why

<!-- One paragraph. Link the ADR or issue if there is one. -->

## How I tested it

<!-- Commands run, tests added. CI must be green before review. -->

## Checklist

- [ ] I read my own diff as a reviewer (and, if an agent helped, it ran a review pass) before opening this
- [ ] I re-applied `outerloop:review` after each round of fixes until the reviewer converged (usually 4 to 5 rounds), and replied to findings I rejected
- [ ] Tests added or updated; `uv run pytest -q` passes locally
- [ ] Docs updated in the same PR (`tests/test_docs.py` passes): permissions table, event types, README tool table, ADR index
- [ ] No secrets, real recordings, or real transcripts; fixtures are synthetic
- [ ] Schema change → migration included and applies on SQLite and Postgres
- [ ] Anything deliberately left out is listed below

## Left out on purpose

<!-- Follow-ups, known gaps, decisions deferred. -->
