"""
猎场系统 V6.0 — 扫描器基类
所有信号扫描器的抽象基类
"""

import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class BaseScanner(ABC):
    """信号扫描器基类"""

    def __init__(self, name: str, db, config: dict = None):
        self.name = name
        self.db = db
        self.config = config or {}
        self._last_scan_time: Optional[datetime] = None
        self._scan_count = 0
        self._signal_count = 0

    @abstractmethod
    async def scan(self, ts_codes: List[str]) -> List[Dict]:
        """
        扫描股票列表，返回信号列表

        每个信号 Dict 格式:
        {
            'ts_code': '000001.SZ',
            'name': '平安银行',
            'signal_type': self.name,
            'score': 0-100,          # 信号强度
            'direction': 'buy',      # buy / sell / hold
            'reason': 'RSI超卖',     # 信号原因
            'details': {...},        # 详细信息
            'timestamp': '...',
        }
        """
        pass

    async def run(self, ts_codes: List[str]) -> List[Dict]:
        """执行扫描并记录统计"""
        self._last_scan_time = datetime.now()
        self._scan_count += 1

        try:
            signals = await self.scan(ts_codes)
            self._signal_count += len(signals)
            logger.debug(f"{self.name}: scanned {len(ts_codes)} stocks, found {len(signals)} signals")
            return signals
        except Exception as e:
            logger.error(f"{self.name} scan error: {e}", exc_info=True)
            return []

    def get_stats(self) -> Dict:
        return {
            "name": self.name,
            "last_scan": self._last_scan_time.isoformat() if self._last_scan_time else None,
            "scan_count": self._scan_count,
            "total_signals": self._signal_count,
        }
