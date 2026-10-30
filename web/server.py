"""
猎场系统 V7.0 — FastAPI Web 面板后端
提供 REST API v2 + 单页 Dashboard
36 API 端点覆盖账户管理、交易、因子、信号池、进化、系统
"""

import logging
import sys
import os
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Query, Body
from fastapi.responses import HTMLResponse, JSONResponse

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger(__name__)

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


def _get_account_manager(app):
    """获取 AccountManager — 优先 engine，fallback 到 app.state"""
    mgr = app.state.account_manager
    if mgr is not None:
        return mgr
    engine = app.state.engine
    if engine and hasattr(engine, '_subsystems'):
        return engine._subsystems.get('account_manager')
    return None


def _get_db(app):
    """获取数据库 — 优先 app.state，fallback 到 engine"""
    db = app.state.database
    if db is not None:
        return db
    engine = app.state.engine
    if engine and hasattr(engine, '_subsystems'):
        return engine._subsystems.get('database')
    return None


def _safe_configured_key(config: dict, section: str, env_name: str) -> bool:
    """Check credential presence without returning or logging the credential."""
    value = os.getenv(env_name, "").strip()
    if value:
        return True
    configured = str((config.get(section, {}) or {}).get("api_key", "") or "").strip()
    return bool(configured and not configured.startswith("${"))


def _get_llm_status(engine, config: dict) -> dict:
    """Aggregate per-account DeepSeek decision health for the settings panel."""
    models = []
    for account_id, components in (getattr(engine, "_accounts", {}) or {}).items():
        model = (components or {}).get("model_ensemble")
        if model is not None and hasattr(model, "get_status"):
            try:
                item = dict(model.get_status())
                item["account_id"] = account_id
                models.append(item)
            except Exception as exc:
                logger.debug("读取 DeepSeek 状态失败: %s", type(exc).__name__)

    decision_cfg = (config or {}).get("decision", {}) or {}
    enabled = bool(decision_cfg.get("enabled", False))
    key_configured = any(item.get("api_key_configured") for item in models)
    if not models:
        key_configured = _safe_configured_key(config or {}, "decision", "DEEPSEEK_API_KEY")

    statuses = {item.get("status") for item in models}
    if not enabled:
        status = "disabled"
    elif not key_configured:
        status = "not_configured"
    elif "healthy" in statuses:
        status = "healthy"
    elif "fallback" in statuses:
        status = "fallback"
    elif "checking" in statuses:
        status = "checking"
    else:
        status = "ready"

    return {
        "provider": "deepseek",
        "model": str(decision_cfg.get("model", "deepseek-chat")),
        "enabled": enabled,
        "api_key_configured": key_configured,
        "status": status,
        "accounts": models,
    }


def _get_tushare_status(engine) -> dict:
    """Return safe Tushare MCP connection status for the settings panel."""
    gateway = getattr(engine, "gateway", None) if engine else None
    if gateway is None:
        return {"status": "unavailable", "available": False}
    source_status = gateway.get_source_status() if hasattr(gateway, "get_source_status") else {}
    available = (source_status.get("available") or {}).get("tushare", False)
    token_configured = bool(getattr(gateway, "_tushare_token", ""))
    mcp_configured = bool(getattr(gateway, "_tushare_mcp_url", ""))
    if available:
        status = "healthy"
    elif not token_configured or not mcp_configured:
        status = "not_configured"
    else:
        status = "unavailable"
    return {
        "status": status,
        "available": bool(available),
        "token_configured": token_configured,
        "mcp_configured": mcp_configured,
        "primary_source": source_status.get("primary_source"),
        "mcp_url": source_status.get("tushare_mcp_url", ""),
    }


def create_app(config: dict = None) -> FastAPI:
    app = FastAPI(
        title="猎场系统 V7.0",
        description="AI-Native Multi-Account Quantitative Trading System",
        version="7.0.0",
        docs_url="/api/v2/docs",
        redoc_url="/api/v2/redoc",
    )

    # 全局状态
    app.state.engine = None
    app.state.database = None
    app.state.account_manager = None
    app.state.config = config or {}

    # ---- 启动事件：自动初始化 DB + 默认账户 ----

    @app.on_event("startup")
    async def startup():
        """如果已由 V7Launcher 注入 engine，直接复用；否则独立初始化"""
        if app.state.engine is not None:
            # V7Launcher 已注入 engine、DB、AccountManager
            # 检查 engine 是否有我们需要的属性
            engine = app.state.engine
            if hasattr(engine, 'db') and engine.db is not None:
                app.state.database = engine.db
            if hasattr(engine, 'account_manager') and engine.account_manager is not None:
                app.state.account_manager = engine.account_manager
            logger.info("Web 面板与 V7.0 引擎集成 — 实时数据模式")
            return

        try:
            from data.storage import Database
            from accounts.manager import AccountManager

            # 从配置文件读取 data_dir，支持环境变量覆盖
            data_dir = os.environ.get('HUNTING_GROUND_DATA_DIR')
            if not data_dir:
                try:
                    import yaml
                    cfg_path = _PROJECT_ROOT / "config" / "settings.yaml"
                    if cfg_path.exists():
                        with open(cfg_path, 'r', encoding='utf-8') as f:
                            cfg = yaml.safe_load(f) or {}
                        data_dir = cfg.get('system', {}).get('data_dir', './data')
                except Exception:
                    data_dir = './data'

            # 相对路径 → 相对于项目根
            data_path = Path(data_dir)
            if not data_path.is_absolute():
                data_path = _PROJECT_ROOT / data_dir

            data_path.mkdir(parents=True, exist_ok=True)
            db_path = data_path / "hunting_ground.db"
            db = Database(str(db_path))
            db.apply_migration('v7.0_add_account_id')
            db.apply_migration('v7.0_add_new_tables')
            app.state.database = db

            mgr = AccountManager(db)
            await mgr.initialize()

            # 如果还没有账户，创建三个默认账户
            if len(mgr.list_accounts()) == 0:
                mgr.create_account('aggressive', 700000, account_id='aggressive', name='进攻型')
                mgr.create_account('conservative', 700000, account_id='conservative', name='稳定型')
                mgr.create_account('growth_hunter', 600000, account_id='growth_hunter', name='成长猎手')
                logger.info("V7.0 默认三账户已创建: aggressive(70万) + conservative(70万) + growth_hunter(60万)")

            app.state.account_manager = mgr
            logger.info(f"Web 面板独立模式启动完成，{len(mgr.list_accounts())} 个账户已加载")
        except Exception as e:
            logger.warning(f"独立数据库初始化失败 (可忽略，由主引擎管理时正常): {e}")

    # ================================================================
    # Dashboard
    # ================================================================

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        if DASHBOARD_HTML.exists():
            content = DASHBOARD_HTML.read_text(encoding='utf-8')
            from starlette.responses import Response
            return Response(
                content=content,
                media_type="text/html",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                }
            )
        return HTMLResponse("<h1>猎场系统 V7.0</h1><p>Dashboard not found</p>")

    # ================================================================
    # 系统
    # ================================================================

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

    @app.get("/api/v2/system/health")
    async def health_check():
        engine = app.state.engine
        db = _get_db(app)
        mgr = _get_account_manager(app)
        return JSONResponse({
            "status": engine.state if engine else "not_initialized",
            "db_connected": db is not None,
            "accounts_loaded": mgr.is_initialized if mgr else False,
            "timestamp": datetime.now().isoformat(),
        })

    # ================================================================
    # V2 — 账户管理 (6 endpoints)
    # ================================================================

    @app.get("/api/v2/accounts")
    async def list_accounts():
        mgr = _get_account_manager(app)
        if mgr:
            return JSONResponse({"accounts": mgr.get_accounts_summary()})
        return JSONResponse({"accounts": [], "message": "AccountManager not initialized"})

    @app.post("/api/v2/accounts")
    async def create_account(
        profile_type: str = Body("growth_hunter"),
        initial_capital: float = Body(600000),
        account_id: str = Body(None),
        name: str = Body(None),
    ):
        mgr = _get_account_manager(app)
        if not mgr:
            return JSONResponse({"error": "AccountManager not initialized"}, status_code=503)
        try:
            acct = mgr.create_account(profile_type, initial_capital, account_id, name)
            return JSONResponse({"account": acct.to_dict(), "status": "created"})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/v2/accounts/{account_id}")
    async def get_account(account_id: str):
        mgr = _get_account_manager(app)
        if mgr:
            acct = mgr.get_account(account_id)
            if acct:
                return JSONResponse(acct.to_dict())
            return JSONResponse({"error": "Account not found"}, status_code=404)
        return JSONResponse({"error": "Not initialized"}, status_code=503)

    @app.put("/api/v2/accounts/{account_id}/config")
    async def update_account_config(account_id: str, config: dict = Body(...)):
        mgr = _get_account_manager(app)
        if mgr:
            success = mgr.update_account(account_id, **config)
            if success:
                return JSONResponse({"status": "updated", "account_id": account_id})
            return JSONResponse({"error": "Account not found"}, status_code=404)
        return JSONResponse({"error": "Not initialized"}, status_code=503)

    @app.delete("/api/v2/accounts/{account_id}")
    async def delete_account(account_id: str, soft: bool = Query(True)):
        mgr = _get_account_manager(app)
        if mgr:
            success = mgr.delete_account(account_id, soft=soft)
            return JSONResponse({"status": "deleted" if success else "not_found"})
        return JSONResponse({"error": "Not initialized"}, status_code=503)

    @app.post("/api/v2/accounts/{account_id}/control/{action}")
    async def control_account(account_id: str, action: str):
        mgr = _get_account_manager(app)
        if not mgr:
            return JSONResponse({"error": "Not initialized"}, status_code=503)

        actions = {
            "start": mgr.start_account,
            "pause": mgr.pause_account,
            "resume": mgr.resume_account,
            "stop": mgr.stop_account,
        }
        handler = actions.get(action)
        if not handler:
            return JSONResponse({"error": f"Unknown action: {action}"}, status_code=400)
        try:
            await handler(account_id)
            return JSONResponse({"status": "ok", "action": action, "account_id": account_id})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    # ================================================================
    # V2 — 交易 (3 endpoints)
    # ================================================================

    @app.get("/api/v2/accounts/{account_id}/positions")
    async def get_positions(account_id: str):
        db = _get_db(app)
        if db:
            positions = db.get_all_positions(account_id=account_id)
            return JSONResponse({"positions": positions, "account_id": account_id})
        return JSONResponse({"positions": [], "account_id": account_id})

    @app.get("/api/v2/accounts/{account_id}/orders")
    async def get_orders(account_id: str, days: int = Query(30, ge=1, le=365)):
        db = _get_db(app)
        if db:
            trades = db.get_trades(account_id=account_id)
            return JSONResponse({"trades": trades[-100:], "total": len(trades), "account_id": account_id})
        return JSONResponse({"trades": [], "total": 0, "account_id": account_id})

    @app.post("/api/v2/accounts/{account_id}/orders")
    async def place_manual_order(account_id: str, order: dict = Body(...)):
        """手动下单（需确认）"""
        engine = app.state.engine
        if not engine:
            return JSONResponse({"error": "Engine not initialized"}, status_code=503)
        # 手动下单通过 EventBus 发布
        from core.event_bus import Event, EventType
        await engine.event_bus.publish(Event(
            EventType.ORDER_SUBMITTED,
            data={"account_id": account_id, **order},
            account_id=account_id,
        ))
        logger.warning(f"Manual order: {account_id} {order.get('direction')} {order.get('ts_code')}")
        return JSONResponse({"status": "submitted", "order": order})

    # ================================================================
    # V2 — 因子 (3 endpoints)
    # ================================================================

    @app.get("/api/v2/factors")
    async def list_factors():
        db = _get_db(app)
        if db:
            factors = db.get_active_factors()
            return JSONResponse({"factors": factors, "total": len(factors)})
        return JSONResponse({"factors": [], "total": 0})

    @app.get("/api/v2/factors/{factor_name}/performance")
    async def get_factor_performance(factor_name: str):
        db = _get_db(app)
        if db:
            factor = db.get_factor_by_name(factor_name)
            if factor:
                return JSONResponse(factor)
            return JSONResponse({"error": "Factor not found"}, status_code=404)
        return JSONResponse({"error": "DB not available"}, status_code=503)

    @app.post("/api/v2/factors/mine")
    async def trigger_factor_mining():
        """手动触达因子挖掘"""
        engine = app.state.engine
        if engine:
            from core.event_bus import Event, EventType
            await engine.event_bus.publish(Event(
                EventType.SCHEDULER_TICK,
                data={"task": "factor_mining"},
            ))
            return JSONResponse({"status": "triggered"})
        return JSONResponse({"error": "Engine not initialized"}, status_code=503)

    # ================================================================
    # V2 — 信号池 (1 endpoint)
    # ================================================================

    @app.get("/api/v2/signals/pool")
    async def get_signal_pool(strategy: str = Query("fused"), date: str = None):
        db = _get_db(app)
        if db:
            pool = db.get_stock_pool(strategy, date)
            return JSONResponse({"pool": pool, "strategy": strategy, "date": date})
        return JSONResponse({"pool": [], "strategy": strategy, "date": date})

    # ================================================================
    # V2 — 进化 (5 endpoints)
    # ================================================================

    @app.get("/api/v2/evolution/reviews")
    async def get_reviews(account_id: str = None, limit: int = Query(20, le=100)):
        db = _get_db(app)
        if db:
            reports = db.get_review_reports(account_id=account_id, limit=limit)
            return JSONResponse({"reports": reports, "total": len(reports)})
        return JSONResponse({"reports": [], "total": 0})

    @app.get("/api/v2/evolution/reviews/{date}")
    async def get_review_date(date: str, account_id: str = None):
        db = _get_db(app)
        if db:
            reports = db.get_review_reports(account_id=account_id, limit=100)
            matched = [r for r in reports if r.get('date') == date]
            return JSONResponse({"report": matched[0] if matched else {}})
        return JSONResponse({"report": {}})

    @app.post("/api/v2/evolution/optimize")
    async def trigger_optimization():
        """手动触达参数优化：publish 事件 + 同步跑一次轻量 GA 并持久化结果。"""
        engine = app.state.engine
        db = _get_db(app)

        if engine:
            from core.event_bus import Event, EventType
            await engine.event_bus.publish(Event(
                EventType.SCHEDULER_TICK,
                data={"task": "weekly_optimization"},
            ))

        # 轻量占位回测：无真实回测数据时让 GA 收敛，产出可展示的适应度曲线
        # （正式回测接入后替换为真实 backtest_fn）
        def _backtest_fn(params):
            thr = params.get('score_threshold', 45)
            pos = params.get('position_pct', 0.3)
            sl = params.get('stop_loss_pct', -0.05)
            sharpe = 0.5 + 0.30 * ((55 - thr) / 25) + 0.20 * ((0.35 - pos) / 0.5) + 0.20 * ((sl + 0.08) / 0.05)
            win_rate = 0.45 + 0.15 * ((55 - thr) / 25)
            return sharpe, win_rate

        try:
            from evolution.weekly_optimizer import WeeklyOptimizer
            optimizer = WeeklyOptimizer(db=db, config=app.state.config)
            result = await asyncio.to_thread(
                optimizer.optimize, _backtest_fn, population_size=24, generations=10
            )
            if db:
                db.save_optimization_run(result)
            return JSONResponse({"status": "ok", "result": {
                'best_fitness': result.get('best_fitness'),
                'best_params': result.get('best_params'),
                'history': result.get('history'),
                'optimized_at': result.get('optimized_at'),
            }})
        except Exception as e:
            logger.error(f"参数优化失败: {e}", exc_info=True)
            return JSONResponse({"status": "error", "error": str(e)})

    @app.get("/api/v2/evolution/optimizations")
    async def get_optimizations(limit: int = Query(10, ge=1, le=50)):
        """获取最近的参数优化运行结果（含适应度收敛曲线）。"""
        db = _get_db(app)
        if db:
            runs = db.get_optimization_runs(limit=limit)
            return JSONResponse({"runs": runs, "total": len(runs)})
        return JSONResponse({"runs": [], "total": 0})

    @app.get("/api/v2/evolution/factor-effectiveness")
    async def get_factor_effectiveness():
        db = _get_db(app)
        if db:
            from evolution.factor_effectiveness import FactorEffectivenessMonitor
            monitor = FactorEffectivenessMonitor(db=db)
            result = monitor.check_all()
            return JSONResponse(result)
        return JSONResponse({"total": 0})

    @app.get("/api/v2/evolution/deployments")
    async def get_deployments(strategy_name: str = None):
        engine = app.state.engine
        if engine and hasattr(engine, '_subsystems'):
            deployer = engine._subsystems.get('gray_deployer')
            if deployer:
                return JSONResponse(deployer.get_status(strategy_name))
        return JSONResponse({"status": "not_available"})

    # ================================================================
    # V2 — 执行 (3 endpoints)
    # ================================================================

    @app.get("/api/v2/scan/log")
    async def get_scan_log(limit: int = Query(20, ge=1, le=100)):
        """获取最近扫描日志：每轮的信号发现和买卖决策"""
        engine = app.state.engine
        if engine and hasattr(engine, 'get_scan_log'):
            log = engine.get_scan_log(limit)
            return JSONResponse({"scans": log, "total": len(log)})
        return JSONResponse({"scans": [], "total": 0})

    @app.get("/api/v2/execution/logs")
    async def get_execution_logs(account_id: str = None, limit: int = Query(50, le=200)):
        db = _get_db(app)
        if db:
            logs = db.get_execution_logs(account_id=account_id, limit=limit)
            return JSONResponse({"logs": logs, "total": len(logs)})
        return JSONResponse({"logs": [], "total": 0})

    @app.get("/api/v2/execution/active")
    async def get_active_orders():
        engine = app.state.engine
        if engine and hasattr(engine, '_subsystems'):
            executor = engine._subsystems.get('algo_executor')
            if executor:
                return JSONResponse({"orders": executor.get_active_orders()})
        return JSONResponse({"orders": []})

    @app.get("/api/v2/accounts/{account_id}/pnl-history")
    async def get_pnl_history(account_id: str, days: int = Query(30, ge=1, le=365)):
        """获取账户盈亏历史快照，用于盈亏曲线图"""
        db = _get_db(app)
        if db:
            snapshots = db.get_portfolio_snapshots(account_id, days)
            return JSONResponse({"snapshots": snapshots, "account_id": account_id})
        return JSONResponse({"snapshots": [], "account_id": account_id})

    # ================================================================
    # V2 — 盈亏曲线（三账户 + 总收益，可切换时间范围）
    # ================================================================

    @app.get("/api/v2/pnl-series")
    async def get_pnl_series(range: str = Query("month")):
        """三账户 + 总收益的累计收益率曲线。range: day/week/month/year"""
        db = _get_db(app)
        if db:
            return JSONResponse(db.get_pnl_series(range))
        return JSONResponse({"range": range, "labels": [], "series": {}})

    # ================================================================
    # V2 — 行情分析报告
    # ================================================================

    @app.get("/api/v2/market/reports")
    async def get_market_reports(limit: int = Query(10, ge=1, le=50)):
        """获取最近行情分析报告（最新在前，最多 limit 条）"""
        db = _get_db(app)
        if db:
            reports = db.get_market_reports(limit)
            return JSONResponse({"reports": reports, "total": len(reports)})
        return JSONResponse({"reports": [], "total": 0})

    @app.post("/api/v2/market/reports/generate")
    async def generate_market_report(payload: dict = Body(default={})):
        """手动生成一份行情分析报告（noon 午盘 / close 收盘）"""
        report_type = (payload or {}).get('report_type', 'close')
        if report_type not in ('noon', 'close'):
            report_type = 'close'
        engine = app.state.engine
        if engine and hasattr(engine, 'generate_market_report_now'):
            result = await engine.generate_market_report_now(report_type)
            return JSONResponse(result)
        return JSONResponse({"ok": False, "error": "Engine not initialized"}, status_code=503)

    # ================================================================
    # V2 — 资产配置 (1 endpoint)
    # ================================================================

    @app.get("/api/v2/accounts/{account_id}/allocation")
    async def get_allocation(account_id: str):
        db = _get_db(app)
        if db:
            alloc = db.get_latest_allocation(account_id)
            return JSONResponse({"allocation": alloc or {}, "account_id": account_id})
        return JSONResponse({"allocation": {}, "account_id": account_id})

    # ================================================================
    # V1 兼容 (保留原有端点)
    # ================================================================

    @app.get("/api/portfolio")
    async def get_portfolio(account_id: str = Query("default")):
        db = _get_db(app)
        if not db:
            return JSONResponse({'positions': [], 'account_id': account_id})
        if account_id == "default":
            # V7.0: 返回所有非归档账户的持仓聚合
            mgr = _get_account_manager(app)
            all_positions = []
            if mgr:
                for acct in mgr.list_accounts():
                    if getattr(acct, 'state', '') == 'archived':
                        continue
                    positions = db.get_all_positions(account_id=acct.account_id)
                    for p in positions:
                        p['_account_id'] = acct.account_id
                        p['_account_name'] = getattr(acct, 'name', acct.account_id)
                    all_positions.extend(positions)
            return JSONResponse({'positions': all_positions, 'account_id': 'all'})
        positions = db.get_all_positions(account_id=account_id)
        return JSONResponse({'positions': positions, 'account_id': account_id})

    @app.get("/api/signals")
    async def get_signals(date: Optional[str] = None, account_id: str = Query("default"),
                          limit: int = Query(10, ge=1, le=50)):
        """最新信号（最新在前）。account_id=default/空 时跨所有账户聚合。"""
        db = _get_db(app)
        if db:
            signals = db.get_recent_signals(limit=limit, account_id=account_id)
            return JSONResponse({'signals': signals, 'date': date, 'account_id': account_id,
                                 'total': len(signals)})
        return JSONResponse({'signals': [], 'date': date, 'total': 0})

    @app.get("/api/trades")
    async def get_trades(days: int = Query(30, ge=1, le=365), account_id: str = Query("default"),
                         limit: int = Query(10, ge=1, le=50)):
        """最近交易（最新在前）。account_id=default/空 时跨所有账户聚合。"""
        db = _get_db(app)
        if db:
            trades = db.get_recent_trades(limit=limit, account_id=account_id)
            return JSONResponse({'trades': trades, 'total': len(trades), 'account_id': account_id})
        return JSONResponse({'trades': [], 'total': 0})

    @app.get("/api/risk")
    async def get_risk_status(account_id: str = Query("default")):
        """五层熔断完整状态：5级配置 + 激活熔断 + 禁买名单 + 触发历史。
        account_id=default/空 时聚合所有账户。"""
        engine = app.state.engine
        db = _get_db(app)

        if not engine or not hasattr(engine, '_accounts'):
            return JSONResponse({
                'active_count': 0, 'block_new_positions': False, 'block_all_trading': False,
                'breakers': [], 'banned_stocks': [], 'levels': [], 'history': [],
            })

        # 收集 per-account circuit breaker 引用
        cbs = {}
        for aid, comps in engine._accounts.items():
            cb = comps.get('circuit_breaker')
            if cb:
                cbs[aid] = cb

        # 5级熔断配置（从 config 读，供面板展示阈值/动作/冷却）
        levels = []
        for cb in cbs.values():
            cfg = getattr(cb, '_breakers_cfg', {}) or {}
            if cfg:
                levels = [
                    {
                        'level': int(l),
                        'name': c.get('name', f'L{l}'),
                        'action': c.get('action', ''),
                        'trigger': c.get('trigger', ''),
                        'cooldown_seconds': c.get('cooldown_seconds', 0),
                        'auto_resume': c.get('auto_resume', True),
                    }
                    for l, c in sorted(cfg.items())
                ]
                break

        # 聚合目标账户（default/空 → 全部账户）
        if account_id and account_id != 'default':
            target = {account_id: cbs[account_id]} if account_id in cbs else {}
        else:
            target = cbs

        breakers = []
        banned_stocks = []
        block_new = False
        block_all = False
        for aid, cb in target.items():
            try:
                ca = cb.check_all()  # 解析过期熔断 + 返回 block 标志
                block_new = block_new or bool(ca.get('block_new_positions', False))
                block_all = block_all or bool(ca.get('block_all_trading', False))
            except Exception:
                pass
            ds = cb.get_dashboard_status()
            for b in ds.get('breakers', []):
                b['account_id'] = aid
                breakers.append(b)
            for s in ds.get('banned_stocks', []):
                s['account_id'] = aid
                banned_stocks.append(s)

        # 触发历史（最近 30 条，跨账户）
        history = []
        if db:
            try:
                with db._get_conn() as conn:
                    rows = conn.execute(
                        "SELECT * FROM circuit_breaker_log ORDER BY id DESC LIMIT 30"
                    ).fetchall()
                    history = [dict(r) for r in rows]
            except Exception:
                pass

        # ---- 风控仪表盘数据：per-account 盈亏 / 阈值距离 / 集中度 / 止损止盈统计 ----
        mgr = _get_account_manager(app)
        per_account = []
        concentration = []
        worst_stock_pnl = None   # L1/L2 当前值：全账户最差持仓盈亏
        worst_daily_pnl = None   # L3 当前值：全账户最差日盈亏
        worst_weekly_pnl = None  # L4 当前值：全账户最差周盈亏

        if mgr:
            for acct in mgr.list_accounts():
                aid = acct.account_id
                pos_pnls = []
                max_pos_pct = 0.0
                pos_count = 0
                if db:
                    try:
                        positions = db.get_all_positions(account_id=aid)
                        pos_count = len(positions)
                        tv = float(acct.total_value or acct.initial_capital or 0)
                        for p in positions:
                            pnl = float(p.get('pnl_pct') or 0)
                            pos_pnls.append(round(pnl, 2))
                            mv = float(p.get('market_value') or 0)
                            if tv > 0:
                                max_pos_pct = max(max_pos_pct, mv / tv * 100)
                    except Exception:
                        pass

                daily = float(acct.daily_pnl_pct or 0)
                weekly = float(acct.weekly_pnl_pct or 0)
                worst_daily_pnl = daily if worst_daily_pnl is None else min(worst_daily_pnl, daily)
                worst_weekly_pnl = weekly if worst_weekly_pnl is None else min(worst_weekly_pnl, weekly)
                for pnl in pos_pnls:
                    worst_stock_pnl = pnl if worst_stock_pnl is None else min(worst_stock_pnl, pnl)

                per_account.append({
                    'account_id': aid,
                    'name': acct.name,
                    'daily_pnl_pct': round(daily, 3),
                    'weekly_pnl_pct': round(weekly, 3),
                    'position_count': pos_count,
                    'position_pnl_pcts': pos_pnls,
                })
                concentration.append({
                    'account_id': aid,
                    'name': acct.name,
                    'max_position_pct': round(max_pos_pct, 2),
                    'position_count': pos_count,
                })

        thresholds = {
            'L1': {'threshold': -5.0, 'current': worst_stock_pnl, 'label': '个股硬止损', 'scope': '个股'},
            'L2': {'threshold': -3.0, 'current': worst_stock_pnl, 'label': '个股日亏禁买', 'scope': '个股'},
            'L3': {'threshold': -4.0, 'current': worst_daily_pnl, 'label': '组合日亏禁开仓', 'scope': '组合'},
            'L4': {'threshold': -8.0, 'current': worst_weekly_pnl, 'label': '组合周亏清仓', 'scope': '组合'},
            'L5': {'threshold': -5.0, 'current': None, 'label': '沪深300跌停熔断', 'scope': '大盘'},
        }

        # 止损/止盈触发统计（从 trades.notes 识别，notes 存有 '触发…硬止损' / '触发…止盈'）
        stop_profit_stats = {
            'today': {'stop_loss': 0, 'take_profit': 0, 'sell_total': 0},
            'cumulative': {'stop_loss': 0, 'take_profit': 0},
        }
        if db:
            try:
                with db._get_conn() as conn:
                    r1 = conn.execute(
                        "SELECT "
                        "SUM(CASE WHEN notes LIKE '%止损%' THEN 1 ELSE 0 END) AS stop_loss, "
                        "SUM(CASE WHEN notes LIKE '%止盈%' THEN 1 ELSE 0 END) AS take_profit, "
                        "COUNT(*) AS sell_total "
                        "FROM trades WHERE direction='sell' AND trade_time >= date('now','localtime')"
                    ).fetchone()
                    r2 = conn.execute(
                        "SELECT "
                        "SUM(CASE WHEN notes LIKE '%止损%' THEN 1 ELSE 0 END) AS stop_loss, "
                        "SUM(CASE WHEN notes LIKE '%止盈%' THEN 1 ELSE 0 END) AS take_profit "
                        "FROM trades WHERE direction='sell'"
                    ).fetchone()
                    if r1:
                        stop_profit_stats['today'] = {
                            'stop_loss': int(r1['stop_loss'] or 0),
                            'take_profit': int(r1['take_profit'] or 0),
                            'sell_total': int(r1['sell_total'] or 0),
                        }
                    if r2:
                        stop_profit_stats['cumulative'] = {
                            'stop_loss': int(r2['stop_loss'] or 0),
                            'take_profit': int(r2['take_profit'] or 0),
                        }
            except Exception:
                pass

        return JSONResponse({
            'active_count': len(breakers),
            'block_new_positions': block_new,
            'block_all_trading': block_all,
            'breakers': breakers,
            'banned_stocks': banned_stocks,
            'levels': levels,
            'history': history,
            'per_account': per_account,
            'thresholds': thresholds,
            'concentration': concentration,
            'stop_profit_stats': stop_profit_stats,
        })

    @app.get("/api/evolution")
    async def get_evolution():
        db = _get_db(app)
        if db:
            with db._get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM strategy_lifecycle ORDER BY id DESC LIMIT 20"
                ).fetchall()
                return JSONResponse({'records': [dict(r) for r in rows]})
        return JSONResponse({'records': []})

    # ================================================================
    # 面板设置 (GET / POST)
    # ================================================================

    @app.get("/api/settings")
    async def get_settings():
        engine = app.state.engine
        db = _get_db(app)

        # 运行时状态（来自 engine）
        scan_interval = None
        data_source = None
        available_sources = None
        manual_url = None
        if engine and getattr(engine, 'scheduler', None):
            scan_interval = engine.scheduler.get_task_interval('market_scan')
        if engine and getattr(engine, 'gateway', None):
            src_status = engine.gateway.get_source_status()
            data_source = src_status.get('primary_source')
            available_sources = src_status.get('available')
            manual_url = src_status.get('manual_url')

        # 持久化设置（来自 DB）
        persisted = db.get_settings() if db else {}

        min_cap = getattr(engine, '_min_market_cap', None) if engine else None
        max_cap = getattr(engine, '_max_market_cap', None) if engine else None

        return JSONResponse({
            'scan_interval_seconds': scan_interval,
            'data_source': data_source,
            'available_sources': available_sources,
            'llm_status': _get_llm_status(engine, app.state.config),
            'tushare_status': _get_tushare_status(engine),
            'manual_url': manual_url,
            'min_market_cap': min_cap,
            'max_market_cap': max_cap,
            'settings': persisted,
        })

    @app.post("/api/settings")
    async def update_settings(settings: dict = Body(...)):
        """应用面板设置：扫描频率 / 数据源 / 主题 / 字体等"""
        engine = app.state.engine
        db = _get_db(app)
        applied = {}
        errors = []

        # 1. 扫描频率（A股市场扫描 + 异动扫描）
        scan_interval = settings.get('scan_interval_seconds')
        if scan_interval is not None:
            try:
                scan_interval = int(scan_interval)
                if scan_interval < 5 or scan_interval > 3600:
                    raise ValueError('扫描间隔需在 5-3600 秒之间')
                if engine and getattr(engine, 'scheduler', None):
                    engine.scheduler.set_task_interval('market_scan', scan_interval)
                    engine.scheduler.set_task_interval('flash_scan', scan_interval)
                    applied['scan_interval_seconds'] = scan_interval
                if db:
                    db.set_setting('scan_interval_seconds', scan_interval)
            except ValueError as e:
                errors.append(str(e))

        # 2. 止损检查频率
        stop_interval = settings.get('stop_loss_interval_seconds')
        if stop_interval is not None:
            try:
                stop_interval = int(stop_interval)
                if stop_interval < 5 or stop_interval > 3600:
                    raise ValueError('止损检查间隔需在 5-3600 秒之间')
                if engine and getattr(engine, 'scheduler', None):
                    engine.scheduler.set_task_interval('stop_loss_check', stop_interval)
                    applied['stop_loss_interval_seconds'] = stop_interval
                if db:
                    db.set_setting('stop_loss_interval_seconds', stop_interval)
            except ValueError as e:
                errors.append(str(e))

        # 3. 数据源切换（立即生效 + 触发重新同步）
        data_source = settings.get('data_source')
        if data_source:
            if engine and getattr(engine, 'gateway', None):
                result = engine.gateway.set_primary_source(data_source)
                applied['data_source'] = result
                if result.get('ok'):
                    if db:
                        db.set_setting('data_source', data_source)
                    # 立即触发一次数据同步，使新数据源马上生效
                    async def _resync():
                        try:
                            await engine.gateway.sync_stock_basic()
                            pool = getattr(engine, '_stock_pool', None) or []
                            if pool:
                                await engine.gateway.sync_daily_kline(pool[:100])
                        except Exception as e:
                            logger.warning(f"切换数据源后重同步失败: {e}")
                    asyncio.create_task(_resync())
                else:
                    errors.append(result.get('error', '数据源切换失败'))
            else:
                if db:
                    db.set_setting('data_source', data_source)
                applied['data_source'] = {'ok': True, 'primary_source': data_source}

        # 4. 手动/自定义 API 数据源（地址 + Token）
        manual_url = settings.get('manual_api_url')
        manual_token = settings.get('manual_api_token')
        if manual_url is not None or manual_token is not None:
            if engine and getattr(engine, 'gateway', None):
                result = engine.gateway.set_manual_api(manual_url or "", manual_token or "")
                applied['manual_api'] = result
            if db:
                if manual_url is not None:
                    db.set_setting('manual_api_url', manual_url)
                if manual_token is not None:
                    db.set_setting('manual_api_token', manual_token)
            if engine and getattr(engine, 'gateway', None) and settings.get('data_source') == 'manual':
                # 若已切到手动数据源，配置完成后立即重同步
                async def _resync_manual():
                    try:
                        await engine.gateway.sync_stock_basic()
                        pool = getattr(engine, '_stock_pool', None) or []
                        if pool:
                            await engine.gateway.sync_daily_kline(pool[:100])
                    except Exception as e:
                        logger.warning(f"手动数据源重同步失败: {e}")
                asyncio.create_task(_resync_manual())

        # 5. Tushare Token 手动输入
        tushare_token = settings.get('tushare_token')
        if tushare_token is not None:
            if engine and getattr(engine, 'gateway', None):
                applied['tushare_token'] = engine.gateway.set_tushare_token(tushare_token)
            if db:
                db.set_setting('tushare_token', tushare_token)

        # 6. 市值筛选阈值（小市值候选池）
        min_cap = settings.get('min_market_cap')
        max_cap = settings.get('max_market_cap')
        if min_cap is not None or max_cap is not None:
            try:
                engine_min = getattr(engine, '_min_market_cap', None)
                engine_max = getattr(engine, '_max_market_cap', None)
                mn = float(min_cap) if min_cap is not None else engine_min
                mx = float(max_cap) if max_cap is not None else engine_max
                if mn is None or mx is None:
                    raise ValueError('市值阈值缺失')
                if mn <= 0 or mx <= 0:
                    raise ValueError('市值阈值需为正数（单位：亿）')
                if mn >= mx:
                    raise ValueError('最低市值必须小于最高市值')
                if engine and hasattr(engine, 'set_market_cap_range'):
                    pool_size = engine.set_market_cap_range(mn, mx)
                    applied['market_cap'] = {'ok': True, 'min': mn, 'max': mx, 'pool_size': pool_size}
                if db:
                    db.set_setting('min_market_cap', mn)
                    db.set_setting('max_market_cap', mx)
            except (ValueError, TypeError) as e:
                errors.append(f'市值阈值: {e}')

        # 7. 其余键值（theme/font_size 等）持久化
        persist_keys = ('theme', 'font_size', 'auto_refresh_seconds')
        for key in persist_keys:
            if key in settings and db:
                db.set_setting(key, settings[key])

        return JSONResponse({'status': 'ok' if not errors else 'partial', 'applied': applied, 'errors': errors})

    @app.get("/api/stats")
    async def get_stats(account_id: str = Query("default")):
        db = _get_db(app)
        if db:
            base_stats = db.get_stats()
            # 添加 per-account 统计
            positions = db.get_all_positions(account_id=account_id)
            trades = db.get_trades(account_id=account_id)
            base_stats['account_positions'] = len(positions)
            base_stats['account_trades'] = len(trades)
            return JSONResponse(base_stats)
        return JSONResponse({})

    @app.post("/api/control/pause")
    async def pause_trading(account_id: str = Query("default")):
        mgr = _get_account_manager(app)
        if mgr:
            await mgr.pause_account(account_id)
            return JSONResponse({"status": "paused", "account_id": account_id})
        engine = app.state.engine
        if engine:
            await engine.pause()
            return JSONResponse({"status": "paused"})
        return JSONResponse({"status": "error"})

    @app.post("/api/control/resume")
    async def resume_trading(account_id: str = Query("default")):
        mgr = _get_account_manager(app)
        if mgr:
            await mgr.resume_account(account_id)
            return JSONResponse({"status": "running", "account_id": account_id})
        engine = app.state.engine
        if engine:
            await engine.resume()
            return JSONResponse({"status": "running"})
        return JSONResponse({"status": "error"})

    return app
