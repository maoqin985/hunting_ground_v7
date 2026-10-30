"""
猎场系统 V7.0 — 资产配置器
根据账户人格自动计算目标资产配置权重
"""
from typing import Dict


class AssetAllocator:
    """自动资产配置"""

    @staticmethod
    def get_target_allocation(account) -> Dict[str, float]:
        """
        根据账户人格返回目标配置权重

        Returns:
            {asset_class: target_pct} 如 {'a_share': 0.55, 'convertible_bond': 0.20, ...}
        """
        profile = account.strategy_params

        # 优先使用账户级自定义配置
        allocation = profile.get('capital_allocation', {})
        if allocation:
            return allocation

        # 根据人格类型使用默认配置
        from .profiles import PROFILE_MAP
        default = PROFILE_MAP.get(account.profile_type, {})
        return default.get('capital_allocation', {
            'a_share': 0.50,
            'convertible_bond': 0.25,
            'etf': 0.15,
            'cash_reserve': 0.10,
        })

    @staticmethod
    def compute_drift(account, current_values: Dict[str, float]) -> Dict[str, float]:
        """
        计算当前配置与目标的偏离

        Args:
            current_values: {asset_class: current_value}

        Returns:
            {asset_class: drift_pct} 正数=超配, 负数=低配
        """
        target = AssetAllocator.get_target_allocation(account)
        total = sum(current_values.values())
        if total <= 0:
            return {}

        drift = {}
        for asset_class, target_pct in target.items():
            current_pct = current_values.get(asset_class, 0) / total
            drift[asset_class] = current_pct - target_pct

        return drift

    @staticmethod
    def suggest_rebalance(account, current_values: Dict[str, float],
                          threshold: float = 0.05) -> Dict[str, str]:
        """
        建议再平衡操作

        Args:
            threshold: 偏离超过此值才建议调整

        Returns:
            {asset_class: 'buy' | 'sell' | 'hold'}
        """
        drift = AssetAllocator.compute_drift(account, current_values)
        suggestions = {}
        for asset_class, d in drift.items():
            if d > threshold:
                suggestions[asset_class] = 'sell'
            elif d < -threshold:
                suggestions[asset_class] = 'buy'
            else:
                suggestions[asset_class] = 'hold'
        return suggestions
