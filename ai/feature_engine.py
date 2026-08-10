"""
猎场系统 V6.0 — 特征工程管道
从日K线数据提取 128 维特征向量，供 AI 模型使用

特征分组:
  技术面 (64维): 价格/均线/RSI/MACD/布林/波动率/动量
  资金面 (24维): 成交量/换手率/量比/成交额/北向资金
  情绪面 (16维): 恐贪指数/NLP评分/涨跌比
  基本面 (24维): 市值/PE/PB/行业/ST标记
"""

import numpy as np
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FeatureVector:
    """128维特征向量"""
    values: np.ndarray  # shape: (128,)
    feature_names: List[str]
    metadata: Dict


class FeatureEngine:
    """特征工程管道"""

    FEATURE_DIM = 128

    def __init__(self, config: dict = None):
        self.config = config or {}
        self._feature_names = self._build_feature_names()

    def _build_feature_names(self) -> List[str]:
        names = []
        # 技术面 (0-63)
        tech = ['price', 'change_pct', 'amplitude',
                'ma5_dev', 'ma20_dev', 'ma60_dev',
                'rsi_14', 'rsi_6',
                'macd_dif', 'macd_dea', 'macd_hist', 'macd_cross',
                'boll_width', 'boll_position',
                'volatility_5', 'volatility_20', 'volatility_60',
                'momentum_5', 'momentum_10', 'momentum_20',
                'max_drawdown_20', 'max_drawdown_60',
                'volume_ratio', 'turnover_rate',
                'close_position_5', 'close_position_20', 'close_position_60',
                'volume_ma5', 'volume_ma20',
                'high_low_ratio',
                'up_days_ratio_5', 'up_days_ratio_20',
                'price_acceleration',
                'volume_acceleration',
                'rsi_divergence',
                'ma_convergence',
                'bollinger_squeeze']
        tech += [f'tech_pad_{i}' for i in range(64 - len(tech))]
        names.extend(tech)

        # 资金面 (64-87)
        fund = ['volume_trend_5', 'volume_trend_20',
                'amount_ma5', 'amount_ma20',
                'turnover_trend_5', 'turnover_trend_20',
                'vr_trend', 'vr_vs_ma5',
                'big_order_ratio',
                'north_flow_inflow', 'north_flow_days',
                'volume_price_corr']
        fund += [f'fund_pad_{i}' for i in range(24 - len(fund))]
        names.extend(fund)

        # 情绪面 (88-103)
        sent = ['sentiment_score', 'fear_greed_index',
                'advance_decline', 'limit_up_count', 'limit_down_count',
                'news_positive_ratio', 'news_negative_count',
                'social_sentiment',
                'sector_strength', 'sector_rank']
        sent += [f'sent_pad_{i}' for i in range(16 - len(sent))]
        names.extend(sent)

        # 基本面 (104-127)
        fundam = ['market_cap_log', 'pe_ratio', 'pb_ratio',
                  'is_st', 'is_gem', 'is_star',
                  'industry_code', 'list_years',
                  'revenue_growth', 'profit_growth',
                  'debt_ratio', 'roe']
        fundam += [f'fundam_pad_{i}' for i in range(24 - len(fundam))]
        names.extend(fundam)

        return names

    def extract(self, ts_code: str, kline: List[Dict],
                stock_info: Dict = None, market_data: Dict = None) -> Optional[FeatureVector]:
        """
        从K线数据提取128维特征向量

        Args:
            ts_code: 股票代码
            kline: 日K线列表（至少60条）
            stock_info: 股票基本信息
            market_data: 市场数据（恐贪指数等）

        Returns:
            FeatureVector or None
        """
        if len(kline) < 20:
            return None

        closes = np.array([r['close'] for r in kline], dtype=np.float64)
        opens = np.array([r['open'] for r in kline], dtype=np.float64)
        highs = np.array([r['high'] for r in kline], dtype=np.float64)
        lows = np.array([r['low'] for r in kline], dtype=np.float64)
        volumes = np.array([r.get('volume', 0) or 0 for r in kline], dtype=np.float64)
        amounts = np.array([r.get('amount', 0) or 0 for r in kline], dtype=np.float64)

        features = np.zeros(self.FEATURE_DIM, dtype=np.float32)

        # === 技术面特征 (0-63) ===
        idx = 0

        # 价格特征
        features[idx] = closes[-1]; idx += 1  # price
        features[idx] = (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else 0; idx += 1  # change_pct
        features[idx] = (highs[-1] / lows[-1] - 1) * 100 if lows[-1] > 0 else 0; idx += 1  # amplitude

        # 均线偏离
        for period, pos in [(5, 20), (20, 21), (60, 22)]:
            if len(closes) >= period:
                ma = np.mean(closes[-period:])
                features[pos] = (closes[-1] / ma - 1) * 100 if ma > 0 else 0
            idx += 1

        # RSI
        rsi = np.array([r.get('rsi_14', 50) or 50 for r in kline], dtype=np.float32)
        features[idx] = rsi[-1] / 100; idx += 1  # rsi_14
        features[idx] = rsi[-6:].mean() / 100 if len(rsi) >= 6 else 0.5; idx += 1  # rsi_6

        # MACD
        macd_hist = np.array([r.get('macd_hist', 0) or 0 for r in kline], dtype=np.float32)
        features[idx] = (kline[-1].get('macd_dif', 0) or 0) / closes[-1] * 100; idx += 1
        features[idx] = (kline[-1].get('macd_dea', 0) or 0) / closes[-1] * 100; idx += 1
        features[idx] = macd_hist[-1] / closes[-1] * 100; idx += 1
        features[idx] = 1 if (macd_hist[-2] < 0 and macd_hist[-1] > 0) else 0; idx += 1  # cross

        # 布林带
        boll_mid = kline[-1].get('boll_mid', closes[-1]) or closes[-1]
        boll_width = (kline[-1].get('boll_upper', 0) or 0) - (kline[-1].get('boll_lower', 0) or 0)
        features[idx] = boll_width / boll_mid if boll_mid > 0 else 0; idx += 1
        features[idx] = (closes[-1] - (kline[-1].get('boll_lower', closes[-1]) or closes[-1])) / (boll_width + 1e-8); idx += 1

        # 波动率
        for period, col in [(5, 0), (20, 1), (60, 2)]:
            if len(closes) >= period:
                returns = np.diff(closes[-period:]) / closes[-period:-1]
                features[idx + col] = np.std(returns) * np.sqrt(252)
            idx += 1
        idx += 1

        # 动量
        for period, gap in [(5, 0), (10, 1), (20, 2)]:
            if len(closes) > period:
                features[idx + gap] = (closes[-1] / closes[-period-1] - 1) * 100
            idx += 1
        idx += 1

        # 最大回撤
        for period, gap in [(20, 0), (60, 1)]:
            dd = np.array([r.get(f'max_drawdown_{period}', 0) or 0 for r in kline], dtype=np.float32)
            features[idx + gap] = dd[-1] if len(dd) > 0 else 0
            idx += 1
        idx += 1

        # 量比和换手率
        features[idx] = kline[-1].get('volume_ratio', 1.0) or 1.0; idx += 1
        features[idx] = (kline[-1].get('turnover_rate', 0) or 0) / 100; idx += 1

        # 收盘价位置
        for period, gap in [(5, 0), (20, 1), (60, 2)]:
            if len(closes) >= period:
                window = closes[-period:]
                high, low = np.max(window), np.min(window)
                features[idx + gap] = (closes[-1] - low) / (high - low + 1e-8)
            idx += 1
        idx += 1

        # 成交量特征
        features[idx] = volumes[-5:].mean() / volumes[-20:].mean() if len(volumes) >= 20 else 1; idx += 1
        features[idx] = volumes[-20:].mean(); idx += 1

        # 上涨天数比例
        for period, col in [(5, 0), (20, 1)]:
            changes = np.diff(closes[-period-1:]) if len(closes) > period else np.array([0])
            features[idx + col] = np.sum(changes > 0) / max(len(changes), 1)
            idx += 1
        idx += 1

        features[idx] = (features[16] - features[17]) if len(closes) >= 100 else 0; idx += 1  # price_acc
        features[idx] = (features[32] - features[33]) if len(closes) >= 60 else 0; idx += 1  # vol_acc
        features[idx] = 0.0; idx += 1  # rsi_divergence (placeholder)
        features[idx] = 0.0; idx += 1  # ma_convergence
        features[idx] = 0.0; idx += 1  # bollinger_squeeze

        # Padding to 64
        idx = 64

        # === 资金面特征 (64-87) ===
        features[idx] = volumes[-5:].mean() / (volumes[-20:].mean() + 1e-8) - 1; idx += 1
        features[idx] = volumes[-20:].mean() / (volumes[-40:].mean() + 1e-8) - 1 if len(volumes) >= 40 else 0; idx += 1
        features[idx] = amounts[-5:].mean(); idx += 1
        features[idx] = amounts[-20:].mean(); idx += 1
        turnovers = np.array([r.get('turnover_rate', 0) or 0 for r in kline], dtype=np.float32)
        features[idx] = turnovers[-5:].mean() / (turnovers[-20:].mean() + 1e-8) - 1 if len(turnovers) >= 20 else 0; idx += 1
        features[idx] = turnovers[-20:].mean(); idx += 1
        vrs = np.array([r.get('volume_ratio', 1.0) or 1.0 for r in kline], dtype=np.float32)
        features[idx] = vrs[-5:].mean(); idx += 1
        features[idx] = vrs[-1] / (vrs[-5:].mean() + 1e-8); idx += 1
        features[idx] = 0.0; idx += 1  # big_order_ratio
        features[idx] = 0.0; idx += 1  # north_flow_inflow
        features[idx] = 0.0; idx += 1  # north_flow_days

        # Volume-price correlation
        if len(closes) >= 20:
            price_changes = np.diff(closes[-20:])
            vol_changes = np.diff(volumes[-20:])
            corr = np.corrcoef(price_changes, vol_changes)[0, 1] if len(price_changes) > 1 else 0
            features[idx] = 0 if np.isnan(corr) else corr
        idx += 1

        idx = 88  # padding to next section

        # === 情绪面特征 (88-103) ===
        if market_data:
            features[idx] = market_data.get('sentiment', 0); idx += 1
            features[idx] = market_data.get('fear_greed', 50) / 100; idx += 1
            features[idx] = market_data.get('advance_decline', 1.0); idx += 1
            features[idx] = market_data.get('limit_up', 0) / 5000; idx += 1
            features[idx] = market_data.get('limit_down', 0) / 5000; idx += 1
        else:
            idx += 5

        features[idx] = 0.5; idx += 1  # news_positive_ratio default
        features[idx] = 0.0; idx += 1  # news_negative_count
        features[idx] = 0.0; idx += 1  # social_sentiment
        features[idx] = 0.0; idx += 1  # sector_strength
        features[idx] = 0.5; idx += 1  # sector_rank

        idx = 104

        # === 基本面特征 (104-127) ===
        if stock_info:
            mc = stock_info.get('market_cap', 0) or 0
            features[idx] = np.log10(mc + 1); idx += 1  # market_cap_log
            features[idx] = stock_info.get('pe', 0) or 0; idx += 1
            features[idx] = stock_info.get('pb', 0) or 0; idx += 1
            features[idx] = 1 if stock_info.get('is_st', 0) else 0; idx += 1
            features[idx] = 1 if ts_code.startswith('3') else 0; idx += 1  # GEM
            features[idx] = 1 if ts_code.startswith('688') else 0; idx += 1  # STAR
            features[idx] = hash(stock_info.get('industry', '')) % 100 / 100; idx += 1
            features[idx] = min(stock_info.get('list_years', 5) / 30, 1.0); idx += 1
        else:
            idx += 8
        idx = 127  # pad remaining

        return FeatureVector(
            values=features.astype(np.float32),
            feature_names=self._feature_names,
            metadata={'ts_code': ts_code},
        )

    def extract_batch(self, ts_codes: List[str], kline_map: Dict[str, List[Dict]],
                      stock_info_map: Dict[str, Dict] = None,
                      market_data: Dict = None) -> Dict[str, FeatureVector]:
        """批量提取特征"""
        results = {}
        stock_info_map = stock_info_map or {}
        for ts_code in ts_codes:
            kline = kline_map.get(ts_code, [])
            info = stock_info_map.get(ts_code)
            fv = self.extract(ts_code, kline, info, market_data)
            if fv is not None:
                results[ts_code] = fv
        return results

    def to_array(self, features: Dict[str, FeatureVector]) -> np.ndarray:
        """将特征字典转换为numpy数组"""
        if not features:
            return np.array([])
        return np.stack([fv.values for fv in features.values()])
