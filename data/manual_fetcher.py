"""
猎场系统 V7.0 — 手动/自定义 HTTP 数据源
允许用户在设置面板手动填入自有 API 地址，接入自定义行情数据。

约定 REST 接口（返回 JSON，字段见下）：

  1) 股票列表:
     GET {base_url}/stocks
     → {"stocks": [{"symbol": "600519", "name": "贵州茅台", "industry": "", "market_cap": 0}]}

  2) 实时行情:
     GET {base_url}/realtime?symbols=600519,000001
     → {"quotes": [{"symbol": "600519", "name": "贵州茅台", "price": 1500.0,
                    "change_pct": 1.5, "open": 1490.0, "high": 1510.0, "low": 1480.0,
                    "volume": 100000, "amount": 150000000}]}

  3) 日K线:
     GET {base_url}/kline?symbol=600519&days=320
     → {"klines": [{"date": "2024-01-02", "open": 1500.0, "close": 1510.0,
                    "high": 1520.0, "low": 1490.0, "volume": 100000}]}

可选认证: 若配置了 Token，则通过 `Authorization: Bearer {token}` 头传递。
"""

import asyncio
import logging
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class ManualFetcher:
    """手动配置的自定义 HTTP 数据源"""

    def __init__(self, base_url: str, token: str = "", database=None):
        self.db = database
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self._available = bool(self.base_url)
        self._session = None
        logger.info(f"ManualFetcher initialized: {self.base_url or '(none)'}")

    @property
    def available(self) -> bool:
        return self._available

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        """600519.SH / sh600519 / 600519 → 600519"""
        s = (symbol or "").strip()
        s = s.replace(".SH", "").replace(".SZ", "").replace(".sh", "").replace(".sz", "")
        if s.startswith("sh") or s.startswith("sz"):
            s = s[2:]
        return s

    async def _get_session(self):
        if self._session is None:
            import aiohttp
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._session

    def _headers(self) -> Dict:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _get_json(self, url: str) -> Optional[dict]:
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self._headers()) as resp:
                    if resp.status != 200:
                        logger.debug(f"ManualFetcher: HTTP {resp.status} for {url}")
                        return None
                    return await resp.json()
        except Exception as e:
            logger.debug(f"ManualFetcher: request error for {url}: {e}")
            return None

    # ---- 日K线 ----

    async def sync_daily_kline(self, ts_codes: List[str] = None, days: int = 320) -> int:
        if not self._available:
            return 0
        if ts_codes is None:
            ts_codes = self.db.get_all_codes() if hasattr(self.db, 'get_all_codes') else []
        total = 0
        for ts_code in ts_codes:
            symbol = self.normalize_symbol(ts_code)
            data = await self._get_json(f"{self.base_url}/kline?symbol={symbol}&days={days}")
            if not data:
                continue
            klines = data.get("klines") or data.get("data") or []
            rows = []
            for row in klines:
                if not isinstance(row, dict):
                    continue
                try:
                    rows.append({
                        "ts_code": ts_code,
                        "trade_date": str(row.get("date", ""))[:10],
                        "open": float(row.get("open", 0)),
                        "close": float(row.get("close", 0)),
                        "high": float(row.get("high", 0)),
                        "low": float(row.get("low", 0)),
                        "volume": float(row.get("volume", 0)),
                        "amount": float(row.get("amount", 0)),
                        "turnover_rate": float(row.get("turnover_rate", 0)),
                        "volume_ratio": float(row.get("volume_ratio", 1.0)),
                        "rsi_14": 50.0,
                        "change_pct": float(row.get("change_pct", 0)),
                    })
                except (ValueError, TypeError):
                    continue
            if rows:
                total += self.db.upsert_kline(rows)
            await asyncio.sleep(0.01)
        logger.info(f"ManualFetcher: synced {total} kline rows for {len(ts_codes)} stocks")
        return total

    # ---- 实时行情 ----

    async def get_realtime_quotes(self, ts_codes: List[str]) -> Dict[str, Dict]:
        if not self._available:
            return {}
        symbols = ",".join(self.normalize_symbol(c) for c in ts_codes)
        data = await self._get_json(f"{self.base_url}/realtime?symbols={symbols}")
        if not data:
            return {}
        quotes = data.get("quotes") or data.get("data") or []
        result = {}
        for q in quotes:
            if not isinstance(q, dict):
                continue
            symbol = self.normalize_symbol(q.get("symbol", ""))
            if not symbol:
                continue
            result[symbol] = {
                "name": q.get("name", symbol),
                "price": float(q.get("price", 0) or 0),
                "open": float(q.get("open", 0) or 0),
                "high": float(q.get("high", 0) or 0),
                "low": float(q.get("low", 0) or 0),
                "volume": float(q.get("volume", 0) or 0),
                "amount": float(q.get("amount", 0) or 0),
                "change_pct": float(q.get("change_pct", 0) or 0),
                "change": float(q.get("change", 0) or 0),
                "turnover_rate": float(q.get("turnover_rate", 0) or 0),
                "volume_ratio": float(q.get("volume_ratio", 1.0) or 1.0),
                "pe": float(q.get("pe", 0) or 0),
                "market_cap": float(q.get("market_cap", 0) or 0),
                "time": datetime.now().isoformat(),
            }
        return result

    # ---- 股票列表 ----

    async def sync_stock_basic(self) -> int:
        if not self._available:
            return 0
        data = await self._get_json(f"{self.base_url}/stocks")
        if not data:
            return 0
        stocks_data = data.get("stocks") or data.get("data") or []
        stocks = []
        for row in stocks_data:
            if not isinstance(row, dict):
                continue
            symbol = self.normalize_symbol(row.get("symbol", ""))
            name = row.get("name", "")
            if not symbol or not name:
                continue
            stocks.append({
                "ts_code": symbol,
                "name": name,
                "industry": row.get("industry", ""),
                "market_cap": float(row.get("market_cap", 0) or 0),
                "list_date": str(row.get("list_date", "")),
                "is_st": 1 if "ST" in name else 0,
                "updated_at": datetime.now().isoformat(),
            })
        if stocks:
            self.db.upsert_stock_basic(stocks)
        logger.info(f"ManualFetcher: synced {len(stocks)} stock basics")
        return len(stocks)

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None
