# iFund Integration

## Deployment architecture

The system dashboard is the browser entry point. It authenticates users and
proxies the complete iFund SPA and API surface:

```text
Browser
  -> system-dashboard :8001
     -> /ifund/* (strip /ifund prefix)
        -> iFund :8003
           -> SQLite /root/workspace/ifund/backend/data.db
```

The proxy preserves iFund's own JWT/PAT authentication. iFund's database is
independent from fin-data and resonance.

| Service | Port | Purpose |
| --- | ---: | --- |
| fin-data | 8000 | Financial data API |
| system-dashboard | 8001 | Authenticated UI and reverse proxy |
| resonance | 8002 | ETF resonance API and SPA |
| iFund | 8003 | Fund research API and SPA |

## Operations

```bash
# Start / stop / restart / inspect iFund
systemctl start ifund.service
systemctl stop ifund.service
systemctl restart ifund.service
systemctl status ifund.service

# Logs
journalctl -u ifund.service -f
tail -f /root/workspace/ifund/logs/waitress.log
```

Update procedure:

```bash
cd /root/workspace/ifund
git pull --ff-only
backend/venv/bin/pip install -r backend/requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
systemctl restart ifund.service
curl -s http://127.0.0.1:8003/api/health
```

Restart `system-dashboard.service` as well when its iFund proxy or embedding
files under `apps/dashboard/` are changed.

## Access paths

- Direct iFund: `http://127.0.0.1:8003/` and `/api/*`
- Dashboard proxy: `http://localhost:8001/ifund/` and `/ifund/api/*`
- Dashboard requests require its normal `X-Dashboard-Token` header or login
  cookie. iFund write and user-specific APIs retain their own JWT/PAT checks.

## Known issues and operational notes

As verified on 2026-07-31:

- The configured SQLite database is `backend/data.db`, not
  `data/ifund.db`.
- The database schema is healthy, but the main business tables are empty.
  Fund list/search/type, trade-calendar, and industry APIs therefore return no
  business data until the initial upstream synchronization/import is run.
- Current read routes are `/api/trade_calendar/dates` and
  `/api/stock_industry/list?keyword=...`. The older
  `/api/trade_calendar/list` and `/api/stock_industry/search` paths return 404.
- Direct health is `/api/health`; through the dashboard it is
  `/ifund/api/health`.
