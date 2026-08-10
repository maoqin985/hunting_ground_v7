"""
猎场系统 V6.0 — AI 模型集成投票
整合 XGBoost + LSTM + CatBoost + 信号融合，输出最终决策
"""

import logging
import numpy as np
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class ModelEnsemble:
    """
    AI 模型集成引擎
    借鉴 Qbot 多模型信号融合 + 置信度加权投票
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._scorer = None          # XGBoostScorer
        self._predictor = None       # LSTMPredictor
        self._position_rl = None     # RLPredictor
        self._risk_cls = None        # CatBoostRiskClassifier
        self._macro_llm = None       # MacroLLM
        self._initialized = False

    def initialize(self, scorer, predictor, position_rl, risk_cls, macro_llm):
        """注入各模型实例"""
        self._scorer = scorer
        self._predictor = predictor
        self._position_rl = position_rl
        self._risk_cls = risk_cls
        self._macro_llm = macro_llm
        self._initialized = True
        logger.info("ModelEnsemble initialized with all models")

    def decide(self, ts_code: str, feature_vector, kline: List[Dict],
               signal_score: float, current_position_pct: float = 0.0,
               portfolio_pnl_pct: float = 0.0, market_data: Dict = None) -> Dict:
        """
        综合所有模型，输出最终决策

        Returns:
            {
                'ts_code': str,
                'action': 'buy' / 'hold' / 'sell',
                'score': 0-100,
                'confidence': 0-1,
                'suggested_position_pct': 0-0.70,
                'stop_loss_pct': -0.05,
                'expected_hold_days': 3,
                'risk_level': 'low' / 'medium' / 'high',
                'model_votes': {...},
                'reasoning': str,
            }
        """
        if not self._initialized:
            return self._fallback_decision(ts_code, signal_score, current_position_pct)

        # 1. XGBoost 评分
        xgb_score = signal_score
        if self._scorer and self._scorer.is_trained and feature_vector is not None:
            xgb_score = self._scorer.predict_single(feature_vector)

        # 2. LSTM 预测
        lstm_result = {'direction': 'up', 'confidence': 0.5, 'signal': False}
        if self._predictor:
            lstm_result = self._predictor.predict(kline)

        # 3. CatBoost 风险检查
        risk_prob = 0.5
        if self._risk_cls and feature_vector is not None:
            risk_prob = self._risk_cls.predict_risk(feature_vector.values)

        # 4. PPO 仓位建议
        suggested_pct = 0.0
        if self._position_rl:
            market_obs = self._build_market_obs(market_data) if market_data else None
            suggested_pct = self._position_rl.predict(
                xgb_score, market_obs, current_position_pct, 1.0, portfolio_pnl_pct
            )

        # 5. 集成投票
        votes = {
            'xgb_score': xgb_score,
            'lstm_direction': lstm_result['direction'],
            'lstm_confidence': lstm_result['confidence'],
            'risk_prob': risk_prob,
            'suggested_position_pct': suggested_pct,
        }

        # 决策逻辑
        action = 'hold'
        confidence = 0.5

        if risk_prob >= 0.60:
            # AI 风险模型拒绝
            action = 'hold'
            confidence = 1 - risk_prob
            reasoning = f"AI风险模型预警（大跌概率{risk_prob:.0%}），建议观望"
        elif xgb_score < 40:
            action = 'hold'
            confidence = xgb_score / 100
            reasoning = f"XGBoost评分偏低（{xgb_score:.0f}分），不满足买入条件"
        elif xgb_score >= 50 and lstm_result['direction'] == 'up' and risk_prob < 0.45:
            action = 'buy'
            confidence = min(
                xgb_score / 100,
                lstm_result['confidence'],
                1 - risk_prob,
            )
            reasoning = f"三模型一致看多：XGB={xgb_score:.0f}分, LSTM看涨(置信{lstm_result['confidence']:.0%}), 风险{risk_prob:.0%}"
        elif xgb_score >= 70 and lstm_result['direction'] == 'down':
            # 模型分歧
            action = 'hold'
            confidence = 0.4
            reasoning = f"模型分歧：XGBoost强烈看多({xgb_score:.0f}分)但LSTM看跌，建议观望"
        else:
            reasoning = f"模型信号不满足入场条件"

        # 调整仓位建议
        if action == 'hold':
            suggested_pct = 0.0
        elif action == 'sell':
            suggested_pct = 0.0

        return {
            'ts_code': ts_code,
            'action': action,
            'score': xgb_score,
            'confidence': round(confidence, 2),
            'suggested_position_pct': round(suggested_pct, 2),
            'stop_loss_pct': -0.05,
            'expected_hold_days': self._estimate_hold_days(lstm_result),
            'risk_level': 'high' if risk_prob > 0.5 else ('medium' if risk_prob > 0.35 else 'low'),
            'model_votes': votes,
            'reasoning': reasoning,
            'timestamp': datetime.now().isoformat(),
        }

    def _fallback_decision(self, ts_code: str, signal_score: float,
                           current_pct: float) -> Dict:
        """模型未初始化时的fallback决策"""
        if signal_score >= 60:
            action = 'buy'
            confidence = min(signal_score / 100, 0.7)
            suggested_pct = 0.35 if signal_score >= 70 else 0.20
        elif signal_score >= 40:
            action = 'hold'
            confidence = 0.5
            suggested_pct = 0.0
        else:
            action = 'hold'
            confidence = 0.6
            suggested_pct = 0.0

        return {
            'ts_code': ts_code,
            'action': action,
            'score': signal_score,
            'confidence': confidence,
            'suggested_position_pct': suggested_pct,
            'stop_loss_pct': -0.05,
            'expected_hold_days': 3,
            'risk_level': 'medium',
            'model_votes': {},
            'reasoning': '规则决策（AI模型未训练）',
            'timestamp': datetime.now().isoformat(),
        }

    def _build_market_obs(self, market_data: Dict) -> np.ndarray:
        obs = np.zeros(16, dtype=np.float32)
        obs[0] = market_data.get('fear_greed', 50) / 100
        obs[1] = market_data.get('hs300_change', 0) / 100
        obs[2] = market_data.get('advance_decline', 1.0)
        obs[3] = market_data.get('limit_up', 0) / 5000
        obs[4] = market_data.get('limit_down', 0) / 5000
        return obs

    def _estimate_hold_days(self, lstm_result: Dict) -> int:
        """估计持有天数"""
        preds = lstm_result.get('predictions', {})
        if preds.get(1, 0) > preds.get(5, 0):
            return 2  # 短线
        elif preds.get(5, 0) > 0.03:
            return 8  # 中线
        return 5
