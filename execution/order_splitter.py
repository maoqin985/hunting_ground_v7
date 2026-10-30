"""
猎场系统 V7.0 — 动态拆单器
大单自动拆分为子单，支持 TWAP/VWAP/POV/Iceberg 四种算法
"""

import logging
import math
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from .slippage import SlippageEstimator

logger = logging.getLogger(__name__)


class OrderSplitter:
    """
    智能拆单引擎

    - TWAP: 时间加权平均价格 — 按时间均匀拆分
    - VWAP: 成交量加权平均价格 — 按历史成交量分布拆分
    - POV: 参与率算法 — 按目标参与率执行
    - Iceberg: 冰山算法 — 每次只显露小额订单
    """

    # 最小子单量 (A股100股=1手)
    MIN_SLICE_SHARES = 100
    # 默认拆分时间窗口(分钟)
    DEFAULT_WINDOW_MINUTES = 30

    def __init__(self):
        self.slippage = SlippageEstimator()

    def split(self, ts_code: str, total_shares: int, price: float,
              algo: str = 'TWAP', daily_volume: int = 0,
              session_duration_minutes: int = 240,
              participation_rate: float = 0.10,
              max_slices: int = 50) -> Optional[Dict]:
        """
        拆单

        Args:
            ts_code: 股票代码
            total_shares: 总股数
            price: 参考价格
            algo: 'TWAP' | 'VWAP' | 'POV' | 'Iceberg'
            daily_volume: 日均成交量(股)
            session_duration_minutes: 交易时段长度(分钟)
            participation_rate: 目标参与率 (POV模式)
            max_slices: 最大拆单数

        Returns:
            {
                'algo': str,
                'total_shares': int,
                'slices': int,               # 子单数
                'slices_planned': int,
                'schedule': [
                    {'seq': 1, 'shares': 100, 'target_pct': 0.1, 'delay_min': 0,},
                    ...
                ],
                'avg_price': float,          # 预估均价
                'slippage_bps': float,       # 预估滑点
            }
        """
        if total_shares < self.MIN_SLICE_SHARES:
            return {
                'algo': algo, 'total_shares': total_shares,
                'slices': 1, 'slices_planned': 1,
                'schedule': [{'seq': 1, 'shares': total_shares,
                              'target_pct': 1.0, 'delay_min': 0}],
                'avg_price': price, 'slippage_bps': 0,
            }

        if algo == 'TWAP':
            schedule = self._twap_split(total_shares, session_duration_minutes, max_slices)
        elif algo == 'VWAP':
            schedule = self._vwap_split(total_shares, session_duration_minutes, max_slices)
        elif algo == 'POV':
            schedule = self._pov_split(total_shares, daily_volume, participation_rate, max_slices)
        elif algo == 'Iceberg':
            schedule = self._iceberg_split(total_shares, max_slices)
        else:
            logger.warning(f"Unknown algo {algo}, falling back to TWAP")
            schedule = self._twap_split(total_shares, session_duration_minutes, max_slices)

        # 预估滑点
        total_amount = total_shares * price
        daily_amount = daily_volume * price if daily_volume > 0 else None
        slip = self.slippage.estimate(
            ts_code, price, total_amount, daily_amount,
            urgency='low' if algo in ('TWAP', 'VWAP') else 'normal'
        )

        return {
            'algo': algo,
            'total_shares': total_shares,
            'slices': len(schedule),
            'slices_planned': len(schedule),
            'schedule': schedule,
            'avg_price': round(price * (1 + slip['slippage_pct']), 2),
            'slippage_bps': slip['slippage_bps'],
        }

    def _twap_split(self, total_shares: int, duration_min: int,
                     max_slices: int) -> List[Dict]:
        """时间加权均匀拆分"""
        n = min(duration_min, max_slices)
        base = total_shares // n // self.MIN_SLICE_SHARES * self.MIN_SLICE_SHARES
        schedule = []
        remaining = total_shares
        for i in range(n):
            if i == n - 1:
                shares = remaining
            else:
                shares = min(base, remaining)
            if shares < self.MIN_SLICE_SHARES and i < n - 1:
                continue
            if shares <= 0:
                break
            schedule.append({
                'seq': i + 1,
                'shares': shares,
                'target_pct': round(shares / total_shares, 3) if total_shares > 0 else 0,
                'delay_min': duration_min // n * i,
            })
            remaining -= shares

        return schedule

    def _vwap_split(self, total_shares: int, duration_min: int,
                     max_slices: int) -> List[Dict]:
        """
        成交量加权拆分
        按历史成交量分布分配子单大小（简化：开盘/收盘时段权重高）
        """
        n = min(duration_min // 5, max_slices)  # 每5分钟一单
        if n < 2:
            return self._twap_split(total_shares, duration_min, max_slices)

        # 模拟成交量曲线权重 (U形曲线: 开盘高 → 盘中低 → 收盘高)
        weights = []
        for i in range(n):
            t = i / (n - 1) if n > 1 else 0.5
            # U形: 开盘/收盘时段成交量更大
            w = 1.0 - 0.4 * (2 * t - 1) ** 2  # 抛物线，两端高
            weights.append(max(w, 0.6))

        total_weight = sum(weights)
        schedule = []
        remaining = total_shares

        for i in range(n):
            target_shares = int(total_shares * weights[i] / total_weight
                               / self.MIN_SLICE_SHARES * self.MIN_SLICE_SHARES)
            if i == n - 1:
                shares = remaining
            else:
                shares = max(self.MIN_SLICE_SHARES,
                            min(target_shares, remaining - self.MIN_SLICE_SHARES))
            if shares <= 0:
                break
            schedule.append({
                'seq': i + 1,
                'shares': shares,
                'target_pct': round(shares / total_shares, 3) if total_shares > 0 else 0,
                'delay_min': duration_min // n * i,
            })
            remaining -= shares

        return schedule

    def _pov_split(self, total_shares: int, daily_volume: int,
                    participation_rate: float, max_slices: int) -> List[Dict]:
        """
        参与率算法 — 每次子单不超过成交量×参与率
        """
        if daily_volume <= 0:
            return self._twap_split(total_shares, 30, max_slices)

        max_slice = max(self.MIN_SLICE_SHARES,
                       int(daily_volume * participation_rate / 240 * 5))  # 5分钟窗口
        n = min(max_slices, math.ceil(total_shares / max_slice))
        base = total_shares // n // self.MIN_SLICE_SHARES * self.MIN_SLICE_SHARES

        schedule = []
        remaining = total_shares
        for i in range(n):
            if i == n - 1:
                shares = remaining
            else:
                shares = min(base, remaining, max_slice)
            if shares < self.MIN_SLICE_SHARES:
                continue
            schedule.append({
                'seq': i + 1,
                'shares': shares,
                'target_pct': round(shares / total_shares, 3) if total_shares > 0 else 0,
                'delay_min': i * 3,  # 每3分钟一单
            })
            remaining -= shares

        return schedule

    def _iceberg_split(self, total_shares: int, max_slices: int) -> List[Dict]:
        """
        冰山算法 — 每次只暴露固定小额，隐藏大单意图
        """
        # 每次只显示总订单的 10-20%
        display_pct = 0.15
        display_shares = max(self.MIN_SLICE_SHARES,
                            int(total_shares * display_pct / self.MIN_SLICE_SHARES)
                            * self.MIN_SLICE_SHARES)

        n = min(max_slices, math.ceil(total_shares / display_shares))
        schedule = []
        remaining = total_shares
        for i in range(n):
            if i == n - 1:
                shares = remaining
            else:
                shares = min(display_shares, remaining)
            if shares < self.MIN_SLICE_SHARES:
                continue
            schedule.append({
                'seq': i + 1,
                'shares': shares,
                'target_pct': round(shares / total_shares, 3) if total_shares > 0 else 0,
                'delay_min': i * 2,  # 每2分钟一单
                'display_shares': min(shares, display_shares),  # 冰山可见部分
            })
            remaining -= shares

        return schedule
