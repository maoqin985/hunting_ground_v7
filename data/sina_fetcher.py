"""
猎场系统 V7.0 — 新浪财经全量股票列表数据源
用于补全 stock_basic（股票名称 + 总市值），支撑小市值选股池。

腾讯 queryAllStock 接口已下线（返回 302/undefined method），
改用新浪 Market_Center 接口，免费零认证，一次翻页拉取全部 A 股。

接口：
  GET http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData
      ?page=N&num=100&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page
  返回 JSON 数组，每项含 code/name/mktcap(万元)/nmc(万元) 等字段。

市值单位统一换算为「亿元」，与 config 里 min_market_cap/max_market_cap 一致。
"""

import json
import logging
from typing import List, Dict
from datetime import datetime

logger = logging.getLogger(__name__)


class SinaFetcher:
    """新浪财经全量 A 股列表（含总市值），用于 stock_basic 补全与小市值筛选"""

    _BASE = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    _COUNT = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"

    def __init__(self, database=None):
        self.db = database
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def _headers(self) -> Dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "http://finance.sina.com.cn/",
        }

    async def _get_json(self, url: str):
        import aiohttp
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self._headers()) as resp:
                    if resp.status != 200:
                        return None
                    text = await resp.text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.debug(f"SinaFetcher request error: {e}")
            return None
        if not text or not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    async def sync_stock_basic(self) -> int:
        """同步全部沪深 A 股列表（含总市值），写入 stock_basic"""
        if not self._available:
            return 0

        stocks: List[Dict] = []
        page = 1
        num = 100  # 新浪单页上限 100
        max_pages = 80  # 上限 8000 只，足够覆盖全部 A 股

        while page <= max_pages:
            url = f"{self._BASE}?page={page}&num={num}&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page"
            rows = await self._get_json(url)
            if not rows:
                break

            for r in rows:
                if not isinstance(r, dict):
                    continue
                symbol = str(r.get("symbol", "")).strip().lower()
                code = str(r.get("code", "")).strip()
                name = str(r.get("name", "")).strip()
                # 只保留沪深 A 股（跳过北交所 bj / 非 6 位数字代码）
                if symbol.startswith("bj"):
                    continue
                if not code.isdigit() or len(code) != 6 or not name:
                    continue
                try:
                    mktcap_wan = float(r.get("mktcap", 0) or 0)
                except (ValueError, TypeError):
                    mktcap_wan = 0.0
                stocks.append({
                    "ts_code": code,
                    "name": name,
                    "industry": "",
                    "market_cap": round(mktcap_wan / 10000.0, 2),  # 万元 → 亿元
                    "list_date": "",
                    "is_st": 1 if "ST" in name.upper() else 0,
                    "updated_at": datetime.now().isoformat(),
                })

            if len(rows) < num:
                break
            page += 1

        if stocks:
            self.db.upsert_stock_basic(stocks)
            logger.info(f"SinaFetcher: synced {len(stocks)} A-share stock basics (with market_cap)")
        return len(stocks)
