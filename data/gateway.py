"""
猎场系统 V6.0 — 统一数据接入网关
协调多个数据源，提供统一接口
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, date

logger = logging.getLogger(__name__)


class DataGateway:
    """
    统一数据网关
    - 主数据源: Tushare Pro
    - 备选数据源: AkShare
    - 实时数据: 腾讯 WebSocket
    - 新闻数据: 东财 NLP
    """

    def __init__(self, config: dict, database):
        self.config = config
        self.db = database
        self._tushare = None
        self._akshare = None
        self._ws_client = None
        self._news_service = None
        self._initialized = False

        # 数据源配置
        ds = config.get("data_sources", {})
        self._tushare_token = ds.get("primary", {}).get("token", "")
        self._use_primary = True
        self._last_error = None

    async def initialize(self):
        """初始化所有数据源"""
        # Lazy import to avoid hard dependency if one source is unavailable
        try:
            from .tushare_fetcher import TushareFetcher
            self._tushare = TushareFetcher(self._tushare_token, self.db)
            logger.info("TushareFetcher loaded")
        except ImportError:
            logger.warning("tushare not installed, using AkShare as primary")
            self._use_primary = False

        try:
            from .akshare_fetcher import AkShareFetcher
            self._akshare = AkShareFetcher(self.db)
            logger.info("AkShareFetcher loaded")
        except ImportError:
            logger.warning("akshare not installed")

        try:
            from .websocket_realtime import WebSocketClient
            self._ws_client = WebSocketClient(self.db)
            logger.info("WebSocketClient loaded")
        except ImportError:
            logger.warning("websocket realtime not available")

        try:
            from .news_nlp import NewsNLPService
            self._news_service = NewsNLPService(self.db)
            logger.info("NewsNLPService loaded")
        except ImportError:
            logger.warning("news NLP service not available")

        self._initialized = True
        logger.info("DataGateway initialized")

    # ---- 日K线数据 ----

    async def sync_daily_kline(self, ts_codes: List[str] = None) -> int:
        """同步日K线增量数据"""
        try:
            if self._use_primary and self._tushare:
                return await self._tushare.sync_daily_kline(ts_codes)
            elif self._akshare:
                return await self._akshare.sync_daily_kline(ts_codes)
        except Exception as e:
            logger.warning(f"Primary source failed: {e}, trying fallback")
            if self._akshare:
                return await self._akshare.sync_daily_kline(ts_codes)
        return 0

    async def get_kline(self, ts_code: str, days: int = 60) -> List[Dict]:
        """获取个股K线数据"""
        return self.db.get_kline(ts_code, limit=days)

    # ---- 实时行情 ----

    async def get_realtime_quote(self, ts_codes: List[str]) -> Dict[str, Dict]:
        """获取实时行情"""
        if self._ws_client:
            return await self._ws_client.get_quotes(ts_codes)
        # Fallback: REST API
        if self._akshare:
            return await self._akshare.get_realtime_quotes(ts_codes)
        return {}

    async def start_realtime_streaming(self, ts_codes: List[str]):
        """启动 WebSocket 实时推送"""
        if self._ws_client:
            await self._ws_client.subscribe(ts_codes)
            logger.info(f"Realtime streaming started for {len(ts_codes)} stocks")

    async def stop_realtime_streaming(self):
        if self._ws_client:
            await self._ws_client.stop()

    # ---- 股票基本信息 ----

    async def sync_stock_basic(self):
        """同步股票基本信息"""
        try:
            if self._use_primary and self._tushare:
                return await self._tushare.sync_stock_basic()
            elif self._akshare:
                return await self._akshare.sync_stock_basic()
        except Exception as e:
            logger.warning(f"Stock basic sync failed: {e}")
            if self._akshare:
                return await self._akshare.sync_stock_basic()

    # ---- 新闻 NLP ----

    async def fetch_news(self, ts_codes: List[str] = None) -> List[Dict]:
        """抓取并分析新闻"""
        if self._news_service:
            return await self._news_service.fetch_and_analyze(ts_codes)
        return []

    async def get_negative_stocks(self) -> List[str]:
        """获取近期负面新闻股票列表"""
        return self.db.get_negative_stocks()

    # ---- 市值筛选 ----

    async def get_small_cap_stocks(self, min_cap: float = 20, max_cap: float = 100) -> List[Dict]:
        """获取小市值股票列表"""
        return self.db.get_stocks_by_market_cap(min_cap, max_cap)

    # ---- 状态查询 ----

    def is_initialized(self) -> bool:
        return self._initialized

    def get_stats(self) -> Dict:
        return {
            "initialized": self._initialized,
            "primary_source": "tushare" if self._use_primary else "akshare",
            "db_stats": self.db.get_stats(),
        }
