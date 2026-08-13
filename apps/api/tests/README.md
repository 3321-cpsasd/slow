# API test strategy

The API suite is grouped by the risk it protects, not by a target test count.
The current inventory is a snapshot rather than a quota:

| Group | Tests | Responsibility | Pull request frequency |
|---|---:|---|---|
| Core | 291 | Rules, module collaboration, permissions, adapters, and deterministic acceptance | API or deployment changes |
| Vertical | 78 | Workflows spanning routes, background tasks, persistence, and the learning loop | API or deployment changes |
| Database | 22 | Historical SQLite migration/repair, downgrade refusal, and SQLite-to-PostgreSQL behavior | Database, migration, or deployment changes |

All three groups run for every push to `main` and every manual CI run. A pull
request skips the database group unless it changes schema authority, migration,
database tooling, database tests, dependencies, deployment, or the CI grouping.
This keeps release coverage complete without charging every API pull request for
the historical compatibility matrix and PostgreSQL startup.

Fast deterministic acceptance tests remain in Core. Although some overlap with
a vertical journey at the feature-name level, they validate different boundaries
and add little runtime. A test should enter Vertical only when it genuinely needs
several application layers, and Database only when it validates versioned schema
behavior or a PostgreSQL difference.

Run the complete suite locally:

```bash
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests
```

Run the same groups as CI:

```bash
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests \
  -m "not api_vertical and not migration and not postgresql"
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests -m api_vertical
PYTHONPATH=apps/api .venv/bin/pytest -q apps/api/tests \
  -m "migration or postgresql"
```

Add `--durations=20` while reviewing a group. Do not add real network calls,
provider credentials, or long sleeps to these deterministic CI suites; those
belong in controlled evaluation workflows with explicit evidence handling.
