"""
猎场系统 V7.0 — 智能选股分级器
多策略分级选股池：动量/价值/突破/融合
动态加权评分，输出分级股票池
"""

import logging
import numpy as np
from typing import Dict, List, Optional
from datetime import datetime

from .factor_registry import FactorRegistry

logger = logging.getLogger(__name__)


class StockGrader:
    """
    多策略股票分级器

    四大策略池：
    - momentum:  动量驱动 — 进攻型偏好
    - value:     价值分散 — 稳定型偏好
    - breakout:  突破发现 — 成长猎手偏好
    - fused:     融合加权 — 全账户通用

    输出格式: {ts_code: {score, rank, tier, factors}}
    分级: S(≥80) / A(≥65) / B(≥50) / C(≥35) / D(<35)
    """

    # 各策略的因子权重模板
    STRATEGY_WEIGHTS = {
        'momentum': {
            'volume_price': 0.45,
            'sentiment': 0.25,
            'fundamental': 0.10,
            'momentum_bias': 0.20,  # 动量因子加成
        },
        'value': {
            'volume_price': 0.20,
            'sentiment': 0.15,
            'fundamental': 0.45,
            'quality_bias': 0.20,    # 质量因子加成
        },
        'breakout': {
            'volume_price': 0.35,
            'sentiment': 0.20,
            'fundamental': 0.15,
            'discovery_bias': 0.30,  # 发现因子加成（新因子权重高）
        },
        'fused': {
            'volume_price': 0.30,
            'sentiment': 0.25,
            'fundamental': 0.25,
            'ai_bias': 0.20,         # AI增强因子加成
        },
    }

    # 分级阈值
    TIER_THRESHOLDS = {
        'S': 80,
        'A': 65,
        'B': 50,
        'C': 35,
        'D': 0,
    }

    def __init__(self, db=None, registry: FactorRegistry = None):
        self.db = db
        self.registry = registry

    def grade(self, stock_scores: Dict[str, Dict[str, float]],
              strategy_type: str = 'fused',
              top_n: int = 50) -> List[Dict]:
        """
        对股票池打分排序

        Args:
            stock_scores: {
                ts_code: {
                    'factor_1': score,
                    'factor_2': score,
                    ...
                }
            }
            strategy_type: momentum | value | breakout | fused
            top_n: 返回前N只

        Returns:
            [{ts_code, score, rank, tier, tier_label, factor_scores}]
        """
        weights = self.STRATEGY_WEIGHTS.get(strategy_type,
                                             self.STRATEGY_WEIGHTS['fused'])

        # 获取因子权重（从registry）
        factor_weights = self._get_factor_weights(strategy_type)

        graded = []
        for ts_code, factor_scores in stock_scores.items():
            score = self._compute_weighted_score(
                factor_scores, weights, factor_weights
            )
            tier = self._assign_tier(score)
            graded.append({
                'ts_code': ts_code,
                'score': round(score, 1),
                'rank': 0,
                'tier': tier,
                'tier_label': self._tier_label(tier),
                'factors': {k: round(v, 2) for k, v in factor_scores.items()},
            })

        # 排序
        graded.sort(key=lambda x: x['score'], reverse=True)

        # 赋值排名
        for i, item in enumerate(graded):
            item['rank'] = i + 1

        # Top N
        result = graded[:top_n]

        # 分级统计
        tiers = {}
        for item in result:
            t = item['tier']
            tiers[t] = tiers.get(t, 0) + 1
        logger.info(f"[{strategy_type}] Pool graded: {len(result)} stocks, "
                    f"tiers: {tiers}")

        return result

    def grade_multi_strategy(self, stock_scores: Dict[str, Dict[str, float]],
                              top_n: int = 50) -> Dict[str, List[Dict]]:
        """
        输出四大策略分级池

        Returns:
            {strategy_type: [{ts_code, score, rank, tier}]}
        """
        pools = {}
        for strategy in ['momentum', 'value', 'breakout', 'fused']:
            pools[strategy] = self.grade(stock_scores, strategy, top_n)
        return pools

    def _compute_weighted_score(self, factor_scores: Dict[str, float],
                                 strategy_weights: Dict[str, float],
                                 factor_weights: Dict[str, float]) -> float:
        """计算加权总分"""
        total = 0.0
        weight_sum = 0.0

        for factor_name, score in factor_scores.items():
            fw = factor_weights.get(factor_name, 1.0)
            total += score * fw
            weight_sum += fw

        if weight_sum > 0:
            total /= weight_sum

        # 归一化到0-100
        return max(0, min(total * 20, 100))

    def _get_factor_weights(self, strategy_type: str) -> Dict[str, float]:
        """从registry获取各因子在当前策略下的权重"""
        weights = {}
        if not self.registry:
            return weights

        strategy_cfg = self.STRATEGY_WEIGHTS.get(strategy_type, {})
        category_weights = {
            'volume_price': strategy_cfg.get('volume_price', 0.30),
            'sentiment': strategy_cfg.get('sentiment', 0.25),
            'fundamental': strategy_cfg.get('fundamental', 0.25),
        }

        for factor in self.registry.get_active():
            name = factor.get('factor_name', '')
            category = factor.get('category', 'volume_price')
            base_weight = factor.get('weight', 1.0)
            category_multiplier = category_weights.get(category, 0.30)

            # 新发现因子加成（breakout策略）
            if strategy_type == 'breakout' and factor.get('status') == 'active':
                category_multiplier *= 1.3

            weights[name] = base_weight * category_multiplier

        return weights

    def _assign_tier(self, score: float) -> str:
        """分配等级"""
        for tier, threshold in self.TIER_THRESHOLDS.items():
            if score >= threshold:
                return tier
        return 'D'

    def _tier_label(self, tier: str) -> str:
        labels = {
            'S': '强烈推荐',
            'A': '推荐买入',
            'B': '观察候选',
            'C': '暂不参与',
            'D': '回避',
        }
        return labels.get(tier, '未知')

    def save_pool(self, strategy_type: str, pool: List[Dict],
                   trade_date: str = None):
        """保存选股池到数据库"""
        if not self.db or not trade_date:
            return
        self.db.save_stock_pool(pool, strategy_type, trade_date)

    def load_pool(self, strategy_type: str, trade_date: str = None) -> List[Dict]:
        """从数据库加载选股池"""
        if not self.db:
            return []
        return self.db.get_stock_pool(strategy_type, trade_date)
