"""
猎场系统 V6.0 — 统一数据接入网关
协调多个数据源，提供统一接口
"""

import logging
import os
from typing import Optional, List, Dict, Any
from datetime import datetime, date

logger = logging.getLogger(__name__)


class DataGateway:
    """
    统一数据网关
    - 主数据源: Tushare MCP
    - 备选数据源: 腾讯 HTTP / AkShare / 手动 API
    - 实时数据: Tushare MCP，失败时腾讯 HTTP/WebSocket
    - 新闻数据: 东财 NLP
    """

    def __init__(self, config: dict, database):
        self.config = config
        self.db = database
        self._tencent = None
        self._tushare = None
        self._akshare = None
        self._manual = None
        self._sina = None
        self._ws_client = None
        self._news_service = None
        self._initialized = False

        # 数据源配置
        ds = config.get("data_sources", {})
        primary_cfg = ds.get("primary", {})
        configured_token = primary_cfg.get("token", "")
        # Environment wins.  A literal ${TUSHARE_TOKEN} is a placeholder,
        # never a credential and must not be sent to a provider.
        self._tushare_token = os.getenv("TUSHARE_TOKEN", "").strip()
        if not self._tushare_token and configured_token and not str(configured_token).startswith("${"):
            self._tushare_token = str(configured_token).strip()
        self._tushare_mcp_url = (
            primary_cfg.get("mcp_url") or os.getenv("TUSHARE_MCP_URL", "")
        ).strip()
        self._use_primary = True
        self._primary_name = ds.get("primary", {}).get("name", "tencent")
        self._last_error = None

        # 手动/自定义数据源（由设置面板配置，持久化到 DB）
        manual_cfg = ds.get("manual", {})
        self._manual_url = manual_cfg.get("base_url", "")
        self._manual_token = manual_cfg.get("token", "")

    async def initialize(self):
        """初始化所有数据源"""
        # 腾讯免费行情（零依赖、零认证）
        try:
            from .tencent_fetcher import TencentFetcher
            self._tencent = TencentFetcher(self.db)
            logger.info("TencentFetcher loaded (free, no auth)")
        except ImportError:
            logger.debug("tencent_fetcher not available")

        # Tushare Pro (需要 token)
        try:
            from .tushare_fetcher import TushareFetcher
            self._tushare = TushareFetcher(self._tushare_token, self.db, self._tushare_mcp_url)
            logger.info("TushareFetcher loaded")
        except ImportError:
            logger.debug("tushare not installed")
            if self._primary_name == "tushare":
                self._use_primary = False

        # AkShare (免费备选)
        try:
            from .akshare_fetcher import AkShareFetcher
            self._akshare = AkShareFetcher(self.db)
            logger.info("AkShareFetcher loaded")
        except ImportError:
            logger.debug("akshare not installed")

        # 新浪全量股票列表（含总市值，用于 stock_basic 补全 + 小市值筛选）
        try:
            from .sina_fetcher import SinaFetcher
            self._sina = SinaFetcher(self.db)
            logger.info("SinaFetcher loaded (full stock list + market cap)")
        except Exception as e:
            logger.debug(f"sina_fetcher not available: {e}")

        # 手动/自定义 HTTP 数据源（有地址时启用）
        if self._manual_url:
            try:
                from .manual_fetcher import ManualFetcher
                self._manual = ManualFetcher(self._manual_url, self._manual_token, self.db)
                logger.info(f"ManualFetcher loaded ({self._manual_url})")
            except Exception as e:
                logger.debug(f"manual_fetcher not available: {e}")

        # WebSocket 实时行情
        try:
            from .websocket_realtime import WebSocketClient
            self._ws_client = WebSocketClient(self.db)
            logger.info("WebSocketClient loaded")
        except ImportError:
            logger.debug("websocket realtime not available")

        # 新闻 NLP
        try:
            from .news_nlp import NewsNLPService
            self._news_service = NewsNLPService(self.db)
            logger.info("NewsNLPService loaded")
        except ImportError:
            logger.debug("news NLP service not available")

        self._initialized = True
        logger.info(f"DataGateway initialized (primary={self._primary_name})")

    # ---- 数据源切换 ----

    def _source_priority(self) -> List[str]:
        """根据当前主数据源返回优先级列表（用于动态切换）"""
        sources = ['tencent', 'tushare', 'akshare', 'manual', 'sina']
        if self._primary_name not in sources:
            sources.append(self._primary_name)
        # 将主数据源排到最前，其余保持默认相对顺序
        return [self._primary_name] + [s for s in sources if s != self._primary_name]

    def _available_map(self) -> Dict[str, bool]:
        return {
            'tencent': bool(self._tencent and getattr(self._tencent, 'available', True)),
            'tushare': bool(self._tushare and getattr(self._tushare, 'available', False)),
            'akshare': bool(self._akshare and getattr(self._akshare, 'available', True)),
            'manual': bool(self._manual and self._manual.available),
            'sina': bool(self._sina and getattr(self._sina, 'available', True)),
        }

    def set_primary_source(self, name: str) -> Dict:
        """运行时切换主数据源，立即生效（下次数据同步按新优先级走）"""
        name = (name or '').lower()
        valid = {'tencent', 'tushare', 'akshare', 'manual', 'sina'}
        if name not in valid:
            return {'ok': False, 'error': f'无效数据源: {name}，可选 {sorted(valid)}'}

        self._primary_name = name
        self._use_primary = (name == 'tushare')  # tushare 需要 token 才可用

        available = self._available_map()
        logger.info(f"主数据源已切换为: {name} (可用={available.get(name, False)})")
        return {
            'ok': True,
            'primary_source': name,
            'available': available,
            'priority': self._source_priority(),
        }

    def set_manual_api(self, base_url: str, token: str = "") -> Dict:
        """配置手动/自定义 HTTP 数据源地址与 Token"""
        base_url = (base_url or "").strip().rstrip("/")
        self._manual_url = base_url
        self._manual_token = (token or "").strip()
        if base_url:
            from .manual_fetcher import ManualFetcher
            self._manual = ManualFetcher(base_url, self._manual_token, self.db)
            logger.info(f"手动数据源已配置: {base_url}")
        else:
            self._manual = None
        return {
            'ok': True,
            'manual_url': base_url,
            'manual_available': bool(self._manual and self._manual.available),
        }

    def set_tushare_token(self, token: str) -> Dict:
        """手动设置 Tushare Pro Token（热更新）"""
        token = (token or "").strip()
        self._tushare_token = token
        if self._tushare:
            self._tushare.set_token(token)
        logger.info("Tushare token 已更新" if token else "Tushare token 已清空")
        return {
            'ok': True,
            'tushare_available': bool(self._tushare and getattr(self._tushare, 'available', False)),
        }

    def get_source_status(self) -> Dict:
        return {
            'primary_source': self._primary_name,
            'priority': self._source_priority(),
            'available': self._available_map(),
            'tushare_mcp_url': self._tushare_mcp_url,
            'manual_url': self._manual_url,
        }

    # ---- 日K线数据 ----

    async def _sync_kline_from(self, source: str, ts_codes: List[str]) -> int:
        """从指定数据源同步日K线"""
        if source == 'tencent' and self._tencent and getattr(self._tencent, 'available', True):
            return await self._tencent.sync_daily_kline(ts_codes)
        if source == 'tushare' and self._tushare and getattr(self._tushare, 'available', False):
            return await self._tushare.sync_daily_kline(ts_codes)
        if source == 'akshare' and self._akshare and getattr(self._akshare, 'available', True):
            return await self._akshare.sync_daily_kline(ts_codes)
        if source == 'manual' and self._manual and self._manual.available:
            return await self._manual.sync_daily_kline(ts_codes)
        return 0

    async def sync_daily_kline(self, ts_codes: List[str] = None) -> int:
        """同步日K线增量数据 — 按当前数据源优先级依次尝试"""
        for source in self._source_priority():
            try:
                count = await self._sync_kline_from(source, ts_codes)
                if count and count > 0:
                    return count
            except Exception as e:
                logger.warning(f"{source} kline sync failed: {e}")
        return 0

    async def get_kline(self, ts_code: str, days: int = 60) -> List[Dict]:
        """获取个股K线数据"""
        return self.db.get_kline(ts_code, limit=days)

    # ---- 实时行情 ----

    async def get_realtime_quote(self, ts_codes: List[str]) -> Dict[str, Dict]:
        """获取实时行情 — Tushare MCP 优先，腾讯 HTTP/WebSocket 降级"""
        # 按当前数据源优先级依次尝试快照；这 keeps all normal reads on
        # Tushare once it is configured, while retaining the user's requested
        # HTTP fallback for capabilities the MCP account cannot access.
        for source in self._source_priority():
            fetcher = {'tencent': self._tencent, 'tushare': self._tushare,
                       'akshare': self._akshare, 'manual': self._manual,
                       'sina': self._sina}.get(source)
            if not fetcher or not getattr(fetcher, 'available', True):
                continue
            if source == 'tushare' and not getattr(fetcher, 'available', False):
                continue
            try:
                result = await fetcher.get_realtime_quotes(ts_codes)
                if result and any(q.get('price', 0) > 0 for q in result.values()):
                    return result
            except Exception as e:
                logger.debug(f"{source} realtime error: {e}")

        # WebSocket remains a last-resort low-latency fallback.
        if self._ws_client:
            try:
                quotes = await self._ws_client.get_quotes(ts_codes)
                if quotes and any(q.get('price', 0) > 0 for q in quotes.values()):
                    return quotes
            except Exception as e:
                logger.debug(f"websocket realtime error: {e}")

        return {}

    async def get_daily_basic(self, ts_codes: List[str] = None,
                              trade_date: str = None) -> List[Dict]:
        """获取 Tushare 每日指标（市值、换手率、量比、估值）。

        该接口只把 Tushare 的结果透传给决策层，不改变原有验证规则。
        MCP 账户没有 ``daily_basic`` 权限时返回空列表，由调用方使用已
        缓存的 K 线或实时行情继续运行。
        """
        if not self._tushare or not getattr(self._tushare, 'available', False):
            return []
        try:
            rows = await self._tushare.get_daily_basic(trade_date=trade_date)
            if rows is None:
                return []
            if hasattr(rows, 'to_dict'):
                rows = rows.to_dict('records')
            rows = [dict(row) for row in rows if isinstance(row, dict)]
            if ts_codes:
                wanted = {str(code) for code in ts_codes}
                rows = [row for row in rows if str(row.get('ts_code', '')) in wanted]
            return rows
        except Exception as exc:
            logger.debug("Tushare daily_basic unavailable: %s", exc)
            return []

    async def get_index_quotes(self) -> Dict[str, Dict]:
        """获取主要指数实时行情（上证/深成/沪深300/创业板），供行情分析报告使用"""
        for source in self._source_priority():
            fetcher = {
                'tushare': self._tushare,
                'tencent': self._tencent,
            }.get(source)
            if not fetcher or not hasattr(fetcher, 'get_index_quotes'):
                continue
            if source == 'tushare' and not getattr(fetcher, 'available', False):
                continue
            try:
                result = await fetcher.get_index_quotes()
                if result:
                    return result
            except Exception as e:
                logger.debug(f"{source} index quotes error: {e}")
        return {}

    async def start_realtime_streaming(self, ts_codes: List[str]):
        """启动 WebSocket 实时推送"""
        if self._ws_client:
            await self._ws_client.subscribe(ts_codes)
            logger.info(f"Realtime streaming started for {len(ts_codes)} stocks")

    async def stop_realtime_streaming(self):
        if self._ws_client:
            await self._ws_client.stop()

    async def close(self):
        """Close provider sessions held by the gateway."""
        await self.stop_realtime_streaming()
        if self._tushare and hasattr(self._tushare, 'close'):
            await self._tushare.close()
        if self._tencent and hasattr(self._tencent, 'close'):
            await self._tencent.close()

    # ---- 股票基本信息 ----

    async def sync_stock_basic(self):
        """同步股票基本信息 — Tushare MCP 优先，其他源依次降级"""
        for source in self._source_priority():
            try:
                if source == 'tencent' and self._tencent and getattr(self._tencent, 'available', True):
                    count = await self._tencent.sync_stock_basic()
                elif source == 'akshare' and self._akshare and getattr(self._akshare, 'available', True):
                    count = await self._akshare.sync_stock_basic()
                elif source == 'tushare' and self._tushare and getattr(self._tushare, 'available', False):
                    count = await self._tushare.sync_stock_basic()
                elif source == 'manual' and self._manual and self._manual.available:
                    count = await self._manual.sync_stock_basic()
                elif source == 'sina' and self._sina and getattr(self._sina, 'available', True):
                    count = await self._sina.sync_stock_basic()
                else:
                    continue
                if count > 0:
                    return count
            except Exception as e:
                logger.warning(f"{source} stock list sync failed: {e}")

        return 0

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
            "primary_source": self._primary_name,
            "priority": self._source_priority(),
            "db_stats": self.db.get_stats(),
        }
