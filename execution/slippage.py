"""
猎场系统 V7.0 — 滑点估算器
基于实时买卖价差 + 历史冲击成本模型
"""

import logging
import numpy as np
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class SlippageEstimator:
    """
    滑点预估模型

    使用三层模型：
    1. 价差模型 — 基于 bid/ask spread
    2. 冲击成本 — 基于订单量/日均成交量
    3. 市场状态 — 波动率调整
    """

    # 中国市场典型参数
    DEFAULT_SPREAD_BPS = 2.0      # 默认买卖价差(bps)
    IMPACT_FACTOR = 0.1           # 冲击成本因子
    VOLATILITY_FACTOR = 0.5       # 波动率放大系数

    def __init__(self):
        self._history: Dict[str, list] = {}  # ts_code -> [slippage_records]

    def estimate(self, ts_code: str, price: float, order_amount: float,
                 daily_volume: float = None, spread_bps: float = None,
                 volatility: float = 0.02, urgency: str = 'normal') -> Dict:
        """
        预估滑点

        Args:
            ts_code: 股票代码
            price: 当前价格
            order_amount: 订单金额（用于计算冲击）
            daily_volume: 日均成交量（金额）
            spread_bps: 买卖价差 (bps)
            volatility: 日波动率
            urgency: 'low' | 'normal' | 'high' — 影响算法选择

        Returns:
            {
                'slippage_bps': float,      # 预估总滑点(bps)
                'slippage_pct': float,       # 百分比
                'spread_cost': float,        # 价差成本(bps)
                'impact_cost': float,        # 冲击成本(bps)
                'volatility_cost': float,    # 波动率成本(bps)
                'recommended_algo': str,     # 推荐算法
                'confidence': float,         # 预估值信度 (0-1)
            }
        """
        # 1. 价差成本
        spread = spread_bps or self.DEFAULT_SPREAD_BPS
        spread_cost = spread * 0.5  # 一半价差

        # 2. 冲击成本
        impact_cost = 0.0
        if daily_volume and daily_volume > 0:
            participation_rate = order_amount / daily_volume
            # Square-root impact model
            impact_cost = self.IMPACT_FACTOR * np.sqrt(participation_rate) * 10000
            impact_cost = min(impact_cost, 50)  # 上限50bps
        else:
            # 无成交量数据时保守估算
            impact_cost = 3.0

        # 3. 波动率调整
        vol_multiplier = 1.0
        if volatility > 0.03:  # 高波动
            vol_multiplier = 1.0 + self.VOLATILITY_FACTOR * (volatility - 0.03) / 0.02
        volatility_cost = spread_cost * (vol_multiplier - 1)

        # 4. 紧急度调整
        urgency_mult = {'low': 0.7, 'normal': 1.0, 'high': 1.5}[urgency]
        impact_cost *= urgency_mult

        total_bps = spread_cost + impact_cost + volatility_cost

        # 5. 推荐算法
        if impact_cost > 10 or urgency == 'low':
            algo = 'TWAP'  # 大冲击 → 时间加权
        elif volatility > 0.03:
            algo = 'VWAP'  # 高波动 → 成交量加权
        elif urgency == 'high':
            algo = 'POV'   # 紧急 → 参与率
        else:
            algo = 'TWAP'

        # 6. 信度
        confidence = 0.8
        if daily_volume:
            confidence = min(0.95, 0.5 + participation_rate * 50)
        if spread_bps is None:
            confidence -= 0.1

        return {
            'slippage_bps': round(total_bps, 2),
            'slippage_pct': round(total_bps / 10000, 6),
            'spread_cost': round(spread_cost, 2),
            'impact_cost': round(impact_cost, 2),
            'volatility_cost': round(volatility_cost, 2),
            'recommended_algo': algo,
            'confidence': round(confidence, 2),
        }

    def record_actual(self, ts_code: str, estimated_bps: float,
                       actual_bps: float, algo: str):
        """记录实际滑点用于模型校准"""
        if ts_code not in self._history:
            self._history[ts_code] = []
        self._history[ts_code].append({
            'estimated': estimated_bps,
            'actual': actual_bps,
            'algo': algo,
            'error': actual_bps - estimated_bps,
        })
        if len(self._history[ts_code]) > 100:
            self._history[ts_code] = self._history[ts_code][-100:]

    def get_calibration(self, ts_code: str = None) -> Dict:
        """获取模型校准统计"""
        all_errors = []
        codes = [ts_code] if ts_code else list(self._history.keys())
        for code in codes:
            all_errors.extend([r['error'] for r in self._history.get(code, [])])

        if not all_errors:
            return {'mean_error_bps': 0, 'rmse_bps': 0, 'sample_count': 0}

        errors = np.array(all_errors)
        return {
            'mean_error_bps': round(float(np.mean(errors)), 2),
            'rmse_bps': round(float(np.sqrt(np.mean(errors ** 2))), 2),
            'sample_count': len(errors),
        }
