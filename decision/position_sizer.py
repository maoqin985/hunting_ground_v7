"""
猎场系统 V7.0 — 仓位计算器（per-account 资金）
Kelly公式 + 波动率调整 + 小资金约束
"""

import logging
import numpy as np
from typing import Dict, Optional
from math import log

logger = logging.getLogger(__name__)


class PositionSizer:
    """
    仓位计算引擎（per-account 资金）
    - Kelly公式 + 波动率调整 + 流动性约束
    - 支持显式 capital/limits 覆盖（V7.0 per-account）
    """

    def __init__(self, config: dict = None,
                 total_capital: float = None,
                 max_positions: int = None,
                 max_single_pct: float = None,
                 min_cash_pct: float = None):
        self.config = config or {}
        cap_cfg = self.config.get("capital", {})

        # V7.0: 显式参数优先于 config
        self.total_capital = total_capital or cap_cfg.get("total", 2000000.0)
        self.max_positions = max_positions or cap_cfg.get("max_positions", 6)
        self.max_single_pct = max_single_pct or cap_cfg.get("max_single_position_pct", 0.25)
        self.min_cash_pct = min_cash_pct or cap_cfg.get("min_cash_reserve_pct", 0.10)

    def calculate(self, ts_code: str, price: float,
                  signal_score: float,
                  ai_suggested_pct: float = 0.0,
                  volatility: float = 0.02,
                  win_rate: float = 0.55,
                  avg_win_loss_ratio: float = 1.5,
                  current_positions: int = 0) -> Dict:
        """
        计算最优仓位

        Returns:
            {
                'shares': int,           # 建议买入股数（100的倍数）
                'amount': float,         # 建议买入金额
                'position_pct': float,   # 仓位比例
                'kelly_pct': float,      # Kelly公式算出的比例
                'method': str,           # 计算方法
            }
        """
        # 1. Kelly 公式
        kelly_pct = self._kelly_criterion(win_rate, avg_win_loss_ratio)

        # 2. 波动率调整
        if volatility > 0:
            vol_adjustment = 0.02 / max(volatility, 0.005)  # 以2%日波动为基准
            kelly_pct *= vol_adjustment

        # 3. 分数调整
        score_factor = (signal_score - 40) / 60  # 评分40-100映射到0-1
        score_factor = max(0, min(score_factor, 1))
        adjusted_pct = kelly_pct * score_factor

        # 4. 资金约束
        available_cash = self.total_capital * (1 - self.min_cash_pct)
        if current_positions >= self.max_positions:
            adjusted_pct = 0.0  # 持仓已满

        # 5. AI 建议融合
        if ai_suggested_pct > 0:
            adjusted_pct = adjusted_pct * 0.4 + ai_suggested_pct * 0.6  # AI权重60%

        # 6. 最终约束
        final_pct = min(adjusted_pct, self.max_single_pct, 0.70)
        if final_pct < 0.05:  # 最小5%仓位
            final_pct = 0.0

        # 7. 计算股数（100股整数倍）
        amount = self.total_capital * final_pct
        raw_shares = int(amount / price / 100) * 100 if price > 0 else 0

        if raw_shares < 100:
            raw_shares = 0
            final_pct = 0.0
            amount = 0.0

        # 8. 确保金额不超过约束
        actual_amount = raw_shares * price
        actual_pct = actual_amount / self.total_capital if self.total_capital > 0 else 0

        # 9. 交易成本检查
        fee = actual_amount * 0.00075  # 约0.075%买卖
        if fee > actual_amount * 0.005:  # 手续费不超过0.5%
            logger.warning(f"交易成本占比过高: {fee / actual_amount * 100:.2f}%")

        return {
            'shares': raw_shares,
            'amount': actual_amount,
            'position_pct': round(actual_pct, 3),
            'kelly_pct': round(kelly_pct, 3),
            'adjusted_pct': round(adjusted_pct, 3),
            'fee_estimate': round(fee, 2),
            'method': f"Kelly({kelly_pct:.1%})+VolAdjust+AI({ai_suggested_pct:.1%})",
        }

    def calculate_convertible_bond(self, bond_price: float,
                                    signal_score: float,
                                    current_positions: int = 0) -> Dict:
        """可转债专用仓位计算"""
        bond_cfg = self.config.get("instruments", {}).get("convertible_bond", {})
        max_cb_pct = bond_cfg.get("capital_pct", 0.40)

        # 可转债用更激进的Kelly（T+0流动性好，止损紧）
        kelly_pct = self._kelly_criterion(0.55, 1.2) * 1.2

        adjusted = kelly_pct * (signal_score - 40) / 60
        adjusted = min(adjusted, max_cb_pct, 0.50)

        amount = self.total_capital * adjusted
        # 转债1手=10张，每张约100-130元
        shares = int(amount / (bond_price * 10)) * 10 if bond_price > 0 else 0

        return {
            'shares': shares,  # 张数
            'amount': shares * bond_price,
            'position_pct': round((shares * bond_price) / self.total_capital, 3),
            'kelly_pct': round(kelly_pct, 3),
            'method': 'Kelly(CB)+T0Adjust',
        }

    def _kelly_criterion(self, win_rate: float, win_loss_ratio: float) -> float:
        """
        Kelly 公式: f* = p - (1-p)/(W/L)

        Args:
            win_rate: 胜率
            win_loss_ratio: 盈亏比 (平均盈利/平均亏损的绝对值)

        Returns:
            最优仓位比例
        """
        win_rate = max(0.3, min(win_rate, 0.8))
        win_loss_ratio = max(0.5, min(win_loss_ratio, 5.0))

        kelly = win_rate - (1 - win_rate) / win_loss_ratio

        # 使用 1/2 Kelly 降低风险
        half_kelly = kelly * 0.5

        return max(0.0, min(half_kelly, self.max_single_pct))

    def calculate_rebalance(self, current_positions: Dict[str, Dict],
                            new_signal: Dict) -> Dict:
        """计算调仓（优胜劣汰）"""
        # 检查是否需要替换持仓
        current_count = len(current_positions)

        if current_count < self.max_positions:
            return {'action': 'add', 'reason': f'持仓{current_count}/{self.max_positions}'}
        elif current_count >= self.max_positions:
            # 比较末位持仓
            worst_code = min(current_positions.keys(),
                             key=lambda c: current_positions[c].get('score', 0))

            if new_signal.get('score', 0) > current_positions[worst_code].get('score', 0) + 5:
                return {
                    'action': 'replace',
                    'replace_code': worst_code,
                    'reason': f'评分高于末位{worst_code}超过5分'
                }

        return {'action': 'skip', 'reason': '持仓已满且新信号不优于末位'}
