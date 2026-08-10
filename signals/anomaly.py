"""
猎场系统 V6.0 — AI 异常检测扫描器
孤立森林 (Isolation Forest) 检测异常交易行为
"""

import logging
from typing import List, Dict
import numpy as np
from .base_scanner import BaseScanner

logger = logging.getLogger(__name__)


class AnomalyScanner(BaseScanner):
    """基于孤立森林的异常检测扫描器"""

    def __init__(self, db, config: dict = None):
        super().__init__("anomaly", db, config)
        self._model = None
        self._contamination = 0.05  # 预期5%异常

    def _get_model(self):
        """延迟加载模型"""
        if self._model is None:
            try:
                from sklearn.ensemble import IsolationForest
                self._model = IsolationForest(
                    n_estimators=100,
                    contamination=self._contamination,
                    random_state=42,
                    n_jobs=-1,
                )
                logger.info("IsolationForest model initialized")
            except ImportError:
                logger.warning("scikit-learn not installed")
        return self._model

    async def scan(self, ts_codes: List[str]) -> List[Dict]:
        model = self._get_model()
        if model is None:
            return []

        signals = []
        features_list = []
        valid_codes = []

        # 提取特征
        for ts_code in ts_codes:
            kline = self.db.get_kline(ts_code, limit=30)
            if len(kline) < 20:
                continue

            features = self._extract_features(kline)
            if features is not None:
                features_list.append(features)
                valid_codes.append(ts_code)

        if len(features_list) < 10:
            return signals

        # 孤立森林检测
        try:
            X = np.array(features_list)
            model.fit(X)
            scores = model.decision_function(X)
            predictions = model.predict(X)

            # -1 = 异常，1 = 正常。但我们对"异常上涨"感兴趣（可能是启动信号）
            for i, (pred, score) in enumerate(zip(predictions, scores)):
                if pred == -1 and score < -0.05:
                    # 检查是否是上涨异常
                    kline = self.db.get_kline(valid_codes[i], limit=5)
                    if kline and kline[-1].get('change_pct', 0) or 0 > 0:
                        signals.append({
                            'ts_code': valid_codes[i],
                            'name': '',
                            'signal_type': 'anomaly',
                            'score': min(abs(score) * 200, 60),
                            'direction': 'buy',
                            'reason': f'AI异常检测(score={score:.3f})—价格行为异于常态',
                            'details': {
                                'anomaly_score': float(score),
                                'prediction': int(pred),
                            },
                        })
        except Exception as e:
            logger.error(f"IsolationForest error: {e}")

        signals.sort(key=lambda s: s['score'], reverse=True)
        return signals

    def _extract_features(self, kline: List[Dict]) -> List[float]:
        """提取异常检测特征"""
        closes = [r['close'] for r in kline]
        volumes = [r.get('volume', 0) or 0 for r in kline]
        changes = [r.get('change_pct', 0) or 0 for r in kline]
        turnovers = [r.get('turnover_rate', 0) or 0 for r in kline]

        if not closes:
            return None

        try:
            returns = np.diff(closes) / closes[:-1]

            features = [
                np.mean(returns[-10:]) if len(returns) >= 10 else 0,     # 近期平均收益
                np.std(returns[-10:]) if len(returns) >= 10 else 0,      # 波动率
                np.mean(volumes[-5:]) / np.mean(volumes[-20:]) if len(volumes) >= 20 else 1,  # 量比
                np.mean(changes[-5:]),                                     # 近期平均涨跌
                np.max(changes[-10:]) - np.min(changes[-10:]),            # 振幅
                np.mean(turnovers[-5:]) if len(turnovers) >= 5 else 0,    # 平均换手率
                (closes[-1] / np.max(closes[-20:]) - 1) if len(closes) >= 20 else 0,  # 距高点回撤
                len([c for c in changes[-5:] if c > 0]) / 5.0,           # 近期阳线比例
            ]
            return features
        except Exception:
            return None
