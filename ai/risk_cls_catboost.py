"""
猎场系统 V6.0 — CatBoost 风险分类模型
预测：未来5日下跌>5%的概率
用途：买入前的最后一道AI防线（V5.2 十二层防线 L11）
"""

import logging
import numpy as np
from typing import Optional
from pathlib import Path
from datetime import datetime
import pickle

logger = logging.getLogger(__name__)


class CatBoostRiskClassifier:
    """
    风险分类器
    预测未来5日是否会出现>5%的大跌
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._model = None
        self._model_path = Path("data/models/catboost_risk.pkl")
        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        self._trained = False

        cfg = self.config.get("ai_models", {}).get("catboost", {})
        self._risk_threshold = cfg.get("risk_threshold", 0.60)

    @property
    def is_trained(self) -> bool:
        return self._trained

    def predict_risk(self, features: np.ndarray) -> float:
        """
        预测大跌风险

        Args:
            features: 128维特征向量

        Returns:
            下跌>5%的概率 (0.0 ~ 1.0)
        """
        if not self._trained:
            return self._rule_based_risk(features)

        try:
            if features.ndim == 1:
                features = features.reshape(1, -1)
            proba = self._model.predict_proba(features)[0, 1]
            return float(proba)
        except Exception as e:
            logger.error(f"CatBoost predict error: {e}")
            return self._rule_based_risk(features)

    def is_high_risk(self, features: np.ndarray) -> bool:
        """是否高风险（建议拒绝买入）"""
        risk = self.predict_risk(features)
        return risk >= self._risk_threshold

    def _rule_based_risk(self, features: np.ndarray) -> float:
        """规则风险评估（fallback）"""
        if features.ndim == 1:
            features = features.reshape(1, -1)

        f = features[0]
        risk = 0.5  # 基准50%

        # RSI过高 → 高风险
        rsi = f[6] * 100  # denormalized
        if rsi > 70:
            risk += (rsi - 70) * 0.01

        # 跌破均线 → 高风险
        if f[2] < -0.03:  # ma20 deviation
            risk += abs(f[2]) * 2

        # 高换手率下跌 → 高风险
        turnover = f[19]
        change = f[1]
        if turnover > 0.05 and change < -0.02:
            risk += 0.2

        # 近期已经跌了不少 → 高风险
        momentum_5 = f[15]
        if momentum_5 < -5:
            risk += 0.15

        # ST股票 → 极高风险
        if f[107] > 0:
            risk += 0.3

        return min(risk, 0.95)

    def train(self, X: np.ndarray, y: np.ndarray):
        """
        训练风险分类模型

        Args:
            X: 特征矩阵 (N, 128)
            y: 标签 1 = 未来5日跌>5%, 0 = 没有大跌
        """
        try:
            from catboost import CatBoostClassifier

            cfg = self.config.get("ai_models", {}).get("catboost", {})
            iterations = cfg.get("iterations", 300)

            self._model = CatBoostClassifier(
                iterations=iterations,
                learning_rate=0.05,
                depth=6,
                loss_function='Logloss',
                eval_metric='AUC',
                random_seed=42,
                verbose=False,
            )

            self._model.fit(X, y)
            self._trained = True
            self.save()
            logger.info(f"CatBoost trained: {len(X)} samples, AUC: {self._model.best_score_['learn']['AUC']:.3f}")

        except ImportError:
            logger.warning("catboost not installed, using rule-based risk")
        except Exception as e:
            logger.error(f"CatBoost training error: {e}")

    def save(self):
        if self._model:
            with open(self._model_path, 'wb') as f:
                pickle.dump({
                    'model': self._model,
                    'trained_at': datetime.now().isoformat(),
                }, f)

    def load(self) -> bool:
        if not self._model_path.exists():
            return False
        try:
            with open(self._model_path, 'rb') as f:
                data = pickle.load(f)
            self._model = data['model']
            self._trained = True
            logger.info(f"CatBoost model loaded from {self._model_path}")
            return True
        except Exception as e:
            logger.error(f"CatBoost load error: {e}")
            return False

    def get_risk_threshold(self) -> float:
        return self._risk_threshold
