# iFund Integration

## Deployment architecture

The system dashboard is the browser entry point. It authenticates users and
proxies the complete iFund SPA and API surface:

```text
Browser
  -> system-dashboard :8001
     -> /ifund/* (strip /ifund prefix)
        -> iFund :8003
           -> MySQL 192.168.0.9/ifund (DB_BACKEND=mysql)
```

The proxy preserves iFund's own JWT/PAT authentication. iFund's database is
independent from fin-data and resonance. Production credentials live in
`/etc/ifund-prod.env` (chmod 600); systemd injects them via `EnvironmentFile`
(two files: backend/.env first, /etc/ifund-prod.env second so DB_* win), and
cron jobs export them explicitly (`set -a; . /etc/ifund-prod.env; set +a`).

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

## Data lineage

| Domain | Table (MySQL) | Source |
| --- | --- | --- |
| Fund master | `funds`, `fund_types` | 蛋卷/东财列表同步 |
| Details | `fund_details` (return_3m/6m/1y 唯一事实源) | 蛋卷 djapi (`ifund_cli fetch detail`) |
| NAV | `fund_nav` (33.9M rows), `fund_cum_return`, `fund_div_split` | Tushare + akshare 东财 |
| Holdings | `fund_holdings` | 东财季报 |
| AI | `fund_ai_analysis` | 本地 LLM (`ifund_cli ai-analyze`) |

Full inventory: `backend/docs/data-structure-inventory.md` and
`backend/docs/db-dictionary.md`.

## Access paths

- Direct iFund: `http://127.0.0.1:8003/` and `/api/*`
- Dashboard proxy: `http://localhost:8001/ifund/` and `/ifund/api/*`
- Dashboard requests require its normal `X-Dashboard-Token` header or login
  cookie. iFund write and user-specific APIs retain their own JWT/PAT checks.

## Known issues and operational notes

As verified on 2026-09-01 (post SQLite→MySQL migration):

- Production DB is MySQL `192.168.0.9/ifund`, selected by `DB_BACKEND=mysql`
  (injected via /etc/ifund-prod.env). Legacy SQLite `backend/data.db` is kept
  as rollback for ≥7 days.
- MySQL account `ifund` is restricted to the `ifund` database with minimal
  privileges (SELECT/INSERT/UPDATE/DELETE/CREATE/INDEX/ALTER/DROP/REFERENCES
  on `ifund.*`).
- All 5 ifund cron jobs export `/etc/ifund-prod.env` (4 inline `set -a`
  prefixes + quarterly_holdings_sync.sh loads it inside the script).
- `fund_details` has 1,164 `source_unavailable` placeholders (蛋卷不收录):
  skipped for 7 days, then auto-reprobed.
- Direct health is `/api/health`; through the dashboard it is
  `/ifund/api/health`.
