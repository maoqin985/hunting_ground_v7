"""
猎场系统 V7.0 — 因子有效性检测
滚动IC变化监控，标记衰减因子，自动降权/淘汰
"""

import logging
import numpy as np
from typing import Dict, List, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class FactorEffectivenessMonitor:
    """
    因子有效性监控

    检测维度:
    1. IC 衰减 — 滚动IC趋势下降
    2. 换手率 — 因子多空组合换手是否异常
    3. 拥挤度 — 同风格因子数量是否过剩
    """

    DECAY_THRESHOLD = 0.3       # 有效性低于此值标记 decay
    RETIRE_THRESHOLD = 0.1      # 有效性低于此值淘汰
    DECAY_RATE_THRESHOLD = -0.02  # IC下降率超过-2%/月 → 衰减
    CROWDING_LIMIT = 8          # 同类别因子超过8个 → 拥挤

    def __init__(self, db=None, registry=None):
        self.db = db
        self.registry = registry  # FactorRegistry

    def check_all(self, factor_matrix: Dict[str, List[float]] = None) -> Dict:
        """
        检查所有活跃因子的有效性

        Args:
            factor_matrix: 可选，{factor_name: [因子值序列]}，提供时额外做冗余度检测

        Returns:
            {
                'total': int,
                'healthy': [name],
                'decaying': [name],
                'retired': [name],
                'crowded_categories': [category],
                'redundant_pairs': [{factor_a, factor_b, correlation}],
                'actions': [{factor, action, reason}],
            }
        """
        empty = {'total': 0, 'healthy': [], 'decaying': [], 'retired': [],
                 'crowded_categories': [], 'redundant_pairs': [], 'actions': []}
        if not self.db:
            return empty

        factors = self.db.get_active_factors()
        if not factors:
            return empty

        healthy = []
        decaying = []
        retired = []
        actions = []

        # 冗余度检测（维度三：Pearson 相关 > 0.7 剔除）
        redundant_pairs = self.detect_redundancy(factor_matrix) if factor_matrix else []

        # 按类别统计
        category_counts: Dict[str, int] = {}
        for f in factors:
            cat = f.get('category', 'unknown')
            category_counts[cat] = category_counts.get(cat, 0) + 1

        # 拥挤度检测
        crowded = [
            cat for cat, count in category_counts.items()
            if count > self.CROWDING_LIMIT
        ]

        for factor in factors:
            name = factor.get('factor_name', '')
            effectiveness = factor.get('effectiveness', 1.0)
            ic = factor.get('ic_value', 0)
            category = factor.get('category', 'volume_price')

            # 有效性判定
            if effectiveness < self.RETIRE_THRESHOLD:
                retired.append(name)
                actions.append({
                    'factor': name,
                    'action': 'retire',
                    'reason': f'Effectiveness {effectiveness:.3f} < {self.RETIRE_THRESHOLD}',
                })
            elif effectiveness < self.DECAY_THRESHOLD:
                decaying.append(name)
                actions.append({
                    'factor': name,
                    'action': 'decay',
                    'reason': f'Effectiveness {effectiveness:.3f} < {self.DECAY_THRESHOLD}',
                })
            elif category in crowded:
                # 拥挤类别中的弱因子降权
                if effectiveness < 0.5:
                    actions.append({
                        'factor': name,
                        'action': 'reduce_weight',
                        'reason': f'Category {category} crowded ({category_counts[category]} factors)',
                    })
            else:
                healthy.append(name)

        # 执行动作
        for action in actions:
            factor_name = action['factor']
            if action['action'] == 'retire':
                if self.registry:
                    self.registry.retire(factor_name)
                elif self.db:
                    self.db.update_factor_performance(factor_name, 0, 0, 0.05)
                    self.db.save_factor({
                        'factor_name': factor_name,
                        'status': 'retired',
                        'effectiveness': 0.05,
                        'last_evaluated': datetime.now().strftime('%Y-%m-%d'),
                    })
            elif action['action'] == 'decay':
                if self.registry:
                    self.registry.mark_decaying(factor_name)
            elif action['action'] == 'reduce_weight':
                if self.registry:
                    factor['weight'] = factor.get('weight', 1.0) * 0.7
                    self.db.save_factor(factor)

        logger.info(f"Factor check: {len(healthy)} healthy, {len(decaying)} decaying, "
                    f"{len(retired)} retired, {len(crowded)} crowded cats")

        return {
            'total': len(factors),
            'healthy': healthy,
            'decaying': decaying,
            'retired': retired,
            'crowded_categories': crowded,
            'redundant_pairs': redundant_pairs,
            'actions': actions,
        }

    def compute_rolling_ic_trend(self, factor_name: str,
                                   ic_history: List[float],
                                   window: int = 10) -> Dict:
        """
        计算滚动IC趋势

        Returns:
            {
                'current_ic': float,
                'rolling_mean': float,
                'trend_slope': float,     # 正=改善，负=恶化
                'trend_direction': str,    # 'improving' | 'stable' | 'decaying'
                'volatility': float,
            }
        """
        if len(ic_history) < window:
            return {'current_ic': 0, 'rolling_mean': 0, 'trend_slope': 0,
                    'trend_direction': 'stable', 'volatility': 0}

        ic_array = np.array(ic_history[-window:])
        current_ic = ic_array[-1] if len(ic_array) > 0 else 0
        rolling_mean = np.mean(ic_array)

        # 线性趋势
        if len(ic_array) >= 5:
            t = np.arange(len(ic_array))
            slope = np.polyfit(t, ic_array, 1)[0]
        else:
            slope = 0

        # 方向判定
        if slope > 0.005:
            direction = 'improving'
        elif slope < -0.005:
            direction = 'decaying'
        else:
            direction = 'stable'

        volatility = np.std(ic_array) if len(ic_array) > 1 else 0

        return {
            'current_ic': round(float(current_ic), 4),
            'rolling_mean': round(float(rolling_mean), 4),
            'trend_slope': round(float(slope), 6),
            'trend_direction': direction,
            'volatility': round(float(volatility), 4),
        }

    def suggest_weights(self, method: str = 'effectiveness') -> Dict[str, float]:
        """根据有效性建议因子权重（支持多算法，见 compute_weights）"""
        if not self.db:
            return {}
        return self.compute_weights(self.db.get_active_factors(), method)

    @staticmethod
    def detect_redundancy(factor_matrix: Dict[str, List[float]],
                          threshold: float = 0.7) -> List[Dict]:
        """
        冗余度检测（维度三）：Pearson 相关 |corr| > threshold 的因子对。

        对每个因子对计算 Pearson 相关系数，超过阈值即标记冗余，
        供上层剔除其中有效性较低的一方。
        """
        if not factor_matrix:
            return []
        names = list(factor_matrix.keys())
        redundant = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a = np.asarray(factor_matrix[names[i]], dtype=float)
                b = np.asarray(factor_matrix[names[j]], dtype=float)
                if a.shape != b.shape:
                    continue
                mask = ~(np.isnan(a) | np.isnan(b))
                if mask.sum() < 10:
                    continue
                try:
                    corr = float(np.corrcoef(a[mask], b[mask])[0, 1])
                except (ValueError, IndexError):
                    continue
                if abs(corr) > threshold:
                    redundant.append({
                        'factor_a': names[i],
                        'factor_b': names[j],
                        'correlation': round(corr, 4),
                    })
        return redundant

    @staticmethod
    def compute_weights(factors: List[Dict], method: str = 'effectiveness',
                        volatility_map: Dict[str, float] = None) -> Dict[str, float]:
        """
        多因子加权算法（维度三「6种多因子加权算法」）。

        Args:
            factors: 因子字典列表，需含 factor_name，可选 effectiveness/ic_value/sharpe
            method: 加权算法
              - 'equal'          等权
              - 'equal_topn'     等权 TopN（按有效性取前 N，N=max(5, n/2)）
              - 'ic'             IC 加权（|IC| 归一化，无 IC 时退化为等权）
              - 'inverse_vol'    逆波动率加权（需 volatility_map，否则退化为等权）
              - 'max_sharpe'     最大夏普加权（Sharpe 归一化，无正 Sharpe 时退化为等权）
              - 'effectiveness'  综合有效性加权（默认）
            volatility_map: {factor_name: 波动率}，仅 inverse_vol 使用

        Returns:
            {factor_name: weight}
        """
        if not factors:
            return {}
        names = [f['factor_name'] for f in factors]
        n = len(factors)
        if n == 1:
            return {names[0]: 1.0}

        def _normalize(key, default=0.0):
            vals = [max(f.get(key, default), 0.0) for f in factors]
            s = sum(vals)
            if s <= 0:
                return None
            return {f['factor_name']: round(max(f.get(key, default), 0.0) / s, 4) for f in factors}

        if method == 'equal':
            return {nm: round(1.0 / n, 4) for nm in names}

        if method == 'equal_topn':
            top_n = min(n, max(5, n // 2))
            top = sorted(factors, key=lambda f: f.get('effectiveness', 0), reverse=True)[:top_n]
            return {f['factor_name']: round(1.0 / top_n, 4) for f in top}

        if method == 'ic':
            ics = [abs(f.get('ic_value', 0.0)) for f in factors]
            s = sum(ics)
            if s <= 0:
                return {nm: round(1.0 / n, 4) for nm in names}
            return {f['factor_name']: round(abs(f.get('ic_value', 0.0)) / s, 4) for f in factors}

        if method == 'inverse_vol':
            if volatility_map:
                inv = {f['factor_name']: 1.0 / max(volatility_map.get(f['factor_name'], 1.0), 1e-6)
                       for f in factors}
                s = sum(inv.values())
                return {nm: round(v / s, 4) for nm, v in inv.items()}
            return {nm: round(1.0 / n, 4) for nm in names}

        if method == 'max_sharpe':
            w = _normalize('sharpe')
            return w if w else {nm: round(1.0 / n, 4) for nm in names}

        # 默认 effectiveness
        w = _normalize('effectiveness', 1.0)
        return w if w else {nm: round(1.0 / n, 4) for nm in names}
