# AITrader Admin App

FastAPI + HTMX web UI for managing AITrader at runtime. Provides admin panels
for AI model config, Telegram notifications, and cron/registry management,
backed by psycopg2 (raw SQL, no ORM). Shares mutable state with the host-side
orchestrator through a bind-mounted `AITRADER_STATE_DIR`. Bound to port 9237,
no auth — network/host-scoped trust boundary only.
