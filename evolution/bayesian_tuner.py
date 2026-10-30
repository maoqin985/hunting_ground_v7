"""
猎场系统 V7.0 — 贝叶斯优化器 (GP-UCB)
作为遗传算法的备选方案，两优化器结果对比，优者胜出
"""

import logging
import numpy as np
from typing import Dict, List, Callable, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class BayesianTuner:
    """
    贝叶斯超参优化 (Gaussian Process Upper Confidence Bound)

    与 GA 配合使用：
    1. GA 优化 (exploration) → 产出候选参数集
    2. Bayesian 优化 (exploitation) → 精细化调参
    3. 两者结果对比 → 优者胜出
    """

    def __init__(self, param_bounds: Dict[str, Tuple[float, float]] = None):
        self._param_bounds = param_bounds or {
            'score_threshold': (30, 65),
            'position_pct': (0.10, 0.65),
            'stop_loss_pct': (-0.10, -0.02),
            'rsi_threshold': (20, 40),
            'volume_ratio_threshold': (1.2, 3.0),
            'trailing_stop_activate': (0.03, 0.10),
            'max_hold_days': (3, 20),
            'max_positions': (3, 10),
        }
        self._param_names = list(self._param_bounds.keys())
        self._n_dims = len(self._param_names)

        # GP 参数
        self._X_observed: List[np.ndarray] = []  # 已评估参数点
        self._y_observed: List[float] = []        # 对应目标值
        self._best_params: Dict = None
        self._best_value: float = -float('inf')

    def optimize(self, objective_fn: Callable[[Dict], float],
                 n_iterations: int = 30,
                 n_initial_random: int = 5,
                 exploration_weight: float = 2.0) -> Dict:
        """
        贝叶斯优化

        Args:
            objective_fn: 目标函数 fn(params_dict) -> fitness
            n_iterations: 总迭代次数
            n_initial_random: 初始随机探索次数
            exploration_weight: UCB 探索权重 (越大越探索)

        Returns:
            {best_params, best_value, history, method: 'bayesian'}
        """
        logger.info(f"Bayesian optimization: {n_iterations} iters, {n_initial_random} random init")

        history = []

        # 1. 随机探索
        for i in range(n_initial_random):
            params = self._random_sample()
            value = self._evaluate_safe(objective_fn, params)
            self._add_observation(params, value)
            history.append({'iter': i, 'phase': 'random', 'value': value})

            if value > self._best_value:
                self._best_value = value
                self._best_params = params

        # 2. GP-UCB 迭代
        for i in range(n_initial_random, n_iterations):
            # 用 GP 预测所有候选点的 UCB
            n_candidates = 200
            candidates = [self._random_sample() for _ in range(n_candidates)]
            candidate_values = []

            for cand in candidates:
                ucbs = self._ucb(cand, exploration_weight)
                candidate_values.append((cand, ucbs))

            # 选最优 UCB
            best_candidate = max(candidate_values, key=lambda x: x[1])
            params = best_candidate[0]

            # 评估
            value = self._evaluate_safe(objective_fn, params)
            self._add_observation(params, value)
            history.append({'iter': i, 'phase': 'gp_ucb', 'value': value, 'ucb': best_candidate[1]})

            if value > self._best_value:
                self._best_value = value
                self._best_params = params

            logger.debug(f"Bayes iter {i + 1}/{n_iterations}: value={value:.4f}, best={self._best_value:.4f}")

        result = {
            'best_params': self._best_params,
            'best_value': self._best_value,
            'history': history,
            'optimized_at': datetime.now().isoformat(),
            'method': 'bayesian_gp_ucb',
            'n_observations': len(self._X_observed),
        }

        logger.info(f"Bayesian optimization complete: best={self._best_value:.4f}")
        return result

    def _ucb(self, params: Dict, kappa: float = 2.0) -> float:
        """计算 Upper Confidence Bound"""
        mean, std = self._gp_predict(params)

        if np.isnan(mean) or np.isinf(mean):
            mean = 0.0
        if np.isnan(std) or np.isinf(std) or std < 0:
            std = 1.0

        return mean + kappa * std

    def _gp_predict(self, params: Dict) -> Tuple[float, float]:
        """
        简化 GP 预测（基于加权平均的近似）

        使用 RBF-like 核：k(x, x') = exp(-||x - x'||^2 / (2 * l^2))
        """
        if not self._X_observed:
            return 0.0, 1.0

        x = self._params_to_array(params)
        length_scale = 0.5  # GP 长度尺度

        # 计算与所有观测点的核
        k_values = []
        for x_obs, y_obs in zip(self._X_observed, self._y_observed):
            sq_dist = np.sum((x - x_obs) ** 2) / length_scale ** 2
            k = np.exp(-0.5 * sq_dist)
            k_values.append(k)

        k_values = np.array(k_values)
        y_array = np.array(self._y_observed)

        # 加权平均 (简化预测均值)
        total_k = np.sum(k_values)
        if total_k < 1e-10:
            return 0.0, 1.0

        mean = np.sum(k_values * y_array) / total_k

        # 预测标准差：与最近观测点的距离成反比
        max_k = np.max(k_values)
        std = np.sqrt(max(0, 1 - max_k))  # 简单近似

        return mean, std

    def _add_observation(self, params: Dict, value: float):
        self._X_observed.append(self._params_to_array(params))
        self._y_observed.append(value)

    def _params_to_array(self, params: Dict) -> np.ndarray:
        """参数归一化到 [0,1]"""
        arr = np.zeros(self._n_dims)
        for i, name in enumerate(self._param_names):
            lo, hi = self._param_bounds[name]
            val = params.get(name, (lo + hi) / 2)
            arr[i] = (val - lo) / (hi - lo) if hi > lo else 0.5
        return arr

    def _array_to_params(self, arr: np.ndarray) -> Dict:
        """归一化数组还原为参数"""
        params = {}
        for i, name in enumerate(self._param_names):
            lo, hi = self._param_bounds[name]
            val = lo + arr[i] * (hi - lo)
            params[name] = round(val, 4)
        return params

    def _random_sample(self) -> Dict:
        return {
            name: round(np.random.uniform(lo, hi), 4)
            for name, (lo, hi) in self._param_bounds.items()
        }

    def _evaluate_safe(self, fn, params: Dict) -> float:
        try:
            result = fn(params)
            if isinstance(result, tuple):
                sharpe, win_rate = result
                return sharpe * max(win_rate, 0.01)
            return float(result) if result is not None else -1.0
        except Exception:
            return -1.0

    def compare_with_ga(self, ga_result: Dict, bayes_result: Dict) -> Dict:
        """对比 GA 和 Bayesian 结果，返回最优"""
        ga_value = ga_result.get('best_fitness', -float('inf'))
        bayes_value = bayes_result.get('best_value', -float('inf'))

        if bayes_value > ga_value:
            winner = 'bayesian'
            best_params = bayes_result['best_params']
            best_value = bayes_value
        else:
            winner = 'ga'
            best_params = ga_result['best_params']
            best_value = ga_value

        return {
            'winner': winner,
            'best_params': best_params,
            'best_value': best_value,
            'ga_value': ga_value,
            'bayes_value': bayes_value,
            'improvement': (best_value - min(ga_value, bayes_value)),
        }
