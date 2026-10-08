# PriceLedger API

Private development backend for India product comparison and transparent price history. Python 3.12+, FastAPI, SQLAlchemy and PostgreSQL; SQLite supports local development without extra services.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
uv run alembic upgrade head
uv run python -m app.seed --demo
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Health: http://127.0.0.1:8000/api/v1/health. API reference: http://127.0.0.1:8000/docs.

Optional configuration is in `.env.example`; copy it to `.env`. Credentials and local runtime data are excluded from Git. Development mode is explicit; approved data integrations will be introduced separately.

The seed command is optional and creates six fictional product variants and synthetic history across sample retailer labels. It never fetches retailer pages or establishes actual historical prices. Repeating the seed command does not duplicate the fixtures. Set `PRICELEDGER_DEMO_MODE=false` to hide and reject demo data.

For PostgreSQL, set `PRICELEDGER_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/priceledger` in the local environment, then run migrations. Do not commit credentials. SQLite runs without extra local services; GitHub CI exercises PostgreSQL migrations, sample imports, catalog, history and assessment endpoints against a PostgreSQL 17 service.

## API

- `GET /api/v1/categories`: catalog categories.
- `GET /api/v1/products?q=&category=&limit=24&offset=0`: paginated exact variants.
- `GET /api/v1/products/{id}`: comparable current offers, freshness and history coverage.
- `GET /api/v1/products/{id}/history?days=90`: timestamped price/stock series with gap policy.
- `GET /api/v1/products/{id}/assessment`: explainable daily-median reference, coverage and recent rise/drop evidence for the best fresh matching offer.
- `POST /api/v1/ingest`: administrative batch import; disabled unless `PRICELEDGER_ADMIN_TOKEN` is configured, then requires `X-Admin-Token`. Never expose this secret to frontend code.

Imports require an approved source, its mapped offer, matching variant attributes, INR values in integer paise, an aware observation timestamp and a unique source event ID. Repeated identical events are idempotent; conflicting reuse is rejected. Backfills do not overwrite newer current prices. Shipping uncertainty, unavailable stock and stale prices remain explicit.

Assessments compare the current known delivered total with the median of daily observed delivered totals over 30 days ending seven days ago. At least 20 distinct observed days are required. The recorded low is for the selected offer over up to 365 days, not a claim of an all-time market low. A recent rise followed by a drop is an observed pattern, not proof of manipulation.

## Verify

```sh
uv run pytest -q
```

Frontend repository: https://github.com/codeWith-Ashwani/priceledger-web. Research and sprint planning remain local. Both repositories stay private until the owner explicitly changes their visibility.
