"""
猎场系统 V6.0 — 示例回测策略
规则化均值回归策略（RSI 超卖），用于打通回测引擎的样本外验证流程。

说明：数据库中的 rsi_14 / change_pct 等指标列当前为占位值（rsi_14=50），
因此本策略从原始 close 价自行计算 RSI(14)（Wilder 平滑），
既避免 look-ahead，又不受占位值污染——这也是「高质量数据」维度的示范：
指标应从原始 OHLCV 现算，而非依赖可能失真的预存字段。
"""

import logging
from typing import Dict, List, Callable
import numpy as np

logger = logging.getLogger(__name__)


def _compute_rsi(closes: List[float], period: int = 14) -> List:
    """Wilder 平滑 RSI，返回与 closes 对齐的序列（前 period 个为 None）。"""
    n = len(closes)
    rsi = [None] * n
    if n < period + 1:
        return rsi
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    rsi[period] = _rsi_from_avg(avg_gain, avg_loss)

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        rsi[i] = _rsi_from_avg(avg_gain, avg_loss)
    return rsi


def _rsi_from_avg(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


class MeanReversionStrategy:
    """
    RSI(14) 超卖买入策略（从原始 close 现算 RSI）。

    预加载每只股票全区间 K 线到内存，回测循环内按日 O(1) 查询。
    """

    def __init__(self, db, universe: List[str], start: str, end: str,
                 top_n: int = 3, rsi_threshold: float = 30.0, position_pct: float = 0.30):
        self.top_n = top_n
        self.rsi_threshold = rsi_threshold
        self.position_pct = position_pct
        self._rsi: Dict[str, Dict[str, float]] = {}  # code -> {trade_date: rsi}

        for code in universe:
            try:
                rows = db.get_kline(code, start, end, limit=3000)
            except Exception:
                rows = []
            if len(rows) < 15:
                continue
            closes = [float(r['close']) for r in rows]
            dates = [r['trade_date'] for r in rows]
            rsi_series = _compute_rsi(closes)
            self._rsi[code] = {
                d: rsi_series[i]
                for i, d in enumerate(dates) if rsi_series[i] is not None
            }
        logger.info(f"MeanReversionStrategy 就绪: 预加载 {len(self._rsi)} 只股票（RSI 现算）")

    def __call__(self, trade_date: str, positions: Dict, cash: float,
                 total_value: float) -> List[Dict]:
        """策略入口：返回当日买入信号列表。"""
        held = set(positions.keys())
        candidates = []

        for code, rsi_map in self._rsi.items():
            if code in held:
                continue
            rsi = rsi_map.get(trade_date)
            if rsi is None:
                continue
            if rsi <= self.rsi_threshold:
                candidates.append({
                    'ts_code': code,
                    'name': '',
                    'score': round(80 - rsi, 1),
                    'position_pct': self.position_pct,
                    'reason': f'RSI超卖 {rsi:.1f}',
                })

        candidates.sort(key=lambda s: s['score'], reverse=True)
        return candidates[:self.top_n]


def build_mean_reversion_strategy(db, universe: List[str], start: str, end: str,
                                  top_n: int = 3, rsi_threshold: float = 30.0) -> Callable:
    """工厂函数：构造可注册到 BacktestEngine 的策略可调用对象。"""
    return MeanReversionStrategy(db, universe, start, end, top_n, rsi_threshold)
