"""
猎场系统 V7.0 — 因子评估器
IC/ICIR 计算 + 分层回测 + 有效性判定
"""

import logging
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class FactorEvaluator:
    """因子评估 — IC分析 + 分层回测"""

    MIN_IC = 0.02          # 最小 |IC| 阈值
    MIN_IR = 0.3           # 最小 IR 阈值
    REDUNDANCY_THRESHOLD = 0.7  # 相关性去重阈值

    def __init__(self, db=None):
        self.db = db
        self._evaluation_cache: Dict[str, Dict] = {}

    @staticmethod
    def _spearmanr(x: np.ndarray, y: np.ndarray) -> float:
        """纯numpy实现的Spearman秩相关系数"""
        if len(x) < 3:
            return 0.0
        # 计算秩
        x_rank = np.argsort(np.argsort(x)).astype(float)
        y_rank = np.argsort(np.argsort(y)).astype(float)
        # Pearson on ranks
        xm = x_rank - np.mean(x_rank)
        ym = y_rank - np.mean(y_rank)
        denom = np.sqrt(np.sum(xm ** 2) * np.sum(ym ** 2))
        if denom < 1e-12:
            return 0.0
        return float(np.sum(xm * ym) / denom)

    def compute_rank_ic(self, factor_values: np.ndarray,
                        forward_returns: np.ndarray) -> float:
        """
        计算 Rank IC (Spearman)

        Args:
            factor_values: shape (N,) 因子值
            forward_returns: shape (N,) 未来N日收益

        Returns:
            Rank IC ∈ [-1, 1]
        """
        if len(factor_values) < 30 or len(forward_returns) < 30:
            return 0.0

        mask = ~(np.isnan(factor_values) | np.isnan(forward_returns))
        if mask.sum() < 30:
            return 0.0

        ic = self._spearmanr(factor_values[mask], forward_returns[mask])
        return float(ic) if not np.isnan(ic) else 0.0

    def compute_icir(self, ic_series: np.ndarray) -> Tuple[float, float]:
        """
        计算 ICIR (IC 信息比)

        Returns:
            (icir, ic_mean)
        """
        ic_series = ic_series[~np.isnan(ic_series)]
        if len(ic_series) < 10:
            return 0.0, 0.0

        ic_mean = np.mean(ic_series)
        ic_std = np.std(ic_series, ddof=1)
        icir = ic_mean / (ic_std + 1e-8)
        return float(icir), float(ic_mean)

    def stratified_backtest(self, factor_values: np.ndarray,
                            forward_returns: np.ndarray,
                            n_groups: int = 5) -> Dict:
        """
        分层回测

        将股票按因子值分N组，计算各组未来收益

        Returns:
            {
                'top_return': float,       # 最高组平均收益
                'bottom_return': float,    # 最低组平均收益
                'spread': float,           # 多空收益差
                'monotonicity': float,     # 单调性得分 (0-1)
                'group_returns': [float],  # 各组收益
            }
        """
        valid = ~(np.isnan(factor_values) | np.isnan(forward_returns))
        fv = factor_values[valid]
        fr = forward_returns[valid]

        if len(fv) < n_groups * 10:
            return {'top_return': 0, 'bottom_return': 0, 'spread': 0,
                    'monotonicity': 0, 'group_returns': []}

        # 分组
        quantiles = np.percentile(fv, np.linspace(0, 100, n_groups + 1))
        group_returns = []
        for i in range(n_groups):
            mask = (fv >= quantiles[i]) & (fv < quantiles[i + 1]) if i < n_groups - 1 \
                else (fv >= quantiles[i]) & (fv <= quantiles[i + 1])
            if mask.sum() > 0:
                group_returns.append(float(np.mean(fr[mask])))
            else:
                group_returns.append(0.0)

        spread = group_returns[-1] - group_returns[0] if group_returns else 0

        # 单调性：逐组递增的比例
        monotonicity = 0
        if len(group_returns) >= 2:
            increases = sum(1 for i in range(1, len(group_returns))
                           if group_returns[i] > group_returns[i - 1])
            monotonicity = increases / (len(group_returns) - 1)

        return {
            'top_return': group_returns[-1] if group_returns else 0,
            'bottom_return': group_returns[0] if group_returns else 0,
            'spread': spread,
            'monotonicity': monotonicity,
            'group_returns': group_returns,
        }

    def evaluate_factor(self, factor_name: str, factor_values: np.ndarray,
                        forward_returns: np.ndarray,
                        ic_series: np.ndarray = None,
                        category: str = 'volume_price',
                        expression: str = '') -> Optional[Dict]:
        """
        完整因子评估

        Returns:
            {
                'factor_name': str,
                'category': str,
                'expression': str,
                'ic_mean': float,        # 平均IC
                'icir': float,           # IC信息比
                'sharpe': float,         # 多空Sharpe
                'effectiveness': float,  # 综合有效性 (0-1)
                'is_valid': bool,        # 是否有效
                'stratified': dict,      # 分层结果
                'recommended_weight': float,
            }
        """
        if len(factor_values) < 30:
            return None

        ic = self.compute_rank_ic(factor_values, forward_returns)
        abs_ic = abs(ic)

        # ICIR
        icir = 0.0
        ic_mean = ic
        if ic_series is not None and len(ic_series) >= 10:
            icir, _ = self.compute_icir(ic_series)

        # 分层回测
        stratified = self.stratified_backtest(factor_values, forward_returns)

        # Sharpe（基于分层 spread 序列简化计算）
        spread = stratified.get('spread', 0)
        sharpe = spread / 0.02 if abs(spread) > 0 else 0  # 简化

        # 综合有效性: IC * 0.3 + IR * 0.3 + monotonicity * 0.2 + spread_norm * 0.2
        spread_norm = min(abs(spread) / 0.03, 1.0)  # 3% spread = 满分
        ir_norm = min(icir / 1.0, 1.0)  # IR=1 = 满分
        effectiveness = (
            abs_ic / 0.1 * 0.3 +
            ir_norm * 0.3 +
            stratified.get('monotonicity', 0) * 0.2 +
            spread_norm * 0.2
        )
        effectiveness = min(effectiveness, 1.0)

        is_valid = abs_ic >= self.MIN_IC and icir >= self.MIN_IR

        return {
            'factor_name': factor_name,
            'category': category,
            'expression': expression,
            'ic_value': round(ic, 6),
            'icir': round(icir, 4),
            'sharpe': round(sharpe, 3),
            'effectiveness': round(effectiveness, 4),
            'is_valid': is_valid,
            'stratified': stratified,
            'recommended_weight': round(max(0.1, effectiveness), 2),
            'evaluated_at': datetime.now().strftime('%Y-%m-%d'),
        }

    def compute_factor_correlation(self, fv1: np.ndarray, fv2: np.ndarray) -> float:
        """计算两因子的Spearman相关性"""
        mask = ~(np.isnan(fv1) | np.isnan(fv2))
        if mask.sum() < 30:
            return 0.0
        corr = self._spearmanr(fv1[mask], fv2[mask])
        return float(corr) if not np.isnan(corr) else 0.0

    def find_redundant(self, new_factor_values: np.ndarray,
                       existing_factors: Dict[str, np.ndarray]) -> List[str]:
        """
        查找与新因子冗余的已有因子 (corr > REDUNDANCY_THRESHOLD)

        Returns:
            冗余因子名称列表
        """
        redundant = []
        for name, values in existing_factors.items():
            corr = self.compute_factor_correlation(new_factor_values, values)
            if abs(corr) > self.REDUNDANCY_THRESHOLD:
                redundant.append(name)
        return redundant
