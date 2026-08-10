"""
猎场系统 V6.0 — 多信号加权融合引擎
整合技术面/资金面/情绪面/AI异常检测信号，输出统一评分
"""

import logging
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class SignalFusion:
    """
    多模态信号融合引擎
    借鉴 Qbot 的多策略信号加权融合机制
    """

    def __init__(self, config: dict = None):
        self.config = config or {}

        # 默认权重（可从配置覆盖）
        weights_cfg = self.config.get("signal_weights", {})
        self.weights = {
            'technical': {
                'rsi_oversold': weights_cfg.get('technical', {}).get('rsi_oversold', 0.15),
                'macd_golden_cross': weights_cfg.get('technical', {}).get('macd_golden_cross', 0.10),
                'bollinger_lower': weights_cfg.get('technical', {}).get('bollinger_lower', 0.10),
                'v_pattern': weights_cfg.get('technical', {}).get('v_pattern', 0.08),
                'bottom_fractal': weights_cfg.get('technical', {}).get('bottom_fractal', 0.07),
            },
            'fund_flow': {
                'north_flow': weights_cfg.get('fund_flow', {}).get('north_flow', 0.12),
                'volume_ratio_anomaly': weights_cfg.get('fund_flow', {}).get('volume_ratio_anomaly', 0.08),
                'turnover_rate_anomaly': weights_cfg.get('fund_flow', {}).get('turnover_rate_anomaly', 0.07),
                'big_order_inflow': weights_cfg.get('fund_flow', {}).get('big_order_inflow', 0.05),
            },
            'sentiment': {
                'nlp_score': weights_cfg.get('sentiment', {}).get('nlp_score', 0.08),
                'fear_greed_index': weights_cfg.get('sentiment', {}).get('fear_greed_index', 0.05),
                'advance_decline_ratio': weights_cfg.get('sentiment', {}).get('advance_decline_ratio', 0.03),
            },
            'ai_enhanced': {
                'anomaly_detection': weights_cfg.get('ai_enhanced', {}).get('anomaly_detection', 0.02),
                'lstm_prediction': weights_cfg.get('ai_enhanced', {}).get('lstm_prediction', 0.02),
            },
        }

    def fuse(self, signal_groups: Dict[str, List[Dict]]) -> Dict[str, Dict]:
        """
        融合所有扫描器信号，按股票代码聚合

        Args:
            signal_groups: {
                'technical': [...signals],
                'fund_flow': [...signals],
                'sentiment': [...signals],
                'anomaly': [...signals],
            }

        Returns:
            {ts_code: {score, signals, confidence, ...}}
        """
        fused = {}

        # 按股票代码聚合
        for source, signals in signal_groups.items():
            for sig in signals:
                ts_code = sig['ts_code']
                if ts_code not in fused:
                    fused[ts_code] = {
                        'ts_code': ts_code,
                        'name': sig.get('name', ''),
                        'signals': [],
                        'total_score': 0,
                        'confidence': 0,
                        'signal_count': 0,
                        'sources': [],
                    }

                fused[ts_code]['signals'].append(sig)
                fused[ts_code]['sources'].append(source)
                fused[ts_code]['signal_count'] += 1

        # 计算融合评分
        for ts_code, data in fused.items():
            score, confidence = self._calculate_fused_score(data['signals'])
            data['total_score'] = score
            data['confidence'] = confidence

        return fused

    def _calculate_fused_score(self, signals: List[Dict]) -> tuple:
        """
        加权融合评分

        Returns:
            (total_score, confidence)
        """
        total_score = 0
        total_weight = 0
        confidence = 0

        for sig in signals:
            source = sig.get('signal_type', '')
            score = sig.get('score', 0)
            reason = sig.get('reason', '')

            # 根据信号内容和来源确定权重
            weight = self._get_signal_weight(source, reason)

            total_score += score * weight
            total_weight += weight

        if total_weight > 0:
            final_score = total_score / total_weight
        else:
            final_score = 0

        # 置信度基于信号数量和一致性
        sig_count = len(signals)
        if sig_count >= 3:
            confidence = min(sig_count * 0.25, 0.9)  # 多信号 = 高置信
        elif sig_count == 2:
            confidence = 0.5
        elif sig_count == 1:
            confidence = 0.3
        else:
            confidence = 0

        # 如果只有AI异常检测信号，降低置信度
        if sig_count == 1 and signals[0].get('signal_type') == 'anomaly':
            confidence = 0.2

        return round(final_score, 1), round(confidence, 2)

    def _get_signal_weight(self, source: str, reason: str) -> float:
        """获取信号权重"""
        # 技术面
        if source == 'technical':
            if 'RSI超卖' in reason:
                return 0.15
            if 'MACD' in reason:
                return 0.10
            if '布林' in reason:
                return 0.10
            if 'V型反转' in reason:
                return 0.08
            return 0.07

        # 资金面
        if source == 'fund_flow':
            if '量比' in reason:
                return 0.08
            if '换手' in reason:
                return 0.07
            if '缩量' in reason:
                return 0.06
            return 0.05

        # 情绪面
        if source == 'sentiment':
            if '新闻' in reason:
                return 0.08
            return 0.05

        # AI
        if source == 'anomaly':
            return 0.04

        return 0.05

    def get_top_signals(self, fused: Dict[str, Dict], top_n: int = 20,
                        min_score: float = 30) -> List[Dict]:
        """获取Top N信号"""
        valid = [
            v for v in fused.values()
            if v['total_score'] >= min_score
        ]
        valid.sort(key=lambda x: x['total_score'], reverse=True)
        return valid[:top_n]

    def get_market_signal(self, fused: Dict[str, Dict]) -> Optional[Dict]:
        """获取市场级别信号"""
        return fused.get('MARKET')
