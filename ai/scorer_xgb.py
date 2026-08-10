"""
猎场系统 V6.0 — XGBoost 信号评分模型
输入: 128维特征向量 → 输出: 0-100 买入信号评分
"""

import logging
import numpy as np
from typing import List, Tuple, Optional
from pathlib import Path
import pickle
from datetime import datetime

logger = logging.getLogger(__name__)


class XGBoostScorer:
    """
    XGBoost 信号评分器
    用于替代 V5.2 的固定规则评分，实现动态权重
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._model = None
        self._model_path = Path("data/models/xgb_scorer.pkl")
        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        self._trained = False
        self._feature_importance = {}

        cfg = self.config.get("ai_models", {}).get("xgboost", {})
        self._score_threshold = cfg.get("score_threshold", 40)

    @property
    def is_trained(self) -> bool:
        return self._trained

    def predict(self, features: np.ndarray) -> np.ndarray:
        """
        预测买入信号评分 (0-100)

        Args:
            features: shape (N, 128) or (128,)

        Returns:
            scores: shape (N,) or scalar
        """
        if not self._trained:
            # 未训练时使用规则评分作为fallback
            return self._rule_based_score(features)

        if features.ndim == 1:
            features = features.reshape(1, -1)

        raw_scores = self._model.predict(features)
        # 映射到 0-100
        scores = 1 / (1 + np.exp(-raw_scores)) * 100
        return np.clip(scores, 0, 100)

    def predict_single(self, feature_vector) -> float:
        """预测单个特征向量的评分"""
        scores = self.predict(feature_vector.values)
        return float(scores[0]) if len(scores) > 0 else 0.0

    def train(self, X: np.ndarray, y: np.ndarray, X_val=None, y_val=None):
        """
        训练模型

        Args:
            X: 特征矩阵 (N, 128)
            y: 标签。1 = 未来N日盈利>2%, 0 = 亏损或横盘
        """
        try:
            import xgboost as xgb

            cfg = self.config.get("ai_models", {}).get("xgboost", {})
            params = {
                'n_estimators': cfg.get('n_estimators', 200),
                'max_depth': cfg.get('max_depth', 6),
                'learning_rate': cfg.get('learning_rate', 0.05),
                'objective': 'binary:logistic',
                'eval_metric': 'auc',
                'subsample': 0.8,
                'colsample_bytree': 0.8,
                'random_state': 42,
                'n_jobs': -1,
            }

            if X_val is not None and y_val is not None:
                self._model = xgb.XGBClassifier(**params)
                self._model.fit(
                    X, y,
                    eval_set=[(X_val, y_val)],
                    verbose=False,
                )
            else:
                self._model = xgb.XGBClassifier(**params)
                self._model.fit(X, y, verbose=False)

            self._trained = True

            # 保存特征重要性
            importance = self._model.feature_importances_
            self._feature_importance = {
                i: imp for i, imp in enumerate(importance) if imp > 0.001
            }

            # 保存模型
            self.save()

            logger.info(f"XGBoost trained: {len(X)} samples, "
                        f"top feature importance: {sorted(self._feature_importance.items(), key=lambda x: -x[1])[:5]}")

        except ImportError:
            logger.warning("xgboost not installed, using rule-based scoring")
        except Exception as e:
            logger.error(f"XGBoost training error: {e}")

    def _rule_based_score(self, features: np.ndarray) -> np.ndarray:
        """规则评分（AI模型未训练时的fallback）"""
        if features.ndim == 1:
            features = features.reshape(1, -1)

        scores = np.zeros(len(features))
        for i in range(len(features)):
            f = features[i]
            score = 50.0  # 基准分

            # RSI超卖加分
            rsi = f[6] * 100  # rsi_14 (denormalized)
            if rsi < 30:
                score += (30 - rsi) * 0.5
            elif rsi > 70:
                score -= (rsi - 70) * 0.5

            # MACD金叉加分
            if f[9] > 0:  # macd_cross
                score += 15

            # 布林下轨加分
            boll_pos = f[13]  # boll_position
            if boll_pos < 0.2:
                score += 15

            # 量比加分
            vr = f[18]  # volume_ratio
            if vr > 1.5:
                score += 10

            # 负面情绪减分
            if f[88] < -0.3:  # sentiment
                score -= 20

            scores[i] = np.clip(score, 0, 100)

        return scores

    def save(self):
        """保存模型"""
        if self._model:
            with open(self._model_path, 'wb') as f:
                pickle.dump({
                    'model': self._model,
                    'feature_importance': self._feature_importance,
                    'trained_at': datetime.now().isoformat(),
                }, f)
            logger.info(f"Model saved to {self._model_path}")

    def load(self) -> bool:
        """加载模型"""
        if self._model_path.exists():
            try:
                with open(self._model_path, 'rb') as f:
                    data = pickle.load(f)
                self._model = data['model']
                self._feature_importance = data.get('feature_importance', {})
                self._trained = True
                logger.info(f"Model loaded from {self._model_path}")
                return True
            except Exception as e:
                logger.error(f"Model load error: {e}")
        return False

    def get_threshold(self) -> float:
        return self._score_threshold

    def get_feature_importance(self) -> dict:
        return self._feature_importance
