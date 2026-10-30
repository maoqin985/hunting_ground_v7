"""
猎场系统 V7.0 — 腾讯免费行情数据源
使用腾讯财经 HTTP API，无需注册/Token，免费获取 A 股行情

接口列表：
  实时行情: http://qt.gtimg.cn/q=sh600519,sz000001  (已有 websocket_realtime.py)
  日K线:    http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz000001,day,,,320,qfq
  分钟K线:  http://web.ifzq.gtimg.cn/appstock/app/kline/mkline?param=sz000001,m5,,320
  分时线:   http://web.ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code=sz000001

字段格式 (日K线 qfqday):
  [日期, 开盘, 收盘, 最高, 最低, 成交量(手)]  — 注意顺序与常见 OHLCV 不同
"""

import asyncio
import json
import logging
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# 腾讯 K 线日期格式: 2024-01-02
TENCENT_DATE_FMT = "%Y-%m-%d"


class TencentFetcher:
    """腾讯免费行情获取器（零依赖、零认证）"""

    # ================================================================
    # 代码格式转换
    # ================================================================

    @staticmethod
    def to_tencent_code(ts_code: str) -> str:
        """600000.SH / 000001.SZ / sh600000 / sz000001 → sh600000 / sz000001"""
        code = ts_code.replace(".SH", "").replace(".SZ", "").replace(".sh", "").replace(".sz", "")
        # Strip already-present exchange prefix
        if code.startswith("sh") or code.startswith("sz"):
            code = code[2:]
        if code.startswith("6"):
            return f"sh{code}"
        elif code.startswith(("0", "3")):
            return f"sz{code}"
        return f"sz{code}"

    @staticmethod
    def from_tencent_code(tc: str) -> str:
        """sh600000 / sz000001 → 600000 / 000001"""
        return tc[2:] if len(tc) > 2 else tc

    # ================================================================
    # 初始化
    # ================================================================

    def __init__(self, database):
        self.db = database
        self._available = True  # 无需第三方库，纯 HTTP
        self._session = None
        logger.info("TencentFetcher initialized (free, no auth)")

    @property
    def available(self) -> bool:
        return self._available

    async def _get_session(self):
        """懒加载 aiohttp session"""
        if self._session is None:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    # ================================================================
    # 日K线 — 腾讯复权K线接口
    # ================================================================

    async def sync_daily_kline(self, ts_codes: List[str] = None, days: int = 320) -> int:
        """
        从腾讯接口同步日K线（前复权）
        API: web.ifzq.gtimg.cn/appstock/app/fqkline/get
        """
        if ts_codes is None:
            ts_codes = self.db.get_all_codes() if hasattr(self.db, 'get_all_codes') else []

        if not ts_codes:
            logger.warning("TencentFetcher: no ts_codes to sync")
            return 0

        import aiohttp
        total = 0

        for ts_code in ts_codes:
            try:
                tc = self.to_tencent_code(ts_code)
                url = (
                    f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                    f"?param={tc},day,,,{days},qfq"
                )

                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        "Referer": "https://gu.qq.com/",
                    }) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text()

                data = json.loads(text)
                if data.get("code") != 0:
                    continue

                stock_data = data.get("data", {}).get(tc, {})
                klines = stock_data.get("qfqday") or stock_data.get("day") or []

                if not klines:
                    logger.debug(f"TencentFetcher: no kline for {ts_code}")
                    continue

                rows = []
                for row in klines:
                    if len(row) < 6:
                        continue
                    try:
                        # 腾讯格式: [日期, 开盘, 收盘, 最高, 最低, 成交量]
                        rows.append({
                            "ts_code": ts_code,
                            "trade_date": str(row[0])[:10],
                            "open": float(row[1]),
                            "close": float(row[2]),
                            "high": float(row[3]),
                            "low": float(row[4]),
                            "volume": float(row[5]),
                            "amount": float(row[6]) if len(row) > 6 and row[6] else 0,
                            "turnover_rate": 0.0,
                            "volume_ratio": 1.0,
                            "rsi_14": 50.0,
                            "change_pct": 0.0,
                        })
                    except (ValueError, IndexError) as e:
                        logger.debug(f"Tencent kline parse error for {ts_code}: {e}")
                        continue

                if rows:
                    count = self.db.upsert_kline(rows)
                    total += count
                    logger.debug(f"TencentFetcher: {ts_code} → {count} rows")

            except json.JSONDecodeError:
                logger.debug(f"TencentFetcher: invalid JSON for {ts_code}")
                continue
            except Exception as e:
                logger.debug(f"TencentFetcher: kline error for {ts_code}: {e}")
                continue

            # 频率控制：腾讯免费接口不需要严格限速，但加个小延迟避免被封
            await asyncio.sleep(0.05)

        logger.info(f"TencentFetcher: synced {total} kline rows for {len(ts_codes)} stocks")
        return total

    # ================================================================
    # 实时行情 — 腾讯行情快照
    # ================================================================

    async def get_realtime_quotes(self, ts_codes: List[str]) -> Dict[str, Dict]:
        """
        批量获取实时行情快照
        API: qt.gtimg.cn/q=...
        """
        if not ts_codes:
            return {}

        import aiohttp

        # 转换代码格式
        tcodes = [self.to_tencent_code(c) for c in ts_codes]

        result = {}
        batch_size = 50

        for i in range(0, len(tcodes), batch_size):
            batch = tcodes[i:i + batch_size]
            url = f"http://qt.gtimg.cn/q={','.join(batch)}"

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        "Referer": "https://gu.qq.com/",
                    }) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text(encoding="gbk")

                # 解析 ~ 分隔的数据
                for line in text.strip().split("\n"):
                    if '="' not in line:
                        continue
                    try:
                        raw_code = line.split('="')[0].split("_")[-1]
                        data_str = line.split('="')[1].strip('";\n')
                        fields = data_str.split("~")

                        if len(fields) < 40:
                            continue

                        ts_code = self.from_tencent_code(raw_code)
                        result[ts_code] = {
                            "name": fields[1],
                            "price": float(fields[3]) if fields[3] else 0,
                            "open": float(fields[5]) if fields[5] else 0,
                            "high": float(fields[33]) if len(fields) > 33 and fields[33] else 0,
                            "low": float(fields[34]) if len(fields) > 34 and fields[34] else 0,
                            "volume": float(fields[6]) if fields[6] else 0,
                            "amount": float(fields[37]) if len(fields) > 37 and fields[37] else 0,
                            "change_pct": float(fields[32]) if len(fields) > 32 and fields[32] else 0,
                            "change": float(fields[31]) if len(fields) > 31 and fields[31] else 0,
                            "turnover_rate": float(fields[38]) if len(fields) > 38 and fields[38] else 0,
                            "volume_ratio": float(fields[49]) if len(fields) > 49 and fields[49] else 1.0,
                            "bid1": float(fields[9]) if fields[9] else 0,
                            "ask1": float(fields[19]) if fields[19] else 0,
                            "pe": float(fields[39]) if len(fields) > 39 and fields[39] else 0,
                            "market_cap": float(fields[45]) if len(fields) > 45 and fields[45] else 0,
                            "time": datetime.now().isoformat(),
                        }

                    except Exception:
                        continue

            except Exception as e:
                logger.debug(f"TencentFetcher: realtime error for batch {i}: {e}")
                continue

        return result

    # ================================================================
    # 指数行情 — 大盘涨跌（行情分析报告用）
    # ================================================================

    async def get_index_quotes(self) -> Dict[str, Dict]:
        """获取主要指数实时行情：上证指数/深证成指/沪深300/创业板指。

        返回 {名称: {price, change, change_pct, ...}}，失败时返回空 dict。
        """
        indices = {
            "sh000001": "上证指数",
            "sz399001": "深证成指",
            "sh000300": "沪深300",
            "sz399006": "创业板指",
        }
        url = f"http://qt.gtimg.cn/q={','.join(indices.keys())}"
        import aiohttp

        result: Dict[str, Dict] = {}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://gu.qq.com/",
                }) as resp:
                    if resp.status != 200:
                        return result
                    text = await resp.text(encoding="gbk")

            for line in text.strip().split("\n"):
                if '="' not in line:
                    continue
                try:
                    raw_code = line.split('="')[0].split("_")[-1]
                    fields = line.split('="')[1].strip('";\n').split("~")
                    if len(fields) < 33:
                        continue
                    name = indices.get(raw_code, raw_code)
                    result[name] = {
                        "code": raw_code,
                        "name": name,
                        "price": float(fields[3]) if fields[3] else 0,
                        "change": float(fields[31]) if len(fields) > 31 and fields[31] else 0,
                        "change_pct": float(fields[32]) if len(fields) > 32 and fields[32] else 0,
                    }
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"TencentFetcher: index quotes error: {e}")

        return result

    # ================================================================
    # 股票列表 — 腾讯全量股票
    # ================================================================

    async def sync_stock_basic(self) -> int:
        """
        同步A股股票基本信息
        API: web.ifzq.gtimg.cn/appstock/app/stockinfo/queryAllStock
        注：此接口可能不稳定，失败时建议用 AkShare fallback
        """
        import aiohttp

        stocks = []
        markets = [
            ("sh", "m:t:6"),   # 上海主板
            ("sz", "m:t:0"),   # 深圳主板
            ("sz", "m:t:3"),   # 创业板
        ]

        for market, board in markets:
            try:
                url = (
                    f"http://web.ifzq.gtimg.cn/appstock/app/stockinfo/queryAllStock"
                    f"?market={market}&board={board}&_var=stocklist"
                )

                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://gu.qq.com/",
                    }) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text(encoding="gbk")

                # 响应格式: var stocklist={...}
                json_str = text
                if "=" in text:
                    json_str = text.split("=", 1)[1].rstrip(";").strip()

                data = json.loads(json_str)
                rows = data.get("data", []) if isinstance(data, dict) else []

                for row in rows:
                    if isinstance(row, dict):
                        code = row.get("code", "")
                        name = row.get("name", "")
                    elif isinstance(row, list) and len(row) >= 2:
                        code, name = str(row[0]), str(row[1])
                    else:
                        continue

                    if not code or not name:
                        continue

                    ts_code = f"{code}.{market.upper()}" if "." not in code else code
                    stocks.append({
                        "ts_code": ts_code.replace(".SH", "").replace(".SZ", ""),
                        "name": name,
                        "industry": "",
                        "market_cap": 0,
                        "list_date": "",
                        "is_st": 1 if "ST" in name else 0,
                        "updated_at": datetime.now().isoformat(),
                    })

            except Exception as e:
                logger.debug(f"TencentFetcher: stock list error for {market}/{board}: {e}")
                continue

        if stocks:
            self.db.upsert_stock_basic(stocks)
            logger.info(f"TencentFetcher: synced {len(stocks)} stock basics")

        return len(stocks)

    # ================================================================
    # 分钟K线 (备用)
    # ================================================================

    async def get_minute_kline(self, ts_code: str, freq: str = "m5", count: int = 240) -> List[Dict]:
        """
        获取分钟K线
        freq: m1, m5, m15, m30, m60
        API: web.ifzq.gtimg.cn/appstock/app/kline/mkline
        """
        tc = self.to_tencent_code(ts_code)
        url = f"http://web.ifzq.gtimg.cn/appstock/app/kline/mkline?param={tc},{freq},,{count}"

        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://gu.qq.com/",
                }) as resp:
                    if resp.status != 200:
                        return []
                    text = await resp.text()

            data = json.loads(text)
            if data.get("code") != 0:
                return []

            klines = data.get("data", {}).get(tc, {}).get(freq, [])
            result = []
            for row in klines:
                if len(row) < 6:
                    continue
                result.append({
                    "ts_code": ts_code,
                    "time": str(row[0]),
                    "open": float(row[1]),
                    "close": float(row[2]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "volume": float(row[5]),
                })

            return result

        except Exception as e:
            logger.debug(f"TencentFetcher: minute kline error for {ts_code}: {e}")
            return []

    # ================================================================
    # 清理
    # ================================================================

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None
            logger.debug("TencentFetcher session closed")
