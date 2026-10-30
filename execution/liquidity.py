"""
猎场系统 V7.0 — 流动性适配器
子单量不超过最近N分钟成交量的10%，低流动性自动缩减
"""

import logging
import numpy as np
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class LiquidityAdapter:
    """
    流动性适配

    规则:
    - 子单量 ≤ 最近5分钟成交量的10%（避免冲击）
    - 低流动性股票自动缩减订单
    - 最小成交量阈值：日成交额 > 500万元
    """

    MAX_PARTICIPATION_RATE = 0.10   # 最大参与率 10%
    MIN_DAILY_AMOUNT = 5_000_000    # 日成交额最低500万
    LOW_LIQUIDITY_MULTIPLIER = 0.5  # 低流动性时减半

    def __init__(self):
        self._liquidity_cache: Dict[str, Dict] = {}

    def check(self, ts_code: str, order_shares: int,
              recent_volume: int = 0,  # 最近5分钟成交量
              daily_volume: int = 0,    # 日均成交量
              daily_amount: float = 0,  # 日均成交额
              ) -> Dict:
        """
        检查并调整订单以适配流动性

        Returns:
            {
                'adjusted_shares': int,     # 调整后的股数
                'is_reduced': bool,          # 是否被缩减
                'reduction_pct': float,      # 缩减比例
                'liquidity_level': str,      # 'high' | 'normal' | 'low' | 'illiquid'
                'warnings': [str],
            }
        """
        warnings = []
        adjusted = order_shares
        is_reduced = False

        # 1. 日成交额检查
        if daily_amount > 0 and daily_amount < self.MIN_DAILY_AMOUNT:
            warnings.append(f"日成交额{daily_amount/10000:.0f}万 < 最低{self.MIN_DAILY_AMOUNT/10000:.0f}万")
            adjusted = int(adjusted * self.LOW_LIQUIDITY_MULTIPLIER)
            is_reduced = True

        # 2. 参与率检查
        if recent_volume > 0:
            max_allowed = int(recent_volume * self.MAX_PARTICIPATION_RATE)
            if adjusted > max_allowed:
                adjusted = max_allowed
                is_reduced = True
                warnings.append(
                    f"订单{adjusted}股 > 10%参与率上限{max_allowed}股（近5分钟成交{recent_volume}）"
                )

        # 3. 日均量占比检查
        if daily_volume > 0:
            daily_pct = order_shares / daily_volume if daily_volume > 0 else 1
            if daily_pct > 0.05:  # 单笔超过日均5%
                capped = int(daily_volume * 0.05)
                if adjusted > capped:
                    adjusted = min(adjusted, capped)
                    is_reduced = True
                    warnings.append(f"订单超过日均5%，限制为{capped}股")

        # 4. 确定流动性等级
        liquidity_level = 'normal'
        if daily_amount <= 0:
            liquidity_level = 'normal'  # 无数据时不判断
        elif daily_amount < 10_000_000:  # <1000万
            liquidity_level = 'low'
        elif daily_amount < self.MIN_DAILY_AMOUNT:  # <500万
            liquidity_level = 'illiquid'
        elif daily_amount > 100_000_000:  # >1亿
            liquidity_level = 'high'

        # 5. 确保100股整数
        adjusted = max(0, adjusted // 100 * 100)

        reduction_pct = (order_shares - adjusted) / order_shares if order_shares > 0 else 0

        if warnings:
            logger.info(f"Liquidity adjustment for {ts_code}: "
                        f"{order_shares} -> {adjusted} ({reduction_pct:.0%}) "
                        f"[{'; '.join(warnings)}]")

        return {
            'adjusted_shares': adjusted,
            'is_reduced': is_reduced,
            'reduction_pct': round(reduction_pct, 3),
            'liquidity_level': liquidity_level,
            'warnings': warnings,
        }

    def estimate_recent_volume(self, realtime_data: Dict) -> int:
        """从实时数据估算最近5分钟成交量"""
        return realtime_data.get('volume_5min', 0)

    def get_liquidity_score(self, daily_amount: float,
                             daily_volume: int, price: float) -> float:
        """
        计算流动性综合得分 (0-1)

        用于选股阶段过滤流动性不足的股票
        """
        if daily_amount <= 0:
            return 0.5  # 未知

        score = 0.0

        # 成交额得分
        if daily_amount >= 100_000_000:  # >1亿
            score += 0.4
        elif daily_amount >= 50_000_000:  # >5000万
            score += 0.3
        elif daily_amount >= 20_000_000:  # >2000万
            score += 0.2
        elif daily_amount >= self.MIN_DAILY_AMOUNT:  # >500万
            score += 0.1

        # 换手率得分
        if price > 0 and daily_volume > 0:
            turnover = daily_amount / (daily_volume * price) if daily_volume * price > 0 else 0
            if 0.02 <= turnover <= 0.10:  # 2-10% 换手率最佳
                score += 0.3
            elif 0.01 <= turnover <= 0.20:
                score += 0.15

        # 价差估算
        score += 0.3 if daily_amount > 50_000_000 else 0.1

        return min(score, 1.0)
