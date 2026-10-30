"""
猎场系统 V7.0 — A股交易日历（API 直连）

优先从新浪财经交易日历接口拉取全量交易日（1990-12-19 至今，含未来已公布休市安排），
纯 Python 解码，零额外依赖（不用 akshare / py_mini_racer）。API 不可用时回退到本地
硬编码节假日（周末 + 法定节假日休市）。

接口：
  GET https://finance.sina.com.cn/realstock/company/klc_td_sh.txt
  返回 `var datelist="<base64 压缩编码>";`，解码后为升序交易日列表（YYYY-MM-DD）。

解码算法为新浪自研的位流压缩格式（base64 字符 → 6bit 位流，按位还原日期区间，
周末由 S() 跳过，节假日以游程长度编码）。算法移植自 akshare 的 hk_js_decode。

A股休市规则：周末 + 法定节假日一律休市，不设调休补班（与银行/工作日安排不同，
交易所周末从不开放，法定节假日也不存在调休交易日）。

用法：
    from data.trading_calendar import TradingCalendar
    cal = TradingCalendar()
    cal.is_trading_day(date(2026, 10, 1))   # False（国庆休市）
    cal.is_trading_day(date(2026, 10, 8))   # True
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import FrozenSet, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# 新浪交易日历接口
SINA_CALENDAR_URL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"

# 纪元偏移：新浪日历第 0 天 = 1990-12-19（上交所开市日），即 1970-01-01 起第 7657 天
_EPOCH_OFFSET_DAYS = 7657

# ---------------------------------------------------------------------------
# 兜底：2026 年交易所休市日（仅列落在工作日的日期；周末由 weekday 判断兜底）
# 数据来源：沪深北交易所 2025-12-22 发布的《2026年部分节假日休市安排》
# ---------------------------------------------------------------------------
HOLIDAYS_2026: Set[str] = {
    # 元旦：1/1(四)~1/3(六)，工作日 1/1、1/2
    "2026-01-01", "2026-01-02",
    # 春节：2/15(日)~2/23(一)，工作日 2/16~2/20、2/23
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
    # 清明节：4/4(六)~4/6(一)，工作日 4/6
    "2026-04-06",
    # 劳动节：5/1(五)~5/5(二)，工作日 5/1、5/4、5/5
    "2026-05-01", "2026-05-04", "2026-05-05",
    # 端午节：6/19(五)~6/21(日)，工作日 6/19
    "2026-06-19",
    # 中秋节：9/25(五)~9/27(日)，工作日 9/25
    "2026-09-25",
    # 国庆节：10/1(四)~10/7(三)，工作日 10/1、10/2、10/5~10/7
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
}

HOLIDAYS_BY_YEAR = {2026: HOLIDAYS_2026}

_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


class _BitReader:
    """新浪日历位流读取器（6bit 字符 → 任意位宽字段，低字节序）"""

    def __init__(self, data: List[int]):
        self.data = data
        self.n = len(data)
        self.e = 0  # 字符下标
        self.o = 0  # 字符内位偏移 (0..5)

    def read_bit(self) -> int:
        if self.e >= self.n:
            return 0
        bit = (self.data[self.e] >> self.o) & 1
        self.o += 1
        if self.o >= 6:
            self.o = 0
            self.e += 1
        return bit

    def read_bits(self, count: int) -> int:
        """读取 count 位（字段内部先读到的位为低位，对齐 JS w() 语义）"""
        val = 0
        rem = count
        while rem > 0:
            if self.e >= self.n:
                break
            h = 6 - self.o
            if rem < h:
                h = rem
            chunk = (self.data[self.e] >> self.o) & ((1 << h) - 1)
            val |= chunk << (count - rem)
            self.o += h
            if self.o >= 6:
                self.o -= 6
                self.e += 1
            rem -= h
        return val

    def read_varint(self) -> int:
        """有符号变长整数（JS N()）"""
        sign = self.read_bit()
        k = 1
        while True:
            if not self.read_bit():
                return k * (2 * sign - 1)
            k += 1


def _advance_trading_day(d: int, steps: int) -> int:
    """从日偏移 d 前进 steps 个交易日（跳过周末），返回新偏移。对应 JS S()"""
    for _ in range(steps):
        d += 1
        wd = d % 7
        if wd in (3, 4):  # 周六(3)/周日(4) → 跳到周一
            d += 5 - wd
    return d


def decode_sina_trade_dates(datelist: str) -> List[str]:
    """解码新浪 klc_td_sh.txt 的 datelist 为升序交易日列表（YYYY-MM-DD）"""
    idx = [_BASE64.index(ch) for ch in datelist if ch in _BASE64]
    if not idx:
        return []

    br = _BitReader(idx)
    mode = br.read_bits(12)
    key = br.read_bits(6)
    salt = 63 ^ key  # JS: s

    if mode != 139 or salt > 1:  # 139 = R 模式（纯日期区间）；其余为 K 线等其它压缩模式
        logger.debug(f"trading_calendar: unsupported mode={mode} salt={salt}")
        return []

    # 读区间的起止日偏移
    d = br.read_bits(18) - 1
    end = br.read_bits(18)

    out: List[str] = []
    gap_level = 0
    pending = -1

    while d < end:
        d = _advance_trading_day(d, 1)
        dt = datetime(1970, 1, 1) + timedelta(days=_EPOCH_OFFSET_DAYS + d)

        if pending <= 0:
            if br.read_bit():
                gap_level += br.read_varint()
            pending = br.read_bits(3 * gap_level) + 1
            if not out:
                out.append(dt.strftime("%Y-%m-%d"))
                pending -= 1
        else:
            out.append(dt.strftime("%Y-%m-%d"))
        pending -= 1

    return out


class TradingCalendar:
    """A股交易日历：优先 API 全量交易日，回退到本地节假日兜底"""

    def __init__(
        self,
        holidays: Optional[Iterable[str]] = None,
        years: Optional[Iterable[int]] = None,
    ):
        self._holidays: Set[str] = set()
        for y in (years if years is not None else HOLIDAYS_BY_YEAR.keys()):
            self._holidays.update(HOLIDAYS_BY_YEAR.get(y, set()))
        if holidays:
            self._holidays.update(holidays)

        self._trading_days: Optional[Set[str]] = None  # API 全量交易日
        self._max_trading_day: Optional[str] = None

    @property
    def source(self) -> str:
        return "api" if self._trading_days else "fallback"

    def set_trading_days(self, dates: Iterable[str]) -> int:
        """注入 API 拉取的全量交易日，返回数量"""
        ds = set(dates or [])
        self._trading_days = ds
        self._max_trading_day = max(ds) if ds else None
        return len(ds)

    def is_holiday(self, d: date) -> bool:
        return not self.is_trading_day(d)

    def is_trading_day(self, d: date) -> bool:
        s = d.strftime("%Y-%m-%d")
        # API 日历覆盖范围内 → 以 API 为准（含未来已公布休市安排）
        if self._trading_days is not None:
            if self._max_trading_day is None or s <= self._max_trading_day:
                return s in self._trading_days
        # 超出 API 覆盖范围或 API 不可用 → 周末 + 本地节假日兜底
        if d.weekday() >= 5:
            return False
        return s not in self._holidays

    @property
    def trading_days(self) -> FrozenSet[str]:
        return frozenset(self._trading_days or set())

    @property
    def holidays(self) -> FrozenSet[str]:
        return frozenset(self._holidays)


class SinaTradeCalendarFetcher:
    """新浪财经交易日历接口（aiohttp，免费零认证）"""

    def __init__(self, url: str = SINA_CALENDAR_URL):
        self.url = url

    async def fetch(self) -> List[str]:
        import aiohttp

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://finance.sina.com.cn/",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.debug(f"SinaTradeCalendarFetcher: HTTP {resp.status}")
                        return []
                    text = await resp.text(encoding="gbk", errors="replace")
        except Exception as e:
            logger.debug(f"SinaTradeCalendarFetcher: request error: {e}")
            return []

        if "=" not in text:
            return []
        datelist = text.split("=", 1)[1].split(";", 1)[0].strip().strip('"')
        try:
            return decode_sina_trade_dates(datelist)
        except Exception as e:
            logger.warning(f"SinaTradeCalendarFetcher: decode error: {e}")
            return []


# 默认实例：未注入 API 数据时仅周末 + 2026 节假日兜底
DEFAULT_CALENDAR = TradingCalendar()
