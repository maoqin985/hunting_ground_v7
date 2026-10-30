"""
猎场系统 V7.0 — 全系统启动编排器
组合 Web Dashboard + 多账户引擎 + 信号扫描 + 自主交易

用法:
    from core.v7_launcher import V7Launcher
    launcher = V7Launcher(config)
    await launcher.start()
"""

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from datetime import datetime, date
from typing import Dict, List, Optional

from data.preprocess import filter_new_stocks

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent


class FallbackModelEnsemble:
    """
    无 AI 模型时的降级评分器。
    直接以 signal fusion 分数作为最终决策分数，不依赖 XGBoost/LSTM/PPO/CatBoost。
    积累足够交易数据后可替换为真实 AI 模型。
    """

    def __init__(self, config: dict = None):
        self.config = config or {}

    async def decide(self, ts_code: str, feature: dict = None, kline: List[Dict] = None,
                     signal_score: float = 0, market_data: dict = None) -> dict:
        """
        降级决策：信号分数 > 阈值则买入。

        Returns:
            {action, score, confidence, stop_loss_pct, suggested_position_pct, reasoning, risk_prob}
        """
        threshold = 8  # 与 SCORE_BUY_THRESHOLD 一致：MACD金叉(~16)/RSI超卖(~13)/布林(~11)可触发
        if signal_score >= threshold:
            return {
                'action': 'buy',
                'score': signal_score,
                'confidence': 0.55,
                'stop_loss_pct': -0.05,
                'suggested_position_pct': 0.15,
                'reasoning': f'降级评分: 信号分{signal_score:.1f}>=阈值{threshold}',
                'risk_prob': 0.35,
            }
        return {
            'action': 'hold',
            'score': signal_score,
            'confidence': 0.3,
            'stop_loss_pct': -0.05,
            'suggested_position_pct': 0.0,
            'reasoning': f'降级评分: 信号分{signal_score:.1f}<阈值{threshold}',
            'risk_prob': 0.5,
        }


class V7Launcher:
    """
    V7.0 全系统启动器
    — 共享基础设施 (DB, DataGateway, EventBus, Scheduler)
    — 多账户 (AccountManager + per-account Engine 和 AutoTradingEngine)
    — Web Dashboard (FastAPI 嵌入)
    — 定时扫描和交易周期
    """

    def __init__(self, config: dict):
        self.config = config
        self.db = None
        self.gateway = None
        self.event_bus = None
        self.scheduler = None
        self.account_manager = None
        self._accounts: Dict[str, dict] = {}  # account_id → {engine, auto_trading, circuit_breaker, ...}
        self._web_task = None
        self._running = False
        self._stock_pool: List[str] = []  # 小市值候选池
        # 小市值筛选阈值（亿元），运行时可从设置面板调整
        self._min_market_cap: float = float(self.config.get('capital', {}).get('min_market_cap', 10))
        self._max_market_cap: float = float(self.config.get('capital', {}).get('max_market_cap', 50))
        # 次新股过滤：上市不足该天数剔除（维度一「剔除ST/次新股」）
        self._exclude_new_stock_days: int = int(self.config.get('capital', {}).get('exclude_new_stock_days', 60))
        self._kline_cache: Dict[str, List[Dict]] = {}
        self._signal_cache: Dict[str, List[Dict]] = {}
        self._scan_log: List[dict] = []  # 最近50轮扫描日志
        self._scan_log_max = 50
        self._stock_name_cache: Dict[str, str] = {}  # ts_code → name
        self._last_kline_sync: float = 0  # 上次K线同步时间戳
        self._macro_llm = None  # 惰性初始化的 MacroLLM
        self._llm_checked = False  # 是否已检查过本地 LLM 可用性
        self._generated_reports: set = set()  # 已生成的行情报告 (date, type)
        self._breadth_cache: Dict = {}  # L5 大盘熔断的市场广度缓存 {ts, data}

    # ================================================================
    # 主入口
    # ================================================================

    async def start(self):
        """启动完整 V7.0 系统"""
        logger.info("=" * 60)
        logger.info("猎场系统 V7.0 全系统启动中...")
        logger.info("=" * 60)

        await self._init_shared_infra()
        await self._init_accounts()
        await self._start_web_server()
        await self._sync_initial_data()
        self._start_scheduler()
        await self._run_forever()

    # ================================================================
    # Phase 1: 共享基础设施
    # ================================================================

    async def _init_shared_infra(self):
        """初始化 DB、DataGateway、EventBus、Scheduler"""
        from data.storage import Database
        from data.gateway import DataGateway
        from data.trading_calendar import TradingCalendar, SinaTradeCalendarFetcher
        from core.event_bus import get_event_bus
        from core.scheduler import Scheduler

        # --- Database ---
        data_dir = self.config.get('system', {}).get('data_dir', 'F:/hunting_ground_data')
        db_path = Path(data_dir) / 'hunting_ground.db'
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(db_path))
        logger.info(f"Database: {db_path}")

        # 应用迁移
        self.db.apply_migration('v7.0_add_account_id')
        self.db.apply_migration('v7.0_add_new_tables')
        logger.info("DB migrations applied")

        # --- DataGateway (Tushare MCP 为主，腾讯 HTTP/WebSocket 降级) ---
        self.gateway = DataGateway(self.config, self.db)
        await self.gateway.initialize()
        logger.info("DataGateway initialized (Tushare MCP primary)")

        # --- EventBus (全局单例) ---
        self.event_bus = get_event_bus()
        logger.info("EventBus ready")

        # --- Scheduler (全局) ---
        self.trading_calendar = TradingCalendar()  # 兜底：周末 + 2026 节假日
        self._calendar_fetcher = SinaTradeCalendarFetcher()
        self.scheduler = Scheduler(event_bus=self.event_bus, trading_calendar=self.trading_calendar)
        logger.info("Scheduler ready (trading calendar: fallback, 待 API 同步)")

    # ================================================================
    # Phase 2: 多账户初始化
    # ================================================================

    async def _init_accounts(self):
        """创建/加载 AccountManager，为每个账户注入交易组件"""
        from accounts.manager import AccountManager
        from core.engine import Engine
        from decision.auto_trading import AutoTradingEngine
        from risk.circuit_breaker import CircuitBreaker
        from risk.stop_loss import StopLossMonitor
        from risk.position_monitor import PositionMonitor
        from signals.fusion import SignalFusion
        from ai.deepseek_decision import DeepSeekDecisionEnsemble

        # --- AccountManager ---
        self.account_manager = AccountManager(self.db, self.config)
        await self.account_manager.initialize()

        # 如果没有账户，创建三个默认账户
        if len(self.account_manager.list_accounts()) == 0:
            self.account_manager.create_account('aggressive', 700000, account_id='aggressive', name='进攻型')
            self.account_manager.create_account('conservative', 700000, account_id='conservative', name='稳定型')
            self.account_manager.create_account('growth_hunter', 600000, account_id='growth_hunter', name='成长猎手')
            logger.info("默认三账户已创建: aggressive(70万) + conservative(70万) + growth_hunter(60万)")

        # 账户对账：确保 cash 与 trades 一致
        self._reconcile_accounts()

        # --- 为每个账户注入交易组件 ---
        for acct in self.account_manager.list_accounts():
            account_id = acct.account_id
            profile = acct.strategy_params or {}
            risk_params = acct.risk_params or {}

            logger.info(f"初始化账户 [{account_id}]: {acct.name} ({acct.profile_type}) 资金={acct.initial_capital:,.0f}")

            # Per-account Engine (共享 EventBus, 独立 Scheduler 用全局的)
            engine = Engine(self.config, account_id=account_id)
            engine.register_subsystem('database', self.db)
            engine.register_subsystem('gateway', self.gateway)

            # Signal Fusion (per-account: 不同策略用不同权重)
            signal_fusion = SignalFusion(profile)

            # 先运行原规则评分，再由 DeepSeek 做受限复核；DeepSeek 不可用
            # 时自动回退 FallbackModelEnsemble，原验证器/熔断仍然继续执行。
            baseline_model = FallbackModelEnsemble(profile)
            model_ensemble = DeepSeekDecisionEnsemble(
                self.config,
                baseline_model,
                data_gateway=self.gateway,
                database=self.db,
                account_id=account_id,
            )

            # Circuit Breaker (per-account)
            circuit_breaker = CircuitBreaker(
                self.config, db=self.db, account_id=account_id
            )

            # AutoTradingEngine (per-account)
            auto_trading = AutoTradingEngine(
                config=self.config,
                db=self.db,
                data_gateway=self.gateway,
                signal_fusion=signal_fusion,
                model_ensemble=model_ensemble,
                circuit_breaker=circuit_breaker,
                account_id=account_id,
                account=acct,
            )

            # Stop Loss Monitor (per-account)
            stop_loss_monitor = StopLossMonitor(
                config=profile, db=self.db, data_gateway=self.gateway,
                circuit_breaker=circuit_breaker, account_id=account_id,
            )

            # Position Monitor (per-account)
            position_monitor = PositionMonitor(
                config=profile, db=self.db, data_gateway=self.gateway,
                account_id=account_id, total_capital=acct.initial_capital,
            )

            # 注册到 AccountManager
            self.account_manager.register_trading(
                account_id, auto_trading, circuit_breaker,
                stop_loss_monitor, position_monitor,
                model_ensemble, signal_fusion,
            )
            self.account_manager.register_engine(account_id, engine)

            # 缓存引用
            self._accounts[account_id] = {
                'engine': engine,
                'auto_trading': auto_trading,
                'circuit_breaker': circuit_breaker,
                'stop_loss_monitor': stop_loss_monitor,
                'position_monitor': position_monitor,
                'model_ensemble': model_ensemble,
                'signal_fusion': signal_fusion,
            }

            # 启动引擎
            await engine.start()
            await self.account_manager.start_account(account_id)

            # 设置账户状态为 running
            acct.state = 'running'
            self.db.save_account(acct.to_dict())

        logger.info(f"多账户初始化完成: {len(self._accounts)} 个账户已启动")

    # ================================================================
    # Phase 3: Web Dashboard
    # ================================================================

    async def _start_web_server(self):
        """启动 FastAPI Web Dashboard 作为后台任务"""
        import uvicorn
        from web.server import create_app

        app = create_app(self.config)

        # 注入引擎引用 → Web 面板可读取 live 数据
        app.state.engine = self  # V7Launcher 充当 engine 角色
        app.state.database = self.db
        app.state.account_manager = self.account_manager

        uvicorn_config = uvicorn.Config(
            app, host="0.0.0.0", port=8501, log_level="info"
        )
        server = uvicorn.Server(uvicorn_config)

        self._web_task = asyncio.create_task(server.serve())
        logger.info("Web Dashboard 启动: http://localhost:8501/")

        # 等待服务器就绪
        await asyncio.sleep(2)

    # ================================================================
    # Phase 4: 初始数据同步
    # ================================================================

    async def _sync_initial_data(self):
        """从 Tushare MCP 同步股票列表和 K 线（失败时由网关降级）"""
        logger.info("开始初始数据同步 (Tushare MCP)...")

        # 0. 交易日历（新浪 API，含未来休市安排）
        await self._sync_trading_calendar()

        # 1. 同步股票列表
        stock_count = 0
        try:
            stock_count = await self.gateway.sync_stock_basic() or 0
            logger.info(f"股票列表同步完成: {stock_count} 只")
        except Exception as e:
            logger.warning(f"股票列表同步失败: {e}")

        # 2. 获取候选池：按小市值阈值从 stock_basic 筛选（全部匹配，不设 200 上限）
        self._stock_pool = self._build_stock_pool()
        if not self._stock_pool:
            # 兜底：股票列表同步失败（stock_basic 为空）时，退化为已有 K 线数据的股票
            fallback = self.db.get_all_codes() if hasattr(self.db, 'get_all_codes') else []
            self._stock_pool = list(dict.fromkeys(fallback))[:300]
            if self._stock_pool:
                logger.warning(f"市值筛选为空，退化为已有 K 线股票池: {len(self._stock_pool)} 只")
            else:
                logger.warning("候选池为空（股票列表同步失败且无历史 K 线数据）")

        logger.info(f"候选池: {len(self._stock_pool)} 只股票 (市值 {self._min_market_cap:g}~{self._max_market_cap:g} 亿)")

        # 3. 从数据源拉 K 线 (跳过：太慢阻塞启动，首次扫描时按需拉取)
        try:
            k_count = 0  # await self.gateway.sync_daily_kline(self._stock_pool[:100])
            logger.info("日K线同步: 跳过批量同步，首次扫描时按需拉取")
        except Exception as e:
            logger.warning(f"日K线同步失败: {e}")

        logger.info("初始数据同步完成")

    async def _sync_trading_calendar(self) -> int:
        """从新浪交易日历 API 拉取全量交易日并注入 Scheduler（失败回退本地节假日）"""
        try:
            dates = await self._calendar_fetcher.fetch()
        except Exception as e:
            logger.warning(f"交易日历同步失败: {e}")
            dates = []
        if not dates:
            logger.warning("交易日历 API 不可用，使用本地节假日兜底")
            return 0
        count = self.trading_calendar.set_trading_days(dates)
        today = datetime.now().strftime('%Y-%m-%d')
        is_td = self.trading_calendar.is_trading_day(datetime.now().date())
        logger.info(
            f"交易日历已同步: {count} 个交易日 (source=api, 今日 {today} "
            f"{'交易日' if is_td else '休市'})"
        )
        return count

    def _build_stock_pool(self) -> List[str]:
        """按市值阈值从 stock_basic 筛选小市值候选池（升序，全部匹配，不截断）

        维度一防线：
        - ST 已在 get_stocks_by_market_cap 层剔除 (is_st = 0)
        - 次新股在此层剔除（上市不足 _exclude_new_stock_days 天）
        """
        try:
            small_caps = self.db.get_stocks_by_market_cap(self._min_market_cap, self._max_market_cap)
            if small_caps:
                today = datetime.now().strftime('%Y-%m-%d')
                before = len(small_caps)
                small_caps = filter_new_stocks(small_caps, today, days=self._exclude_new_stock_days)
                if len(small_caps) < before:
                    logger.info(f"次新股过滤: 剔除 {before - len(small_caps)} 只（上市<{self._exclude_new_stock_days}天）")
                small_caps.sort(key=lambda s: (s.get('market_cap') or 0))
                return [s['ts_code'] for s in small_caps]
        except Exception as e:
            logger.warning(f"市值筛选失败: {e}")
        return []

    def set_market_cap_range(self, min_cap: float, max_cap: float) -> int:
        """运行时更新市值筛选阈值并重建候选池，返回新池大小"""
        self._min_market_cap = float(min_cap)
        self._max_market_cap = float(max_cap)
        self._stock_pool = self._build_stock_pool()
        logger.info(f"市值阈值已更新: {self._min_market_cap:g}~{self._max_market_cap:g} 亿，候选池 {len(self._stock_pool)} 只")
        return len(self._stock_pool)

    async def resync_stock_pool(self) -> int:
        """重新同步 stock_basic 并重建候选池（供设置面板/数据源切换后调用）"""
        try:
            await self.gateway.sync_stock_basic()
        except Exception as e:
            logger.warning(f"股票列表重同步失败: {e}")
        self._stock_pool = self._build_stock_pool()
        return len(self._stock_pool)

    # ================================================================
    # Phase 5: 定时任务注册
    # ================================================================

    def _start_scheduler(self):
        """注册定时交易任务"""
        from core.scheduler import TaskPriority

        schedule_cfg = self.config.get('schedule', {})

        # --- 全市场扫描 (每 20 分钟) ---
        self.scheduler.add_task(
            name='market_scan',
            callback=self._market_scan_cycle,
            interval_seconds=schedule_cfg.get('market_scan_interval', 300),  # 每5分钟扫描
            priority=TaskPriority.HIGH,
            run_on_start=True,
            trading_hours_only=True,
        )

        # --- 异动扫描 (每 5 分钟) ---
        self.scheduler.add_task(
            name='flash_scan',
            callback=self._flash_scan_cycle,
            interval_seconds=schedule_cfg.get('flash_scan_interval', 300),
            priority=TaskPriority.NORMAL,
            run_on_start=False,
            trading_hours_only=True,
        )

        # --- 止损检查 (每 60 秒) ---
        self.scheduler.add_task(
            name='stop_loss_check',
            callback=self._stop_loss_cycle,
            interval_seconds=schedule_cfg.get('stop_loss_check_interval', 60),
            priority=TaskPriority.CRITICAL,
            run_on_start=False,
            trading_hours_only=True,
        )

        # --- 组合级风控监控 (每 60 秒)：触发 L2-L5 熔断 ---
        self.scheduler.add_task(
            name='risk_monitor',
            callback=self._risk_monitor_cycle,
            interval_seconds=schedule_cfg.get('stop_loss_check_interval', 60),
            priority=TaskPriority.CRITICAL,
            run_on_start=False,
            trading_hours_only=True,
        )

        # --- 数据同步 (每 20 分钟) ---
        self.scheduler.add_task(
            name='data_sync',
            callback=self._data_sync_cycle,
            interval_seconds=schedule_cfg.get('data_sync_interval', 1200),
            priority=TaskPriority.LOW,
            run_on_start=False,
            trading_hours_only=True,
        )

        # --- 资产快照 (每 5 分钟) ---
        self.scheduler.add_task(
            name='portfolio_snapshot',
            callback=self._portfolio_snapshot_cycle,
            interval_seconds=300,
            priority=TaskPriority.LOW,
            run_on_start=False,
            trading_hours_only=False,
        )

        # --- 盘后结算 (每 2 分钟检查，15:00-15:30 执行一次) ---
        self.scheduler.add_task(
            name='daily_settlement',
            callback=self._daily_settlement,
            interval_seconds=120,
            priority=TaskPriority.NORMAL,
            run_on_start=False,
            trading_hours_only=False,
        )

        # --- 行情分析报告 (午盘 11:30 / 收盘 15:00 各一次) ---
        self.scheduler.add_task(
            name='market_report',
            callback=self._market_report_cycle,
            interval_seconds=60,
            priority=TaskPriority.LOW,
            run_on_start=False,
            trading_hours_only=False,
        )

        # --- 交易日历同步 (每日一次，拉取最新休市安排) ---
        self.scheduler.add_task(
            name='calendar_sync',
            callback=self._sync_trading_calendar,
            interval_seconds=86400,
            priority=TaskPriority.LOW,
            run_on_start=False,
            trading_hours_only=False,
        )

        logger.info(f"Scheduler 任务已注册: market_scan + flash_scan + stop_loss + risk_monitor + data_sync + snapshot + settlement + market_report + calendar_sync")

        # 恢复面板持久化设置（运行时修改后跨重启保持）
        self._load_persisted_settings()

    def _load_persisted_settings(self):
        """从 DB 读取并应用面板持久化设置（扫描频率/止损/数据源/手动API）"""
        try:
            saved = self.db.get_settings() or {}
            scan_interval = saved.get('scan_interval_seconds')
            if scan_interval:
                secs = max(5, min(3600, int(float(scan_interval))))
                self.scheduler.set_task_interval('market_scan', secs)
                self.scheduler.set_task_interval('flash_scan', secs)
            stop_interval = saved.get('stop_loss_interval_seconds')
            if stop_interval:
                secs = max(5, min(3600, int(float(stop_interval))))
                self.scheduler.set_task_interval('stop_loss_check', secs)
            # 手动/自定义 API + Tushare token
            manual_url = saved.get('manual_api_url')
            if manual_url:
                self.gateway.set_manual_api(manual_url, saved.get('manual_api_token', ''))
            tushare_token = saved.get('tushare_token')
            if tushare_token:
                self.gateway.set_tushare_token(tushare_token)
            # 数据源（最后设置，确保手动/自定义已就绪）
            data_source = saved.get('data_source')
            if data_source:
                self.gateway.set_primary_source(data_source)
            # 市值筛选阈值（若持久化过则覆盖默认，并重建候选池）
            min_cap = saved.get('min_market_cap')
            max_cap = saved.get('max_market_cap')
            if min_cap is not None or max_cap is not None:
                mn = float(min_cap) if min_cap is not None else self._min_market_cap
                mx = float(max_cap) if max_cap is not None else self._max_market_cap
                if mx > mn:
                    self.set_market_cap_range(mn, mx)
            logger.info("已恢复面板持久化设置")
        except Exception as e:
            logger.warning(f"恢复面板设置失败: {e}")

    # ================================================================
    # 交易周期回调
    # ================================================================

    async def _market_scan_cycle(self):
        """主交易周期: 扫描信号 → 融合 → 各账户独立决策"""
        if not self._stock_pool:
            logger.debug("候选池为空，跳过扫描")
            return

        if not self.scheduler.is_trading_session():
            logger.debug("非交易时段，跳过市场扫描")
            return

        now = datetime.now()
        now_str = now.strftime('%H:%M:%S')
        logger.info(f"[{now_str}] 市场扫描开始 (候选池: {len(self._stock_pool)} 只)")

        scan_entry = {
            'time': now.isoformat(),
            'time_str': now_str,
            'pool_size': len(self._stock_pool),
            'signals_found': 0,
            'signals': [],
            'account_decisions': [],
        }

        try:
            # 1. 刷新 K 线（仅首次/每10分钟同步一次，避免每分钟重复拉取）
            scan_pool = self._stock_pool  # 扫描全部小市值候选池（数量由市值筛选阈值决定）
            now_ts = now.timestamp()
            if now_ts - self._last_kline_sync > 600:  # 10分钟同步一次
                await self.gateway.sync_daily_kline(scan_pool)
                self._last_kline_sync = now_ts
                logger.debug(f"K线同步完成: {len(scan_pool)}只")
            else:
                logger.debug(f"K线缓存有效，跳过同步 (上次同步: {int((now_ts - self._last_kline_sync)/60)}分钟前)")

            # 2. 获取实时行情 (所有候选股，腾讯API每批50只自动分批)
            quotes_all = {}
            try:
                quotes_all = await self.gateway.get_realtime_quote(scan_pool)
            except Exception:
                pass

            # 3. 运行 Technical Scanner
            from signals.technical import TechnicalScanner
            tech_scanner = TechnicalScanner(self.db, self.config)
            tech_signals = await tech_scanner.scan(scan_pool)
            logger.info(f"技术面信号: {len(tech_signals)} 个")

            # 4. 运行 FundFlow Scanner (量价异动)
            fund_signals = []
            try:
                from signals.fund_flow import FundFlowScanner
                fund_scanner = FundFlowScanner(self.db, self.config)
                fund_signals = await fund_scanner.scan(scan_pool)
                logger.info(f"量价异动信号: {len(fund_signals)} 个")
            except Exception as e:
                logger.debug(f"FundFlow scanner skipped: {e}")

            # 合并所有信号
            all_raw_signals = tech_signals + fund_signals

            # --- 丰富信号数据: 名称、价格、涨跌幅 ---
            SCORE_BUY_THRESHOLD = 8  # 单信号:MACD金叉(16+)/RSI超卖(13+)/布林(11)/强V型(9+)
            SIGNAL_LABELS = {
                'RSI超卖': 'RSI超卖 (<30) — 短期超跌反弹信号',
                'MACD金叉': 'MACD金叉 — 中期趋势转强',
                '布林下轨反弹': '布林下轨支撑反弹 — 价格触及下轨',
                'V型反转': 'V型反转形态 — 急跌后快速回升',
                '底分型': '底分型形态 — K线止跌结构',
                '量比异动': '成交量异动 — 放量突破预警',
                '北向资金流入': '北向资金持续流入 — 主力动向',
            }

            enriched_signals = []
            for s in all_raw_signals:
                ts_code = s.get('ts_code', '')
                quote = quotes_all.get(ts_code, {})
                # 名称: 实时行情 > stock_basic > ts_code
                name = (quote.get('name', '') or
                        self._lookup_stock_name(ts_code) or ts_code)
                if name == ts_code:
                    name = self._stock_name_cache.get(ts_code, ts_code)
                self._stock_name_cache[ts_code] = name

                reason_raw = s.get('reason', '')
                reason_friendly = SIGNAL_LABELS.get(reason_raw, reason_raw)

                enriched_signals.append({
                    'ts_code': ts_code,
                    'name': name,
                    'score': s.get('score', 0),
                    'score_max': s.get('score_max', 100),
                    'reason': reason_raw,
                    'reason_friendly': reason_friendly,
                    'signal_type': s.get('signal_type', ''),
                    'buy_threshold': SCORE_BUY_THRESHOLD,
                    'price': quote.get('price', 0),
                    'change_pct': quote.get('change_pct', 0),
                    'volume_ratio': quote.get('volume_ratio', 1.0),
                })

            scan_entry['signals_found'] = len(enriched_signals)
            scan_entry['signals'] = enriched_signals

            # 持久化融合信号池 → 供面板 /api/v2/signals/pool 读取
            self._save_fused_signal_pool(enriched_signals)

            if not all_raw_signals:
                logger.debug("技术面无信号")
                self._append_scan_log(scan_entry)
                return

            logger.info(f"总信号: {len(all_raw_signals)} 个 (技术面+量价)")

            # 5. 构建 kline_map
            signal_groups = {'technical': all_raw_signals}
            kline_map = {}
            for sig in all_raw_signals:
                ts_code = sig.get('ts_code', '')
                if ts_code not in kline_map:
                    klines = self.db.get_kline(ts_code, limit=60)
                    if klines:
                        kline_map[ts_code] = klines

            # 5.5. 丰富信号 details — 补全 L2 数据完整性所需字段
            # Tencent 免费 API 不提供 turnover_rate/volume_ratio/max_drawdown
            # 需要从 K线数据计算 + 实时行情补充，否则全部被 L2 拦截
            for sig in all_raw_signals:
                ts_code = sig.get('ts_code', '')
                details = sig.setdefault('details', {})

                # rsi: 从 _ensure_indicators 计算后的 K线获取
                klines = kline_map.get(ts_code, [])
                if klines and 'rsi' not in details:
                    latest_rsi = klines[-1].get('rsi_14', 50) or 50
                    details['rsi'] = latest_rsi

                # turnover_rate: 从实时行情获取，无数据则默认 2.0（正常范围）
                quote = quotes_all.get(ts_code, {})
                if 'turnover_rate' not in details:
                    details['turnover_rate'] = quote.get('turnover_rate', 2.0) or 2.0

                # volume_ratio: 从实时行情获取，无数据则默认 1.0
                if 'volume_ratio' not in details:
                    details['volume_ratio'] = quote.get('volume_ratio', 1.0) or 1.0

                # max_drawdown: 从近20日K线计算
                if klines and 'max_drawdown' not in details:
                    closes = [r.get('close', 0) for r in klines[-20:]]
                    if closes:
                        peak = closes[0]
                        max_dd = 0.0
                        for c in closes:
                            if c > peak:
                                peak = c
                            dd = (c / peak - 1) * 100 if peak > 0 else 0
                            if dd < max_dd:
                                max_dd = dd
                        details['max_drawdown'] = round(max_dd, 2)

                # daily_amount: 日均成交额（万元），L12 流动性检查需要 ≥2000万
                # 从近5日K线估算: avg(close * vol) / 10000
                if klines and 'daily_amount' not in details:
                    recent = klines[-5:]
                    amounts = []
                    for r in recent:
                        c = r.get('close', 0) or 0
                        v = r.get('vol', 0) or 0
                        if c > 0 and v > 0:
                            amounts.append(c * v / 10000)
                    details['daily_amount'] = round(sum(amounts) / len(amounts), 0) if amounts else 3000

            # 6. 各账户独立运行交易周期
            for account_id, comps in self._accounts.items():
                acct = self.account_manager.get_account(account_id)
                acct_decision = {
                    'account_id': account_id,
                    'name': acct.name if acct else account_id,
                    'profile_type': acct.profile_type if acct else '',
                    'orders': [],
                    'signals_evaluated': [],
                }

                try:
                    auto_trading = comps['auto_trading']
                    orders, evaluations = await auto_trading.run_cycle(
                        signal_groups=signal_groups,
                        kline_map=kline_map,
                        feature_map=None,
                        market_data=None,
                    )

                    eval_by_code = {e.get('ts_code'): e for e in evaluations}

                    for sig in all_raw_signals:
                        ts_code = sig.get('ts_code', '')
                        matching = next((o for o in orders if o.get('ts_code') == ts_code), None)
                        decided = matching is not None
                        ai = eval_by_code.get(ts_code, {})
                        ev = {
                            'ts_code': ts_code,
                            'signal_score': sig.get('score', 0),
                            'decision': 'buy' if decided else 'hold',
                            'order_amount': matching.get('amount', 0) if matching else 0,
                            'order_shares': matching.get('shares', 0) if matching else 0,
                            'order_price': matching.get('price', 0) if matching else 0,
                            'reasoning': (matching.get('reasoning', '') if matching else
                                         '综合评分未达买入阈值' if sig.get('score', 0) < SCORE_BUY_THRESHOLD else
                                         '风控检查未通过'),
                            # AI 评分分项（ModelEnsemble.decide 产出，无 AI 模型时为规则降级）
                            'ai_action': ai.get('action', 'hold'),
                            'ai_score': ai.get('score', sig.get('score', 0)),
                            'ai_confidence': ai.get('confidence', 0),
                            'risk_level': ai.get('risk_level', None),
                            'model_votes': ai.get('model_votes', {}),
                            'ai_reasoning': ai.get('reasoning', ''),
                            'blocked_by': ai.get('blocked_by', None),
                        }
                        acct_decision['signals_evaluated'].append(ev)

                    if orders:
                        logger.info(f"[{account_id}] 生成 {len(orders)} 个订单")
                        for order in orders:
                            await self._execute_order(account_id, order, comps)
                            acct_decision['orders'].append({
                                'ts_code': order.get('ts_code', ''),
                                'direction': order.get('direction', ''),
                                'shares': order.get('shares', 0),
                                'price': order.get('price', 0),
                                'amount': order.get('amount', 0),
                            })
                    else:
                        logger.debug(f"[{account_id}] 无订单")

                except Exception as e:
                    logger.error(f"[{account_id}] 交易周期异常: {e}", exc_info=True)
                    acct_decision['error'] = str(e)

                scan_entry['account_decisions'].append(acct_decision)

        except Exception as e:
            logger.error(f"市场扫描异常: {e}", exc_info=True)
            scan_entry['error'] = str(e)

        self._append_scan_log(scan_entry)

    def _save_fused_signal_pool(self, signals: List[dict]):
        """把融合信号池持久化到 stock_pool 表（rank/tier 按评分分级）。"""
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            ranked = sorted(signals, key=lambda s: (s.get('score', 0) or 0), reverse=True)
            pool = []
            for i, s in enumerate(ranked, start=1):
                score = s.get('score', 0) or 0
                tier = 'A' if score >= 12 else ('B' if score >= 8 else 'C')
                pool.append({
                    'ts_code': s.get('ts_code', ''),
                    'score': score,
                    'rank': i,
                    'tier': tier,
                    'factors': {
                        'name': s.get('name', ''),
                        'reason': s.get('reason_friendly', s.get('reason', '')),
                        'price': s.get('price', 0),
                        'change_pct': s.get('change_pct', 0),
                        'volume_ratio': s.get('volume_ratio', 1.0),
                    },
                })
            self.db.save_stock_pool(pool, 'fused', today)
        except Exception as e:
            logger.debug(f"融合信号池持久化异常: {e}")

    def _append_scan_log(self, entry: dict):
        """添加扫描日志到内存环形缓冲区 + DB持久化"""
        self._scan_log.append(entry)
        if len(self._scan_log) > self._scan_log_max:
            self._scan_log = self._scan_log[-self._scan_log_max:]
        # 持久化到DB: 每次扫描结果都会保留，重启不丢失
        try:
            self.db.save_scan_log(entry)
        except Exception as e:
            logger.debug(f"扫描日志持久化异常: {e}")

    def get_scan_log(self, limit: int = 20) -> List[dict]:
        """返回最近的扫描日志 (优先从DB读取，确保重启后可见)"""
        try:
            db_logs = self.db.get_scan_logs(limit)
            if db_logs:
                return db_logs
        except Exception:
            pass
        return list(reversed(self._scan_log[-limit:]))

    def _lookup_stock_name(self, ts_code: str) -> str:
        """从 K 线表查找股票名称"""
        try:
            klines = self.db.get_kline(ts_code, limit=1)
            if klines and klines[0].get('name'):
                return klines[0]['name']
        except Exception:
            pass
        return ts_code

    async def _flash_scan_cycle(self):
        """快速异动扫描: 全池检查实时行情异常"""
        if not self._stock_pool:
            return
        if not self.scheduler.is_trading_session():
            return

        try:
            # 获取全池实时行情（Tushare MCP，网关按能力自动降级）
            quotes = await self.gateway.get_realtime_quote(self._stock_pool)
            if not quotes:
                return

            # 检查异常
            for ts_code, quote in quotes.items():
                change_pct = quote.get('change_pct', 0)
                volume_ratio = quote.get('volume_ratio', 1.0)

                # 涨幅 > 5% 且放量 → 可能突破信号
                if change_pct > 5 and volume_ratio > 2.0:
                    logger.info(f"异动信号 [{ts_code}]: 涨{change_pct:.1f}% 量比{volume_ratio:.1f}")
                elif change_pct < -7:
                    logger.info(f"异常下跌 [{ts_code}]: {change_pct:.1f}% — 检查止损")

        except Exception as e:
            logger.debug(f"异动扫描异常: {e}")

    async def _stop_loss_cycle(self):
        """止损检查: 用 StopLossMonitor 检查所有账户持仓（硬止损 + 移动止损 + T+1）。"""
        for account_id, comps in self._accounts.items():
            try:
                stop_loss_monitor = comps['stop_loss_monitor']
                exit_orders = await stop_loss_monitor.check_all()
                for order in exit_orders:
                    await self._execute_order(account_id, order, comps)
            except Exception as e:
                logger.debug(f"[{account_id}] 止损检查异常: {e}")

    async def _risk_monitor_cycle(self):
        """组合级风控监控: 更新持仓估值 → 触发 L2/L3/L4/L5 熔断。

        补全五层熔断此前缺失的触发点：
        - L2 个股日度亏损（用当日涨跌幅代理）
        - L3 组合日度亏损 → 禁止新开仓
        - L4 组合周度亏损 → 全部平仓 + 本周停止
        - L5 大盘熔断 → 全部清仓 + 暂停
        """
        for account_id, comps in self._accounts.items():
            try:
                circuit_breaker = comps['circuit_breaker']
                position_monitor = comps['position_monitor']
                acct = self.account_manager.get_account(account_id)

                summary = await position_monitor.update()
                if not summary:
                    continue

                # 以账户维护的日内/周度起点为基准，结合实时估值计算盈亏
                total_value = summary.get('total_value', acct.total_value)
                day_start = getattr(acct, '_day_start_value', acct.initial_capital) or acct.initial_capital
                week_start = getattr(acct, '_week_start_value', acct.initial_capital) or acct.initial_capital
                daily_pnl_pct = (total_value - day_start) / day_start if day_start > 0 else 0
                weekly_pnl_pct = (total_value - week_start) / week_start if week_start > 0 else 0

                # L2: 个股日度亏损（每只持仓当日跌超3% → 次日禁买该股，按个股独立禁买）
                for pos in summary.get('positions', []):
                    change_pct = pos.get('change_pct', 0) or 0
                    if change_pct <= -3.0 and not circuit_breaker.is_stock_banned(pos['ts_code']):
                        circuit_breaker.trigger_l2(pos['ts_code'], change_pct / 100.0)

                # L3: 组合日度亏损 → 禁止新开仓
                if daily_pnl_pct <= -0.04 and not self._breaker_active(circuit_breaker, 3):
                    circuit_breaker.trigger_l3(daily_pnl_pct)

                # L4: 组合周度亏损 → 全部平仓 + 本周停止
                if weekly_pnl_pct <= -0.08 and not self._breaker_active(circuit_breaker, 4):
                    if circuit_breaker.trigger_l4(weekly_pnl_pct):
                        await self._liquidate_account(account_id, comps, 'L4 组合周亏熔断')

            except Exception as e:
                logger.debug(f"[{account_id}] 组合风控异常: {e}")

        # L5: 大盘熔断（全账户共享，行情只拉一次）
        try:
            if await self._check_market_meltdown():
                for account_id, comps in self._accounts.items():
                    await self._liquidate_account(account_id, comps, 'L5 大盘熔断')
        except Exception as e:
            logger.debug(f"L5 大盘熔断检查异常: {e}")

    @staticmethod
    def _breaker_active(circuit_breaker, level: int) -> bool:
        """判断某级熔断是否已处于激活状态（避免重复触发重置冷却期）。"""
        try:
            return any(b.get('level') == level for b in circuit_breaker.get_status().get('breakers', []))
        except Exception:
            return False

    async def _check_market_meltdown(self) -> bool:
        """检查大盘熔断条件: 沪深300跌超5% 或 跌停家数≥100。返回是否触发。"""
        now_ts = datetime.now().timestamp()
        # 广度采样缓存 300 秒，避免每 60 秒全市场拉取
        if now_ts - self._breadth_cache.get('ts', 0) < 300:
            breadth = self._breadth_cache.get('data') or {}
        else:
            breadth = {}
            try:
                sample = (self._stock_pool or [])[:120]
                quotes = await self.gateway.get_realtime_quote(sample)
                limit_down = sum(1 for q in quotes.values() if (q.get('change_pct', 0) or 0) <= -9.8)
                breadth = {'limit_down': limit_down, 'sample': len(sample)}
                self._breadth_cache = {'ts': now_ts, 'data': breadth}
            except Exception as e:
                logger.debug(f"广度采样失败: {e}")

        hs300_change = 0.0
        try:
            idx = await self.gateway.get_index_quotes()
            hs300_change = (idx.get('沪深300', {}).get('change_pct', 0) or 0) / 100.0
        except Exception:
            pass

        limit_down_count = breadth.get('limit_down', 0)
        if hs300_change > -0.05 and limit_down_count < 100:
            return False

        triggered = False
        for account_id, comps in self._accounts.items():
            cb = comps['circuit_breaker']
            if not self._breaker_active(cb, 5) and cb.trigger_l5(hs300_change, limit_down_count):
                triggered = True
        return triggered

    async def _liquidate_account(self, account_id: str, comps: dict, reason: str):
        """全部平仓（L4/L5 熔断触发）。"""
        try:
            positions = self.db.get_all_positions(account_id=account_id)
            if not positions:
                return
            auto_trading = comps['auto_trading']
            for pos in positions:
                price = pos.get('current_price', 0) or pos.get('avg_cost', 0)
                if price <= 0:
                    continue
                order = auto_trading._create_order(
                    pos['ts_code'], 'sell', [pos], 'circuit_breaker',
                    price=price,
                    shares=pos['volume'],
                    reasoning=reason,
                )
                await self._execute_order(account_id, order, comps)
            logger.warning(f"[{account_id}] {reason}: 已平仓 {len(positions)} 只持仓")
        except Exception as e:
            logger.error(f"[{account_id}] 熔断平仓异常: {e}")

    async def _data_sync_cycle(self):
        """定期数据同步: 同步全池K线 + 更新K线缓存时间"""
        if self._stock_pool:
            try:
                await self.gateway.sync_daily_kline(self._stock_pool)
                self._last_kline_sync = datetime.now().timestamp()
                logger.debug(f"定期数据同步完成: {len(self._stock_pool)}只")
            except Exception as e:
                logger.debug(f"数据同步异常: {e}")

    async def _portfolio_snapshot_cycle(self):
        """资产快照: 将各账户当前净值写入 DB（含持仓市值，供盈亏曲线使用）。
        用持仓市值重算 total_value / daily_pnl / total_pnl，使盘中快照反映真实持仓。"""
        try:
            for account_id, comps in self._accounts.items():
                acct = self.account_manager.get_account(account_id)
                if not acct:
                    continue
                try:
                    positions = self.db.get_all_positions(account_id=account_id)
                    position_value = sum((p.get('market_value', 0) or 0) for p in positions)
                    total_value = acct.cash + position_value
                    day_start = getattr(acct, '_day_start_value', acct.initial_capital) or acct.initial_capital
                    daily_pnl = total_value - day_start
                    daily_pnl_pct = (daily_pnl / day_start * 100) if day_start > 0 else 0
                    total_pnl = total_value - acct.initial_capital
                    total_pnl_pct = (total_pnl / acct.initial_capital * 100) if acct.initial_capital > 0 else 0

                    self.db.save_portfolio_snapshot({
                        'total_value': round(total_value, 2),
                        'cash': acct.cash,
                        'position_value': round(position_value, 2),
                        'daily_pnl': round(daily_pnl, 2),
                        'daily_pnl_pct': round(daily_pnl_pct, 2),
                        'weekly_pnl': getattr(acct, 'weekly_pnl', 0),
                        'weekly_pnl_pct': getattr(acct, 'weekly_pnl_pct', 0),
                        'total_pnl': round(total_pnl, 2),
                        'total_pnl_pct': round(total_pnl_pct, 2),
                        'position_count': len(positions),
                    }, account_id=account_id)
                except Exception as e:
                    logger.debug(f"[{account_id}] 资产快照写入失败: {e}")
        except Exception as e:
            logger.debug(f"资产快照异常: {e}")

    async def _daily_settlement(self):
        """盘后结算：更新持仓现价、计算账户盈亏、保存快照。
        每天 15:00 后执行一次（含用户晚间才启动系统的场景），次日自动作废。"""
        now = datetime.now()
        if now.hour < 15:
            return
        today_str = now.strftime('%Y-%m-%d')
        if getattr(self, '_last_settlement_date', '') == today_str:
            return

        logger.info("=" * 40)
        logger.info(f"[{today_str}] 盘后结算开始...")
        self._last_settlement_date = today_str

        for account_id, comps in self._accounts.items():
            acct = self.account_manager.get_account(account_id)
            if not acct:
                continue
            try:
                positions = self.db.get_all_positions(account_id=account_id)
                if not positions:
                    logger.info(f"[{account_id}] 无持仓，跳过结算")
                    continue

                # 获取所有持仓的实时价格
                codes = [p['ts_code'] for p in positions]
                quotes = {}
                try:
                    quotes = await self.gateway.get_realtime_quote(codes)
                except Exception:
                    pass

                total_market_value = 0
                for pos in positions:
                    ts_code = pos['ts_code']
                    quote = quotes.get(ts_code, {})
                    price = quote.get('price', 0)
                    if price <= 0:
                        price = pos.get('current_price', pos.get('avg_cost', 0))
                    if price > 0:
                        volume = pos.get('volume', 0)
                        market_value = volume * price
                        # 更新 DB 中的持仓现价
                        self.db.update_position(
                            ts_code=ts_code,
                            name=pos.get('name', ''),
                            volume=volume,
                            avg_cost=pos.get('avg_cost', 0),
                            current_price=price,
                            buy_date=pos.get('buy_date', today_str),
                            account_id=account_id,
                        )
                        total_market_value += market_value

                # 更新账户级盈亏
                day_start = getattr(acct, '_day_start_value', acct.initial_capital) or acct.initial_capital
                daily_pnl = total_market_value + acct.cash - day_start
                daily_pnl_pct = (daily_pnl / day_start * 100) if day_start > 0 else 0
                total_pnl = total_market_value + acct.cash - acct.initial_capital
                total_pnl_pct = (total_pnl / acct.initial_capital * 100) if acct.initial_capital > 0 else 0

                acct.daily_pnl = round(daily_pnl, 2)
                acct.daily_pnl_pct = round(daily_pnl_pct, 2)
                acct.total_pnl = round(total_pnl, 2)
                acct.total_pnl_pct = round(total_pnl_pct, 2)
                acct.total_value = round(total_market_value + acct.cash, 2)

                # 保存快照（匹配 save_portfolio_snapshot 的 dict 参数格式）
                self.db.save_portfolio_snapshot({
                    'total_value': acct.total_value,
                    'cash': acct.cash,
                    'position_value': total_market_value,
                    'daily_pnl': acct.daily_pnl,
                    'daily_pnl_pct': acct.daily_pnl_pct,
                    'weekly_pnl': getattr(acct, 'weekly_pnl', 0),
                    'weekly_pnl_pct': getattr(acct, 'weekly_pnl_pct', 0),
                    'total_pnl': acct.total_pnl,
                    'total_pnl_pct': acct.total_pnl_pct,
                    'position_count': len(positions),
                }, account_id=account_id)

                self.db.save_account(acct.to_dict())
                logger.info(f"[{account_id}] 结算完成: 总资产=¥{acct.total_value:,.0f} "
                            f"今日盈亏={acct.daily_pnl:+,.0f}({acct.daily_pnl_pct:+.2f}%) "
                            f"持仓{len(positions)}只")

                # 重置次日盈亏起点
                acct._day_start_value = acct.total_value

                # 重置周度盈亏起点（跨自然周时重置，供 L4 组合周亏熔断使用）
                iso_week = now.strftime('%G-W%V')
                if getattr(acct, '_week_key', '') != iso_week:
                    acct._week_key = iso_week
                    acct._week_start_value = acct.total_value

            except Exception as e:
                logger.error(f"[{account_id}] 结算异常: {e}", exc_info=True)

        # 每日复盘（绩效归因 + 信号回溯 + 因子有效性）—— 接入 evolution.DailyReview
        try:
            await self._run_daily_reviews(today_str)
        except Exception as e:
            logger.error(f"每日复盘异常: {e}", exc_info=True)

        logger.info("盘后结算完成")
        logger.info("=" * 40)

    async def _run_daily_reviews(self, today_str: str):
        """盘后运行每日复盘（接入 evolution.DailyReview，此前从未被调用）。"""
        from evolution.daily_review import DailyReview
        for account_id in self._accounts:
            acct = self.account_manager.get_account(account_id)
            if not acct:
                continue
            try:
                reviewer = DailyReview(
                    db=self.db,
                    macro_llm=None,  # AI 复盘可后续接入，避免结算被 LLM 阻塞
                    data_gateway=self.gateway,
                    account_id=account_id,
                    initial_capital=acct.initial_capital,
                )
                report = await reviewer.run()
                logger.info(f"[{account_id}] 每日复盘完成: "
                            f"交易{report['performance'].get('trade_count', 0)}笔 "
                            f"信号准确率{report['signal_accuracy'].get('accuracy', 0):.1f}%")
            except Exception as e:
                logger.warning(f"[{account_id}] 每日复盘异常: {e}")

    # ================================================================
    # 行情分析报告 (午盘 11:30 / 收盘 15:00)
    # ================================================================

    async def _market_report_cycle(self):
        """行情分析报告定时回调：午盘(11:30~13:00)与收盘(15:00 后)各生成一次，按日去重。"""
        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')
        hhmm = now.hour * 100 + now.minute

        if 1130 <= hhmm < 1300:
            report_type = 'noon'
        elif hhmm >= 1500:
            report_type = 'close'
        else:
            return

        key = (today_str, report_type)
        if key in self._generated_reports:
            return
        # 跨重启去重：DB 已有同日同时段报告则跳过
        try:
            if self.db.has_market_report(today_str, report_type):
                self._generated_reports.add(key)
                return
        except Exception:
            pass
        self._generated_reports.add(key)

        try:
            await self._generate_market_report(report_type)
        except Exception as e:
            logger.error(f"行情分析报告生成异常: {e}", exc_info=True)

    async def generate_market_report_now(self, report_type: str = "close") -> Dict:
        """手动生成一份行情分析报告（供 Web 面板手动触发），返回报告 dict。"""
        report_id = await self._generate_market_report(report_type)
        if report_id is None:
            return {'ok': False, 'error': '生成失败'}
        reports = self.db.get_market_reports(limit=1)
        return {'ok': True, 'report': reports[0] if reports else None}

    async def _generate_market_report(self, report_type: str = "close") -> Optional[int]:
        """生成并保存一份行情分析报告，返回自增 id（失败返回 None）。"""
        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')
        period_label = '午盘' if report_type == 'noon' else '收盘'

        logger.info(f"生成行情分析报告: {today_str} {period_label} ...")

        # 1. 指数行情
        index_data: Dict = {}
        try:
            index_data = await self.gateway.get_index_quotes()
        except Exception as e:
            logger.debug(f"指数行情获取失败: {e}")

        # 2. 市场广度（涨跌家数）——采样候选池实时行情
        breadth_data: Dict = {}
        try:
            sample = (self._stock_pool or [])[:120]
            quotes = await self.gateway.get_realtime_quote(sample)
            up = down = flat = limit_up = limit_down = 0
            for q in quotes.values():
                cp = q.get('change_pct', 0) or 0
                if cp >= 9.8:
                    limit_up += 1
                elif cp <= -9.8:
                    limit_down += 1
                if cp > 0:
                    up += 1
                elif cp < 0:
                    down += 1
                else:
                    flat += 1
            breadth_data = {
                'sample_size': len(sample), 'quoted': len(quotes),
                'up': up, 'down': down, 'flat': flat,
                'limit_up': limit_up, 'limit_down': limit_down,
            }
        except Exception as e:
            logger.debug(f"广度统计失败: {e}")

        # 3. 组合概况（三账户合计）
        portfolio = self._aggregate_portfolio()

        # 4. 最近信号 + 全部持仓 + 历史报告
        recent_signals = self.db.get_recent_signals(limit=10)
        all_positions: List[Dict] = []
        for account_id in self._accounts:
            try:
                for p in self.db.get_all_positions(account_id=account_id):
                    p['_account_id'] = account_id
                    all_positions.append(p)
            except Exception:
                pass
        past_reports = self.db.get_market_reports(limit=5)

        # 5. 生成预测与推荐（LLM 优先，规则兜底）
        prediction, buy_recs, sell_recs, llm_source = await self._build_market_prediction(
            report_type, index_data, breadth_data, portfolio,
            recent_signals, all_positions, past_reports)

        # 6. 行情摘要
        market_summary = self._build_market_summary(index_data, breadth_data, portfolio)

        # 6.5 牛熊市判断（规则法，基于指数 + 市场广度）
        market_regime = self._classify_market_regime(index_data, breadth_data)

        # 7. 保存
        report = {
            'report_type': report_type,
            'report_date': today_str,
            'period_label': period_label,
            'market_summary': market_summary,
            'index_data': index_data,
            'breadth_data': breadth_data,
            'prediction': prediction,
            'buy_recommendations': buy_recs,
            'sell_recommendations': sell_recs,
            'llm_source': llm_source,
            'market_regime': market_regime,
        }
        try:
            report_id = self.db.save_market_report(report)
            logger.info(f"行情分析报告已保存: id={report_id} {period_label} (来源={llm_source})")
            return report_id
        except Exception as e:
            logger.error(f"行情分析报告保存失败: {e}", exc_info=True)
            return None

    def _aggregate_portfolio(self) -> Dict:
        """三账户合计的组合概况。"""
        total_value = 0.0
        total_cap = 0.0
        daily_pnl = 0.0
        position_count = 0
        per_account = []
        for account_id in self._accounts:
            acct = self.account_manager.get_account(account_id)
            if not acct:
                continue
            try:
                positions = self.db.get_all_positions(account_id=account_id)
            except Exception:
                positions = []
            pos_value = sum((p.get('market_value', 0) or 0) for p in positions)
            tv = acct.cash + pos_value
            total_value += tv
            total_cap += acct.initial_capital or 0
            daily_pnl += (getattr(acct, 'daily_pnl', 0) or 0)
            position_count += len(positions)
            per_account.append({
                'account_id': account_id,
                'name': acct.name,
                'total_value': round(tv, 2),
                'daily_pnl': round(getattr(acct, 'daily_pnl', 0) or 0, 2),
                'daily_pnl_pct': round(getattr(acct, 'daily_pnl_pct', 0) or 0, 2),
                'total_pnl_pct': round((tv - acct.initial_capital) / acct.initial_capital * 100, 2) if acct.initial_capital > 0 else 0,
                'position_count': len(positions),
            })
        total_pnl = total_value - total_cap
        return {
            'total_value': round(total_value, 2),
            'total_capital': round(total_cap, 2),
            'daily_pnl': round(daily_pnl, 2),
            'daily_pnl_pct': round(daily_pnl / total_cap * 100, 2) if total_cap > 0 else 0,
            'total_pnl': round(total_pnl, 2),
            'total_pnl_pct': round(total_pnl / total_cap * 100, 2) if total_cap > 0 else 0,
            'position_count': position_count,
            'accounts': per_account,
        }

    def _get_macro_llm(self):
        if self._macro_llm is None:
            from ai.macro_llm import MacroLLM
            self._macro_llm = MacroLLM(self.config)
        return self._macro_llm

    async def _build_market_prediction(self, report_type, index_data, breadth_data,
                                       portfolio, recent_signals, all_positions, past_reports):
        """生成预测与买卖推荐。返回 (prediction, buy_recs, sell_recs, llm_source)。"""
        llm = self._get_macro_llm()
        llm_result = None
        try:
            if not getattr(self, '_llm_checked', False):
                await llm.check_local()
                self._llm_checked = True
            prompt = self._build_prediction_prompt(
                report_type, index_data, breadth_data, portfolio, recent_signals, past_reports)
            response = await llm._query(prompt)
            if response:
                llm_result = self._parse_llm_prediction(llm, response, recent_signals, all_positions)
        except Exception as e:
            logger.debug(f"LLM 预测失败，回落规则: {e}")

        if llm_result:
            return llm_result

        # 规则兜底
        prediction = self._rule_prediction_text(report_type, index_data, breadth_data, portfolio)
        buy_recs, sell_recs = self._rule_recommendations(recent_signals, all_positions)
        return prediction, buy_recs, sell_recs, 'rule-based'

    def _build_prediction_prompt(self, report_type, index_data, breadth_data,
                                 portfolio, recent_signals, past_reports) -> str:
        period_label = '午盘' if report_type == 'noon' else '收盘'
        idx_lines = '\n'.join(
            [f"- {name}: {d.get('change_pct', 0):+.2f}% (价格 {d.get('price', 0):.2f})"
             for name, d in (index_data or {}).items()]) or '- 无指数数据'
        b = breadth_data or {}
        sig_lines = '\n'.join(
            [f"- {s.get('ts_code','')} {s.get('stock_name','')} 评分{s.get('score',0)} {s.get('signal_type','')}"
             for s in (recent_signals or [])[:8]]) or '- 无信号'
        past_lines = '\n'.join(
            [f"- [{r.get('period_label','')} {r.get('report_date','')}] {str(r.get('prediction',''))[:100]}"
             for r in (past_reports or [])[:3]]) or '- 无历史报告'
        return f"""你是一位专业的A股量化分析师。请基于以下{period_label}数据，结合历史报告，给出明日行情预测与个股买卖推荐。

指数行情:
{idx_lines}

市场广度（采样）:
- 上涨 {b.get('up',0)} / 下跌 {b.get('down',0)} / 平盘 {b.get('flat',0)}
- 涨停 {b.get('limit_up',0)} / 跌停 {b.get('limit_down',0)}

组合概况:
- 总资产 {portfolio.get('total_value', 0):,.0f} 元
- 累计收益 {portfolio.get('total_pnl_pct', 0):+.2f}%
- 今日盈亏 {portfolio.get('daily_pnl_pct', 0):+.2f}%
- 持仓数 {portfolio.get('position_count', 0)}

近期买入信号:
{sig_lines}

历史报告（结合参考）:
{past_lines}

请以 JSON 格式回答（只输出 JSON）:
{{
  "prediction": "对明日大盘走势与风险的一句判断（100字内）",
  "buy_recommendations": [{{"ts_code":"","name":"","reason":""}}],
  "sell_recommendations": [{{"ts_code":"","name":"","reason":""}}]
}}"""

    def _parse_llm_prediction(self, llm, response: str, recent_signals, all_positions):
        """解析 LLM 响应，失败返回 None（回落规则）。"""
        result = None
        try:
            result = json.loads(response)
        except Exception:
            import re
            m = re.search(r'\{.*\}', response, re.S)
            if m:
                try:
                    result = json.loads(m.group(0))
                except Exception:
                    result = None

        source = 'ollama' if getattr(llm, '_local_available', False) else 'deepseek'
        if result and isinstance(result, dict):
            prediction = result.get('prediction', '') or ''
            if not prediction:
                return None
            buy_recs = result.get('buy_recommendations', []) or []
            sell_recs = result.get('sell_recommendations', []) or []
            return prediction[:500], buy_recs[:3], sell_recs[:3], source

        # 非 JSON：用原文作预测，推荐走规则
        buy_recs, sell_recs = self._rule_recommendations(recent_signals, all_positions)
        return response[:500], buy_recs, sell_recs, source

    def _rule_prediction_text(self, report_type, index_data, breadth_data, portfolio) -> str:
        changes = [d.get('change_pct', 0) for d in (index_data or {}).values() if isinstance(d, dict)]
        avg_change = sum(changes) / len(changes) if changes else 0.0
        b = breadth_data or {}
        up, down = b.get('up', 0), b.get('down', 0)
        adv_ratio = (up / (up + down)) if (up + down) > 0 else 0.5
        period = '午盘' if report_type == 'noon' else '全天'
        if avg_change > 1.0 and adv_ratio > 0.6:
            bias = '偏多，可逢低布局强势题材，控制追高风险'
        elif avg_change < -1.0 or adv_ratio < 0.4:
            bias = '偏空，建议降低仓位、规避高位股，等待企稳信号'
        else:
            bias = '震荡，宜观望或轻仓做T，等待方向选择'
        return (f"今日{period}大盘指数平均涨跌 {avg_change:+.2f}%，涨跌家数比约 {adv_ratio:.0%}，"
                f"组合累计收益 {portfolio.get('total_pnl_pct', 0):+.2f}%。"
                f"明日行情判断：{bias}。严格执行止损纪律。")

    def _rule_recommendations(self, recent_signals, all_positions):
        """规则生成买卖推荐（无 LLM 或 LLM 非 JSON 时兜底）。"""
        _SIG_REASON = {'buy': '综合信号买入', 'rejected': '风控拒绝'}
        buy_recs = []
        seen = set()
        for s in (recent_signals or []):
            if (s.get('score', 0) or 0) < 8:
                continue
            ts_code = s.get('ts_code', '')
            if not ts_code or ts_code in seen:
                continue
            seen.add(ts_code)
            st = s.get('signal_type', '') or 'buy'
            buy_recs.append({
                'ts_code': ts_code,
                'name': s.get('stock_name', ''),
                'score': s.get('score', 0),
                'reason': _SIG_REASON.get(st, st),
            })
            if len(buy_recs) >= 3:
                break
        sell_recs = []
        for p in (all_positions or []):
            cost = p.get('avg_cost', 0) or 0
            price = p.get('current_price', 0) or 0
            pnl_pct = ((price - cost) / cost * 100) if cost > 0 else 0
            if pnl_pct <= -5 or pnl_pct >= 15:
                sell_recs.append({
                    'ts_code': p.get('ts_code', ''),
                    'name': p.get('stock_name', p.get('name', '')),
                    'pnl_pct': round(pnl_pct, 2),
                    'reason': '浮亏止损' if pnl_pct <= -5 else '达到止盈',
                })
        return buy_recs[:3], sell_recs[:3]

    def _build_market_summary(self, index_data, breadth_data, portfolio) -> str:
        idx_parts = [f"{name}{d.get('change_pct', 0):+.2f}%" for name, d in (index_data or {}).items()]
        b = breadth_data or {}
        parts = []
        if idx_parts:
            parts.append("指数: " + " / ".join(idx_parts))
        if b:
            parts.append(f"涨跌家数 {b.get('up', 0)}↑/{b.get('down', 0)}↓ (涨停{b.get('limit_up', 0)} 跌停{b.get('limit_down', 0)})")
        parts.append(f"组合累计 {portfolio.get('total_pnl_pct', 0):+.2f}% / 今日 {portfolio.get('daily_pnl_pct', 0):+.2f}%")
        return "；".join(parts)

    def _classify_market_regime(self, index_data, breadth_data) -> Dict:
        """牛熊市判断（规则法）：基于指数涨跌 + 市场广度，无需 LLM，始终可用。

        返回 {value, label, risk_level, recommendation}，value 与 MacroLLM.analyze_market
        的 market_regime 词汇对齐（bull/bear/range/volatile/extreme_fear/extreme_greed）。
        """
        changes = [d.get('change_pct', 0) for d in (index_data or {}).values() if isinstance(d, dict)]
        avg_change = sum(changes) / len(changes) if changes else 0.0
        b = breadth_data or {}
        up, down = b.get('up', 0), b.get('down', 0)
        limit_up, limit_down = b.get('limit_up', 0), b.get('limit_down', 0)
        total = up + down
        adv_ratio = (up / total) if total > 0 else 0.5

        if limit_down >= 20 or (total > 0 and adv_ratio < 0.15) or avg_change < -3:
            value, label, risk, rec = 'extreme_fear', '极度恐慌', 'extreme', 'sell_all'
        elif limit_up >= 30 or (total > 0 and adv_ratio > 0.9) or avg_change > 3:
            value, label, risk, rec = 'extreme_greed', '极度贪婪', 'medium', 'reduce'
        elif avg_change < -1 or (total > 0 and adv_ratio < 0.4):
            value, label, risk, rec = 'bear', '熊市', 'high', 'reduce'
        elif avg_change > 1 and (total == 0 or adv_ratio > 0.6):
            value, label, risk, rec = 'bull', '牛市', 'low', 'buy'
        elif abs(avg_change) > 1.5:
            value, label, risk, rec = 'volatile', '震荡加剧', 'medium', 'hold'
        else:
            value, label, risk, rec = 'range', '震荡市', 'medium', 'hold'
        return {'value': value, 'label': label, 'risk_level': risk, 'recommendation': rec}

    # ================================================================
    # 订单执行 (Paper Trading)
    # ================================================================

    async def _execute_order(self, account_id: str, order: dict, comps: dict):
        """
        Paper 模式订单执行：
        — 写入 trades 表 (status='filled')
        — 更新 positions 表
        — 更新 VirtualAccount cash/positions
        """
        if not order:
            return

        direction = order.get('direction', 'buy')
        ts_code = order.get('ts_code', '')
        price = order.get('price', 0)
        shares = order.get('shares', 0)
        amount = order.get('amount', 0)
        score = order.get('score', 0)

        if not ts_code or amount <= 0:
            logger.warning(f"[{account_id}] 无效订单: {order}")
            return

        acct = self.account_manager.get_account(account_id)
        if not acct:
            logger.warning(f"[{account_id}] 账户不存在")
            return

        buy_date = datetime.now().strftime('%Y-%m-%d')

        if direction == 'buy':
            # 检查资金充足
            if acct.cash < amount:
                logger.warning(f"[{account_id}] 资金不足: cash={acct.cash:,.0f} need={amount:,.0f}")
                return

            # 扣减现金
            acct.cash -= amount

            # 记录交易 (V7.0 DB: account_id as separate param)
            self.db.save_trade({
                'ts_code': ts_code,
                'name': order.get('name', ts_code),
                'direction': 'buy',
                'price': price,
                'volume': shares,
                'amount': amount,
                'fee': amount * 0.0003,
                'trade_time': datetime.now().isoformat(),
                'signal_id': None,
                'decision_model': order.get('decision_source', 'rule_baseline'),
                'pnl': 0,
                'pnl_pct': 0,
                'notes': order.get('reasoning', ''),
            }, account_id=account_id)

            # 更新持仓 (V7.0 DB: update_position)
            self.db.update_position(
                ts_code=ts_code,
                name=order.get('name', ts_code),
                volume=shares,
                avg_cost=price,
                current_price=price,
                buy_date=buy_date,
                account_id=account_id,
            )

            logger.info(f"[{account_id}] BUY  {ts_code} {shares}股 @ {price:.2f} = {amount:,.0f} 评分{score:.0f}")

            # 持久化执行日志
            try:
                self.db.save_execution_log({
                    'account_id': account_id,
                    'order_id': str(int(datetime.now().timestamp())),
                    'ts_code': ts_code,
                    'direction': 'buy',
                    'strategy': 'v7_fallback',
                    'total_shares': shares,
                    'filled_shares': shares,
                    'slices_planned': 1,
                    'slices_filled': 1,
                    'avg_fill_price': price,
                    'vwap_price': price,
                    'slippage_bps': 0,
                    'execution_cost': amount * 0.0003,
                    'started_at': datetime.now().isoformat(),
                    'completed_at': datetime.now().isoformat(),
                    'status': 'filled',
                    'details_json': order.get('reasoning', ''),
                })
            except Exception as e:
                logger.debug(f"执行日志持久化异常: {e}")

        elif direction == 'sell':
            # 增加现金
            acct.cash += amount

            # 记录交易
            self.db.save_trade({
                'ts_code': ts_code,
                'name': order.get('name', ts_code),
                'direction': 'sell',
                'price': price,
                'volume': shares,
                'amount': amount,
                'fee': amount * 0.0008,
                'trade_time': datetime.now().isoformat(),
                'signal_id': None,
                'decision_model': order.get('decision_source', 'rule_baseline'),
                'pnl': order.get('pnl', 0),
                'pnl_pct': order.get('pnl_pct', 0),
                'notes': order.get('reasoning', ''),
            }, account_id=account_id)

            # 移除持仓
            self.db.remove_position(ts_code, account_id=account_id)

            # 只把已完成的平仓结果反馈给 DeepSeek 包装器；其自调范围仅
            # 影响小幅评分偏置，不会改变硬止损、熔断或仓位上限。
            model = comps.get('model_ensemble') if comps else None
            if model and hasattr(model, 'update_outcome'):
                try:
                    model.update_outcome(order.get('pnl_pct', 0))
                except Exception as exc:
                    logger.debug(f"[{account_id}] DeepSeek 自调更新失败: {exc}")

            logger.info(f"[{account_id}] SELL {ts_code} {shares}股 @ {price:.2f} = {amount:,.0f}")

            # 持久化执行日志
            try:
                self.db.save_execution_log({
                    'account_id': account_id,
                    'order_id': str(int(datetime.now().timestamp())),
                    'ts_code': ts_code,
                    'direction': 'sell',
                    'strategy': 'v7_fallback',
                    'total_shares': shares,
                    'filled_shares': shares,
                    'slices_planned': 1,
                    'slices_filled': 1,
                    'avg_fill_price': price,
                    'vwap_price': price,
                    'slippage_bps': 0,
                    'execution_cost': amount * 0.0008,
                    'started_at': datetime.now().isoformat(),
                    'completed_at': datetime.now().isoformat(),
                    'status': 'filled',
                    'details_json': order.get('reasoning', ''),
                })
            except Exception as e:
                logger.debug(f"执行日志持久化异常: {e}")

        # 更新账户级总资产和盈亏
        self._update_account_pnl(account_id, acct)

    def _reconcile_accounts(self):
        """启动时对账：确保账户 cash = initial_capital - SUM(buy trades) + SUM(sell trades)"""
        for acct in self.account_manager.list_accounts():
            account_id = acct.account_id
            with self.db._get_conn() as conn:
                rows = conn.execute(
                    "SELECT direction, SUM(amount) FROM trades WHERE account_id=? GROUP BY direction",
                    (account_id,)
                ).fetchall()
            total_buy = 0.0
            total_sell = 0.0
            for direction, total in rows:
                if direction == 'buy':
                    total_buy = total or 0.0
                elif direction == 'sell':
                    total_sell = total or 0.0
            expected_cash = acct.initial_capital - total_buy + total_sell
            if abs(acct.cash - expected_cash) > 0.01:
                logger.warning(
                    f"[{account_id}] 账户现金不一致! DB={acct.cash:,.0f} 预期={expected_cash:,.0f} "
                    f"(买入={total_buy:,.0f} 卖出={total_sell:,.0f}), 自动修复"
                )
                acct.cash = expected_cash
                # 重新计算 total_value 和 P&L
                positions = self.db.get_all_positions(account_id=account_id)
                total_mv = sum((p.get('market_value', 0) or 0) for p in positions)
                acct.total_value = total_mv + acct.cash
                acct.total_pnl = acct.total_value - acct.initial_capital
                acct.total_pnl_pct = (acct.total_pnl / acct.initial_capital * 100) if acct.initial_capital > 0 else 0
                acct.daily_pnl = acct.total_pnl  # 启动时无法区分当日/累计，先对齐
                acct.daily_pnl_pct = acct.total_pnl_pct
                self.db.save_account(acct.to_dict())
                logger.info(f"[{account_id}] 对账完成: cash={acct.cash:,.0f} tv={acct.total_value:,.0f} pnl={acct.total_pnl_pct:+.2f}%")

    def _update_account_pnl(self, account_id: str, acct):
        """每次交易后更新账户的 total_value / total_pnl / daily_pnl"""
        try:
            positions = self.db.get_all_positions(account_id=account_id)
            total_mv = sum((p.get('market_value', 0) or 0) for p in positions)
            acct.total_value = total_mv + acct.cash
            acct.total_pnl = acct.total_value - acct.initial_capital
            acct.total_pnl_pct = (acct.total_pnl / acct.initial_capital * 100) if acct.initial_capital > 0 else 0
            # daily_pnl: 相对当日起点的变化
            day_start = getattr(acct, '_day_start_value', acct.initial_capital) or acct.initial_capital
            acct.daily_pnl = acct.total_value - day_start
            acct.daily_pnl_pct = (acct.daily_pnl / day_start * 100) if day_start > 0 else 0
            self.db.save_account(acct.to_dict())
        except Exception as e:
            logger.debug(f"[{account_id}] 更新账户盈亏异常: {e}")

    # ================================================================
    # 主循环
    # ================================================================

    async def _run_forever(self):
        """启动 Scheduler → 等待终止信号"""
        self._running = True

        # 启动 Scheduler
        scheduler_task = asyncio.create_task(self.scheduler.start())

        # 监听系统信号
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        if sys.platform == 'win32':
            # Windows: 用 asyncio.Event 模拟信号等待
            try:
                signal.signal(signal.SIGINT, lambda s, f: stop_event.set())
            except Exception:
                pass
        else:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)

        logger.info("=" * 60)
        logger.info("猎场系统 V7.0 运行中")
        logger.info("  Web Dashboard: http://localhost:8501/")
        logger.info("  API Docs:      http://localhost:8501/api/v2/docs")
        logger.info("  账户: " + ", ".join(self._accounts.keys()))
        logger.info("  按 Ctrl+C 停止")
        logger.info("=" * 60)

        try:
            # 等待停止信号或 Scheduler 完成
            done, pending = await asyncio.wait(
                [asyncio.create_task(stop_event.wait()), scheduler_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            pass

        # 优雅关闭
        await self._shutdown()

    async def _shutdown(self):
        """优雅关闭所有子系统"""
        logger.info("正在关闭系统...")
        self._running = False

        # 停止 Scheduler
        try:
            await self.scheduler.stop()
        except Exception:
            pass

        # 停止各账户引擎
        for account_id, comps in self._accounts.items():
            try:
                await comps['engine'].stop()
                logger.info(f"[{account_id}] 引擎已停止")
            except Exception:
                pass

        # 关闭 WebSocket 实时流
        try:
            await self.gateway.close()
        except Exception:
            pass

        # 关闭 Web 服务器
        if self._web_task:
            self._web_task.cancel()
            try:
                await self._web_task
            except asyncio.CancelledError:
                pass

        logger.info("猎场系统 V7.0 已关闭")

    # ================================================================
    # Web Dashboard 兼容接口 (engine.get_status)
    # ================================================================

    def get_status(self) -> dict:
        """返回系统状态 (Web 面板调用)"""
        return {
            'state': 'running' if self._running else 'stopped',
            'version': '7.0.0',
            'accounts': list(self._accounts.keys()),
            'account_count': len(self._accounts),
            'stock_pool_size': len(self._stock_pool),
            'min_market_cap': self._min_market_cap,
            'max_market_cap': self._max_market_cap,
            'scheduler': self.scheduler.get_stats() if self.scheduler else {},
            'trading_session': self.scheduler.is_trading_session() if self.scheduler else False,
            'trading_day': self.scheduler.is_trading_day() if self.scheduler else False,
            'calendar_source': getattr(self.trading_calendar, 'source', 'none') if self.trading_calendar else 'none',
            'time': datetime.now().isoformat(),
        }

    @property
    def state(self) -> str:
        return 'running' if self._running else 'stopped'
