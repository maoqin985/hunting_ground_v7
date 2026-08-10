"""
猎场系统 V6.0 — 周度遗传算法超参优化
优化目标: 最大化 夏普比率 × 胜率
优化参数: 信号阈值 / 仓位系数 / 止损比例
"""

import logging
import random
import numpy as np
from typing import Dict, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class WeeklyOptimizer:
    """
    遗传算法超参优化器
    种群: 50, 迭代: 30代
    """

    def __init__(self, db, config: dict = None):
        self.db = db
        self.config = config or {}
        self._param_bounds = {
            'score_threshold': (30, 60),       # AI评分阈值
            'position_pct': (0.15, 0.65),       # 仓位比例
            'stop_loss_pct': (-0.08, -0.03),    # 止损比例
            'rsi_threshold': (20, 40),          # RSI阈值
            'volume_ratio_threshold': (1.2, 3.0), # 量比阈值
            'trailing_stop_activate': (0.03, 0.08), # 移动止损激活
            'max_hold_days': (3, 15),           # 最大持有天数
        }

    def optimize(self, backtest_fn, population_size: int = 50,
                 generations: int = 30) -> Dict:
        """
        遗传算法优化

        Args:
            backtest_fn: 回测函数 fn(params) -> (sharpe, win_rate)
        """
        logger.info(f"Genetic optimization: pop={population_size}, gen={generations}")

        # 初始化种群
        population = [self._random_individual() for _ in range(population_size)]
        best_individual = None
        best_fitness = -float('inf')
        history = []

        for gen in range(generations):
            # 评估适应度
            fitness_scores = []
            for ind in population:
                try:
                    sharpe, win_rate = backtest_fn(ind)
                    fitness = sharpe * max(win_rate, 0.01)  # 适应度 = 夏普 × 胜率
                    if np.isnan(fitness) or np.isinf(fitness):
                        fitness = -1.0
                except Exception:
                    fitness = -1.0
                fitness_scores.append(fitness)

            # 精英保留
            sorted_pop = sorted(zip(population, fitness_scores), key=lambda x: -x[1])
            if sorted_pop[0][1] > best_fitness:
                best_fitness = sorted_pop[0][1]
                best_individual = sorted_pop[0][0].copy()

            history.append({
                'generation': gen,
                'best_fitness': best_fitness,
                'avg_fitness': np.mean(fitness_scores),
                'median_fitness': np.median(fitness_scores),
            })

            logger.debug(f"Gen {gen + 1}/{generations}: best={best_fitness:.4f}, avg={np.mean(fitness_scores):.4f}")

            # 选择（锦标赛）
            new_pop = [sorted_pop[0][0]]  # 精英保留
            while len(new_pop) < population_size:
                p1 = self._tournament_select(population, fitness_scores)
                p2 = self._tournament_select(population, fitness_scores)

                # 交叉
                c1, c2 = self._crossover(p1, p2)

                # 变异
                c1 = self._mutate(c1, gen / generations)
                c2 = self._mutate(c2, gen / generations)

                new_pop.extend([c1, c2])

            population = new_pop[:population_size]

        # 约束调整：单次变更不超过20%
        if best_individual:
            best_individual = self._apply_change_limits(best_individual)

        result = {
            'best_params': best_individual,
            'best_fitness': best_fitness,
            'history': history,
            'optimized_at': datetime.now().isoformat(),
        }

        logger.info(f"Optimization complete: best_fitness={best_fitness:.4f}, params={best_individual}")
        return result

    def _random_individual(self) -> Dict:
        return {
            name: random.uniform(*bounds)
            for name, bounds in self._param_bounds.items()
        }

    def _tournament_select(self, population: List[Dict], fitness: List[float],
                           k: int = 3) -> Dict:
        """锦标赛选择"""
        idxs = random.sample(range(len(population)), min(k, len(population)))
        best_idx = max(idxs, key=lambda i: fitness[i])
        return population[best_idx].copy()

    def _crossover(self, p1: Dict, p2: Dict) -> Tuple[Dict, Dict]:
        """均匀交叉"""
        c1, c2 = {}, {}
        for key in p1:
            if random.random() < 0.5:
                c1[key], c2[key] = p1[key], p2[key]
            else:
                c1[key], c2[key] = p2[key], p1[key]
        return c1, c2

    def _mutate(self, individual: Dict, progress: float) -> Dict:
        """高斯变异（变异率随代数递减）"""
        mutation_rate = 0.2 * (1 - progress)
        for key in individual:
            if random.random() < mutation_rate:
                lo, hi = self._param_bounds[key]
                sigma = (hi - lo) * 0.1
                individual[key] += random.gauss(0, sigma)
                individual[key] = max(lo, min(hi, individual[key]))
        return individual

    def _apply_change_limits(self, params: Dict) -> Dict:
        """应用变更限制（单次≤20%）"""
        # 从数据库加载当前参数
        # 此处简化：不做硬限制，由调用方控制
        return params

    def suggest_parameter_changes(self, current_params: Dict, optimized_params: Dict) -> List[Dict]:
        """生成参数调整建议"""
        suggestions = []
        for key in optimized_params:
            if key in current_params:
                old = current_params[key]
                new = optimized_params[key]
                change = (new - old) / abs(old) if old != 0 else 1.0
                if abs(change) > 0.05:
                    suggestions.append({
                        'parameter': key,
                        'current': old,
                        'suggested': new,
                        'change_pct': round(change * 100, 1),
                        'capped': abs(change) > 0.2,
                    })
        return suggestions
