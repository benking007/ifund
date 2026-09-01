"""Flask 应用工厂：注册蓝图、JWT、SQLite 建表、SPA fallback。"""
from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_jwt_extended import JWTManager


def create_app() -> Flask:
    """创建并配置 Flask 应用。"""
    # pylint: disable=import-outside-toplevel
    load_dotenv()
    backend_dir = Path(__file__).resolve().parents[1]
    static_dir = backend_dir / "static"

    app = Flask(__name__, static_folder=str(static_dir), static_url_path="")  # pylint: disable=redefined-outer-name
    secret_key = os.getenv("SECRET_KEY", "dev-secret")
    if len(secret_key) < 32:
        logging.warning(
            "SECRET_KEY 过弱（<32 字节）。对外暴露 API / 启用 PAT 前请在 .env 设置"
            "强随机密钥（如 python -c \"import secrets;print(secrets.token_hex(32))\"），"
            "否则 JWT 可被伪造。"
        )
    app.config["JWT_SECRET_KEY"] = secret_key
    app.config["JWT_TOKEN_LOCATION"] = ["headers"]
    app.config["JWT_HEADER_TYPE"] = "Bearer"
    # access token 有效期：默认 30 天（本地个人工具，免于频繁重登）。
    # 可用环境变量 JWT_EXPIRES_DAYS 调整；设为 0 则永不过期。
    try:
        expires_days = float(os.getenv("JWT_EXPIRES_DAYS", "30"))
    except ValueError:
        expires_days = 30
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = (
        False if expires_days <= 0 else datetime.timedelta(days=expires_days)
    )
    JWTManager(app)

    # SQLite 后端：启动时自动建表（幂等）。旧库的 fund_nav 先迁移列，
    # 再执行 schema，避免 schema 中的新索引引用尚不存在的列。
    from app import db as database
    db_backend = os.getenv("DB_BACKEND", "sqlite").lower()
    if db_backend == "sqlite":
        schema_sql = (backend_dir / "schema_sqlite.sql").read_text(encoding="utf-8")
        def ensure_column(table: str, column: str, definition: str) -> None:
            try:
                database.init_db(
                    f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition};'
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                message = str(exc).lower()
                if "duplicate column name" not in message and "no such table" not in message:
                    raise

        ensure_column("fund_nav", "adj_nav", "FLOAT")
        ensure_column("fund_nav", "adj_src", "TEXT")
        database.init_db(schema_sql)
        # 增量迁移：portfolios 表加 cap 列（已存在则跳过）
        ensure_column("portfolios", "cap", "REAL DEFAULT 0.18")
    elif db_backend == "mysql":
        schema_sql = (backend_dir / "schema_sqlite.sql").read_text(encoding="utf-8")
        database.init_db(schema_sql)

    # 注册蓝图
    from app.routers.auth import bp as auth_bp
    from app.fund.api.router import bp as fund_bp
    from app.fund_detail.api.router import bp as fund_detail_bp
    from app.fund_holdings.api.router import bp as holdings_bp
    from app.fund_nav.api.router import bp as nav_bp
    from app.trade_calendar.api.router import bp as calendar_bp
    from app.stock_industry.api.router import bp as industry_bp
    from app.cluster.api.router import bp as cluster_bp
    from app.position.api.router import bp as position_bp
    from app.reconcile.api.router import bp as reconcile_bp
    from app.ai_analyze.router import bp as ai_analyze_bp
    from app.perpetual.api.router import bp as perpetual_bp
    from app.fund_manager.api.router import bp as fund_manager_bp
    from app.fund_etf_linkage.api.router import bp as fund_linkage_bp
    from app.trade_dates.api.router import bp as trade_dates_bp
    for blueprint in (auth_bp, fund_bp, fund_detail_bp, holdings_bp, nav_bp,
                      calendar_bp, industry_bp, cluster_bp, position_bp, reconcile_bp,
                      ai_analyze_bp, perpetual_bp, fund_manager_bp, fund_linkage_bp,
                      trade_dates_bp):
        app.register_blueprint(blueprint)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/stats")
    def api_stats():
        """数据完整性统计（供 fin-data 数据大盘聚合）：各表行数、最新日期、最近抓取任务。

        注意：data.db 达 6.3GB 且 waitress 服务持有连接锁，普通连接查询会被阻塞；
        这里用 immutable=1 只读快照（不参与锁协议），统计值可能滞后 WAL 中少量未合并写入。
        所有聚合查询走索引（避免全表 COUNT 全扫 6.3GB）。
        """
        import sqlite3
        db_path = os.getenv("DB_PATH") or str(backend_dir / "data.db")
        conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True, timeout=5)
        cur = conn.cursor()

        def q(sql: str, args: tuple = ()) -> object:
            try:
                cur.execute(sql, args)
                r = cur.fetchone()
                return r[0] if r else None
            except Exception:  # pylint: disable=broad-exception-caught
                return None

        def rows(sql: str, args: tuple = ()) -> list[dict]:
            try:
                cur.execute(sql, args)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception:  # pylint: disable=broad-exception-caught
                return []

        try:
            nav_total = q(
                "SELECT COUNT(fund_code) FROM fund_nav "
                "INDEXED BY ix_fund_nav_fund_code"
            ) or 0
            nav_adj = q(
                "SELECT COUNT(adj_nav) FROM fund_nav "
                "INDEXED BY ix_fund_nav_adj_nav"
            ) or 0
            out = {
                "funds": q("SELECT COUNT(*) FROM funds"),
                "fund_types": q("SELECT COUNT(*) FROM fund_types"),
                "fund_nav_latest": q("SELECT MAX(trade_date) FROM fund_nav"),
                "fund_nav_today": q(
                    "SELECT COUNT(*) FROM fund_nav WHERE trade_date = "
                    "(SELECT MAX(trade_date) FROM fund_nav)"
                ),
                "fund_nav_has_recent": q(
                    "SELECT 1 FROM fund_nav WHERE trade_date >= date('now', '-90 day') "
                    "LIMIT 1"
                ),
                "holdings_latest_quarter": q("SELECT MAX(quarter) FROM fund_holdings"),
                "snapshots": q("SELECT COUNT(*) FROM fund_snapshots"),
                "cum_return_has": q("SELECT 1 FROM fund_cum_return LIMIT 1"),
                "trade_dates_latest": q("SELECT MAX(trade_date) FROM trade_dates"),
                "adj_nav_coverage": {
                    "non_null": nav_adj,
                    "total": nav_total,
                    "ratio": round(nav_adj / nav_total, 6) if nav_total else 0.0,
                },
                # zero_nav_funds = 在 funds 中没有任何正的单位净值记录的基金数。
                # fund_code/nav 覆盖索引使 NOT EXISTS 不回表扫 fund_nav 全历史。
                "zero_nav_funds": q(
                    "SELECT COUNT(*) FROM funds AS f WHERE NOT EXISTS ("
                    "SELECT 1 FROM fund_nav AS n INDEXED BY ix_fund_nav_fund_nav "
                    "WHERE n.fund_code = f.code AND n.nav IS NOT NULL AND n.nav > 0"
                    ")"
                ),
                "repair_pending": q(
                    "SELECT COUNT(*) FROM nav_repair_queue "
                    "INDEXED BY ix_nav_repair_queue_status_retry WHERE status = 'pending'"
                ),
                "repair_failed": q(
                    "SELECT COUNT(*) FROM nav_repair_queue "
                    "INDEXED BY ix_nav_repair_queue_status_retry WHERE status = 'failed'"
                ),
                "tasks": rows(
                    "SELECT task_type, status, target_count, success_count, fail_count, "
                    "created_at, updated_at FROM fetch_tasks ORDER BY id DESC LIMIT 8"
                ),
            }
            conn.close()
            return jsonify(out)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            try:
                conn.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            return jsonify({"error": f"stats_unavailable: {exc}"}), 500

    @app.errorhandler(404)
    def spa_fallback(_err):
        if request.path.startswith("/api"):
            return jsonify({"detail": "not found"}), 404
        index = static_dir / "index.html"
        if index.exists():
            return send_from_directory(str(static_dir), "index.html")
        return jsonify({"detail": "frontend not built"}), 404

    return app


app = create_app()
