"""
猎场系统 V6.0 — FastAPI Web 面板后端
提供 REST API + 单页 Dashboard
"""

import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


def create_app(config: dict = None) -> FastAPI:
    app = FastAPI(
        title="猎场系统 V6.0",
        description="AI-Native Quantitative Trading System",
        version="6.0.0",
    )

    # 全局状态（由主引擎注入）
    app.state.engine = None
    app.state.config = config or {}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        if DASHBOARD_HTML.exists():
            return DASHBOARD_HTML.read_text(encoding='utf-8')
        return HTMLResponse("<h1>猎场系统 V6.0</h1><p>Dashboard not found</p>")

    @app.get("/api/status")
    async def get_status():
        engine = app.state.engine
        if engine:
            return JSONResponse(engine.get_status())
        return JSONResponse({
            "state": "not_initialized",
            "trading_session": False,
            "time": datetime.now().isoformat(),
        })

    @app.get("/api/portfolio")
    async def get_portfolio():
        """获取组合状态"""
        engine = app.state.engine
        if engine and hasattr(engine, '_subsystems'):
            db = engine._subsystems.get('database')
            if db:
                positions = db.get_all_positions()
                snapshots = []
                with db._get_conn() as conn:
                    rows = conn.execute(
                        "SELECT * FROM portfolio_snapshot ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    if rows:
                        snapshots = [dict(rows)]

                return JSONResponse({
                    'positions': positions,
                    'snapshot': snapshots[0] if snapshots else {},
                })
        return JSONResponse({'positions': [], 'snapshot': {}})

    @app.get("/api/signals")
    async def get_signals(date: Optional[str] = None):
        """获取信号列表"""
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        engine = app.state.engine
        if engine:
            db = engine._subsystems.get('database')
            if db:
                signals = db.get_signals_by_date(date)
                return JSONResponse({'signals': signals, 'date': date})
        return JSONResponse({'signals': [], 'date': date})

    @app.get("/api/trades")
    async def get_trades(days: int = Query(30, ge=1, le=365)):
        """获取交易历史"""
        start = (datetime.now().replace(hour=0, minute=0, second=0)).strftime('%Y-%m-%d')
        engine = app.state.engine
        if engine:
            db = engine._subsystems.get('database')
            if db:
                trades = db.get_trades()
                return JSONResponse({'trades': trades[-50:], 'total': len(trades)})
        return JSONResponse({'trades': [], 'total': 0})

    @app.get("/api/risk")
    async def get_risk_status():
        """获取风控状态"""
        engine = app.state.engine
        if engine:
            cb = engine._subsystems.get('circuit_breaker')
            if cb:
                return JSONResponse(cb.get_status())
        return JSONResponse({'active_count': 0, 'breakers': []})

    @app.get("/api/evolution")
    async def get_evolution():
        """获取策略进化数据"""
        engine = app.state.engine
        if engine:
            db = engine._subsystems.get('database')
            if db:
                with db._get_conn() as conn:
                    rows = conn.execute(
                        "SELECT * FROM strategy_lifecycle ORDER BY id DESC LIMIT 20"
                    ).fetchall()
                    return JSONResponse({'records': [dict(r) for r in rows]})
        return JSONResponse({'records': []})

    @app.get("/api/stats")
    async def get_stats():
        """获取系统统计"""
        engine = app.state.engine
        if engine:
            db = engine._subsystems.get('database')
            if db:
                return JSONResponse(db.get_stats())
        return JSONResponse({})

    @app.post("/api/control/pause")
    async def pause_trading():
        engine = app.state.engine
        if engine:
            await engine.pause()
            return JSONResponse({"status": "paused"})
        return JSONResponse({"status": "error"})

    @app.post("/api/control/resume")
    async def resume_trading():
        engine = app.state.engine
        if engine:
            await engine.resume()
            return JSONResponse({"status": "running"})
        return JSONResponse({"status": "error"})

    return app
