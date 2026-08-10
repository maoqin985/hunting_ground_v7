"""
猎场系统 V6.0 — PPO 强化学习仓位优化
状态: 当前持仓/现金/市场状态/信号评分 → 动作: 仓位比例 0%-70%
"""

import logging
import numpy as np
from typing import Dict, Optional, Tuple
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


class TradingEnv:
    """
    简化交易环境（用于 RL 仓位优化训练）
    基于 OpenAI Gym 接口
    """

    def __init__(self, initial_capital: float = 20000.0):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.position = 0.0       # 当前持仓市值
        self.position_pct = 0.0   # 仓位比例
        self.done = False
        self.step_count = 0
        self.entry_price = 0.0
        self.max_position_pct = 0.70

    def reset(self, market_state: np.ndarray = None) -> np.ndarray:
        """重置环境"""
        self.capital = self.initial_capital
        self.position = 0.0
        self.position_pct = 0.0
        self.done = False
        self.step_count = 0
        self.entry_price = 0.0
        return self._get_obs(market_state)

    def step(self, action: float, market_state: np.ndarray,
             price_change: float) -> Tuple[np.ndarray, float, bool, Dict]:
        """
        执行一步

        Args:
            action: 目标仓位比例 (0.0 ~ 0.70)
            market_state: 市场状态（19维）
            price_change: 本步价格变化率

        Returns:
            (observation, reward, done, info)
        """
        self.step_count += 1
        target_position_pct = np.clip(action, 0.0, self.max_position_pct)

        # 计算调仓
        total_value = self.capital + self.position
        target_position = total_value * target_position_pct

        # 手续费
        trade_amount = abs(target_position - self.position)
        fee = trade_amount * 0.00075  # 买卖双向0.075%

        # 更新
        self.position = target_position * (1 + price_change)
        self.capital = total_value - self.position - fee
        self.position_pct = target_position_pct

        # 奖励函数：风险调整收益
        new_total = self.capital + self.position
        pnl = new_total - self.initial_capital
        pnl_pct = pnl / self.initial_capital

        # 惩罚过度回撤
        max_dd_penalty = 0
        if pnl_pct < -0.05:
            max_dd_penalty = (abs(pnl_pct) - 0.05) * 10

        # 奖励 = 收益 - 波动惩罚 - 回撤惩罚
        reward = pnl_pct * 100 - abs(target_position_pct - self.position_pct) * 2 - max_dd_penalty

        # 终止条件
        if pnl_pct <= -0.10:  # 亏损10%终止
            self.done = True
            reward -= 50

        return self._get_obs(market_state), reward, self.done, {
            'pnl_pct': pnl_pct,
            'position_pct': self.position_pct,
            'total_value': self.capital + self.position,
        }

    def _get_obs(self, market_state: np.ndarray) -> np.ndarray:
        """构造观测向量"""
        obs = np.zeros(19, dtype=np.float32)
        obs[0] = self.position_pct
        obs[1] = self.capital / self.initial_capital
        obs[2] = (self.capital + self.position) / self.initial_capital - 1.0  # PnL%
        if market_state is not None and len(market_state) >= 16:
            obs[3:19] = market_state[:16]
        return obs


class RLPredictor:
    """PPO 仓位优化推理器"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._model = None
        self._model_path = Path("data/models/ppo_position.zip")
        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        self._trained = False

    @property
    def is_trained(self) -> bool:
        return self._trained

    def predict(self, signal_score: float, market_state: np.ndarray = None,
                current_position_pct: float = 0.0,
                capital_ratio: float = 1.0, pnl_pct: float = 0.0) -> float:
        """
        预测最优仓位比例

        Args:
            signal_score: 信号评分 (0-100)
            market_state: 市场状态数组
            current_position_pct: 当前仓位
            capital_ratio: 资金比例 (1.0 = 满资金)
            pnl_pct: 当前盈亏%

        Returns:
            建议仓位比例 (0.0 ~ 0.70)
        """
        if not self._trained:
            return self._rule_based_sizing(signal_score, current_position_pct, pnl_pct)

        try:
            # 构造观测
            obs = np.zeros(19, dtype=np.float32)
            obs[0] = current_position_pct
            obs[1] = capital_ratio
            obs[2] = pnl_pct
            obs[3] = signal_score / 100.0
            if market_state is not None:
                n = min(len(market_state), 15)
                obs[4:4 + n] = market_state[:n]

            action, _ = self._model.predict(obs, deterministic=True)
            return float(np.clip(action[0], 0.0, 0.70))

        except Exception as e:
            logger.error(f"RL predict error: {e}")
            return self._rule_based_sizing(signal_score, current_position_pct, pnl_pct)

    def _rule_based_sizing(self, signal_score: float, current_pct: float, pnl_pct: float) -> float:
        """规则仓位计算（RL未训练时的fallback）"""
        # Kelly 启发式
        if signal_score >= 70:
            base_pct = 0.50
        elif signal_score >= 50:
            base_pct = 0.35
        elif signal_score >= 40:
            base_pct = 0.20
        else:
            return 0.0

        # 亏损时减仓
        if pnl_pct < -0.03:
            base_pct *= 0.5
        elif pnl_pct < -0.01:
            base_pct *= 0.7

        # 不超过最大仓位
        return min(base_pct, 0.70)

    def load(self) -> bool:
        """加载RL模型"""
        if not self._model_path.exists():
            return False
        try:
            from stable_baselines3 import PPO
            self._model = PPO.load(str(self._model_path))
            self._trained = True
            logger.info(f"PPO model loaded from {self._model_path}")
            return True
        except ImportError:
            logger.warning("stable-baselines3 not installed")
        except Exception as e:
            logger.error(f"PPO load error: {e}")
        return False


class RLTrainer:
    """PPO 训练器"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.env = TradingEnv(
            initial_capital=self.config.get('capital', {}).get('total', 20000.0)
        )

    def train(self, market_data: np.ndarray, timesteps: int = 100000):
        """训练PPO仓位优化模型"""
        try:
            from stable_baselines3 import PPO
            from stable_baselines3.common.vec_env import DummyVecEnv

            env = DummyVecEnv([lambda: self.env])

            model = PPO(
                'MlpPolicy',
                env,
                learning_rate=3e-4,
                n_steps=2048,
                batch_size=64,
                n_epochs=10,
                gamma=0.99,
                gae_lambda=0.95,
                clip_range=0.2,
                verbose=0,
            )

            model.learn(total_timesteps=timesteps)
            model.save("data/models/ppo_position")
            logger.info(f"PPO trained: {timesteps} timesteps")

            return model

        except ImportError:
            logger.warning("stable-baselines3 not installed, using rule-based sizing")
        except Exception as e:
            logger.error(f"PPO training error: {e}")
        return None
