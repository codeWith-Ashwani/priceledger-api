# PriceLedger API

Private development backend for India product comparison and transparent price history. Python 3.12+, FastAPI, SQLAlchemy and PostgreSQL; SQLite supports local development without extra services.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Health: http://127.0.0.1:8000/api/v1/health. API reference: http://127.0.0.1:8000/docs.

Optional configuration is in `.env.example`; copy it to `.env`. Credentials and local runtime data are excluded from Git. Development mode is explicit; approved data integrations will be introduced separately.

## Verify

```sh
uv run pytest -q
```

Frontend repository: https://github.com/codeWith-Ashwani/priceledger-web. Research and sprint planning remain local. Both repositories stay private until the owner explicitly changes their visibility.
