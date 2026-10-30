"""
猎场系统 V6.0 — 技术面信号扫描器
RSI超卖 / MACD金叉 / 布林下轨 / V型反转 / 底分型
"""

import logging
from typing import List, Dict
from .base_scanner import BaseScanner

logger = logging.getLogger(__name__)


class TechnicalScanner(BaseScanner):
    """技术面多策略扫描器"""

    def __init__(self, db, config: dict = None):
        super().__init__("technical", db, config)
        cfg = self.config.get("technical_indicators", {})
        self.rsi_oversold = cfg.get("rsi", {}).get("oversold_threshold", 30)
        self.v_decline_min = self.config.get("v_pattern", {}).get("decline_min_pct", -15.0)
        self.v_recovery_min = self.config.get("v_pattern", {}).get("recovery_min_pct", 5.0)

    async def scan(self, ts_codes: List[str]) -> List[Dict]:
        signals = []
        for ts_code in ts_codes:
            kline = self.db.get_kline(ts_code, limit=60)
            if len(kline) < 20:
                continue

            # 补全缺失的技术指标 (Tencent免费接口不提供RSI/MACD/布林)
            self._ensure_indicators(kline)

            details = {}

            # ---- RSI 超卖信号 ----
            rsi_sig = self._check_rsi_oversold(kline)
            if rsi_sig:
                details['rsi_oversold'] = rsi_sig

            # ---- MACD 金叉 ----
            macd_sig = self._check_macd_golden_cross(kline)
            if macd_sig:
                details['macd_golden_cross'] = macd_sig

            # ---- 布林下轨反弹 ----
            boll_sig = self._check_bollinger_bounce(kline)
            if boll_sig:
                details['bollinger_bounce'] = boll_sig

            # ---- V型反转 ----
            v_sig = self._check_v_pattern(kline)
            if v_sig:
                details['v_pattern'] = v_sig

            # ---- 底分型 ----
            bf_sig = self._check_bottom_fractal(kline)
            if bf_sig:
                details['bottom_fractal'] = bf_sig

            if not details:
                continue

            # 综合评分
            score = self._calculate_score(details, kline)
            signals.append({
                'ts_code': ts_code,
                'name': '',
                'signal_type': 'technical',
                'score': score,
                'direction': 'buy',
                'reason': self._build_reason(details),
                'details': details,
            })

        # 按评分排序
        signals.sort(key=lambda s: s['score'], reverse=True)
        return signals

    def _check_rsi_oversold(self, kline: List[Dict]) -> Dict:
        """RSI超卖信号"""
        latest = kline[-1]
        rsi = latest.get('rsi_14', 50)
        if rsi and rsi <= self.rsi_oversold:
            # RSI越低，信号越强 (RSI=20时约50分, RSI=10时约70分)
            strength = (self.rsi_oversold - rsi) / self.rsi_oversold * 80 + 30
            return {'rsi': rsi, 'strength': min(strength, 90)}
        return None

    def _check_macd_golden_cross(self, kline: List[Dict]) -> Dict:
        """MACD金叉信号"""
        if len(kline) < 3:
            return None
        prev = kline[-2]
        curr = kline[-1]
        prev_hist = prev.get('macd_hist', 0) or 0
        curr_hist = curr.get('macd_hist', 0) or 0

        if prev_hist < 0 and curr_hist > 0:
            return {'prev_hist': prev_hist, 'curr_hist': curr_hist, 'strength': 65}
        return None

    def _check_bollinger_bounce(self, kline: List[Dict]) -> Dict:
        """布林下轨反弹"""
        latest = kline[-1]
        close = latest.get('close', 0)
        lower = latest.get('boll_lower', 0)
        if lower and close > 0 and close <= lower * 1.02:
            bounce_pct = (close / lower - 1) * 100
            return {'close': close, 'lower': lower, 'bounce_pct': bounce_pct, 'strength': 55}
        return None

    def _check_v_pattern(self, kline: List[Dict]) -> Dict:
        """V型反转形态"""
        if len(kline) < 20:
            return None
        prices = [r['close'] for r in kline[-20:]]

        min_idx = prices.index(min(prices))
        if min_idx < 3 or min_idx > 16:
            return None

        left_high = max(prices[:min_idx + 1])
        valley = prices[min_idx]
        right_high = prices[-1]

        decline = (valley / left_high - 1) * 100
        recovery = (right_high / valley - 1) * 100

        if decline <= self.v_decline_min and recovery >= self.v_recovery_min:
            strength = min(abs(decline) * 1.5 + recovery * 2, 85)
            return {'decline_pct': decline, 'recovery_pct': recovery, 'strength': strength}
        return None

    def _check_bottom_fractal(self, kline: List[Dict]) -> Dict:
        """底分型识别"""
        if len(kline) < 7:
            return None
        lows = [r['low'] for r in kline[-7:]]
        mid = lows[3]
        if mid < lows[2] and mid < lows[4] and mid < lows[1] and mid < lows[5]:
            return {'fractal_low': mid, 'strength': 50}
        return None

    def _calculate_score(self, details: Dict, kline: List[Dict]) -> float:
        """综合技术面评分"""
        score = 0
        weights = {
            'rsi_oversold': 0.30,
            'macd_golden_cross': 0.25,
            'bollinger_bounce': 0.20,
            'v_pattern': 0.15,
            'bottom_fractal': 0.10,
        }
        for key, weight in weights.items():
            if key in details:
                score += details[key].get('strength', 30) * weight
        return min(round(score, 1), 100)

    def _build_reason(self, details: Dict) -> str:
        reasons = []
        if 'rsi_oversold' in details:
            reasons.append(f"RSI超卖({details['rsi_oversold'].get('rsi', '?')})")
        if 'macd_golden_cross' in details:
            reasons.append("MACD金叉")
        if 'bollinger_bounce' in details:
            reasons.append("布林下轨反弹")
        if 'v_pattern' in details:
            reasons.append("V型反转")
        if 'bottom_fractal' in details:
            reasons.append("底分型")
        return ' + '.join(reasons)

    def _ensure_indicators(self, kline: List[Dict]):
        """补全缺失的技术指标: RSI(14), MACD, Bollinger(20,2)"""
        closes = [r.get('close', 0) for r in kline]
        highs = [r.get('high', 0) for r in kline]
        lows = [r.get('low', 0) for r in kline]

        # RSI-14
        if any(r.get('rsi_14', 0) == 50.0 for r in kline[-20:]):
            for i in range(14, len(kline)):
                gains = sum(max(closes[j] - closes[j-1], 0) for j in range(i-13, i+1))
                losses = sum(max(closes[j-1] - closes[j], 0) for j in range(i-13, i+1))
                avg_gain = gains / 14
                avg_loss = losses / 14
                if avg_loss == 0:
                    kline[i]['rsi_14'] = 100.0
                else:
                    rs = avg_gain / avg_loss
                    kline[i]['rsi_14'] = round(100.0 - 100.0 / (1.0 + rs), 1)

        # MACD (12, 26, 9)
        if any(r.get('macd_hist', 0) == 0 for r in kline[-5:]):
            ema12 = closes[0]
            ema26 = closes[0]
            macd_vals = []
            for i, c in enumerate(closes):
                ema12 = c * 2/(12+1) + ema12 * (1 - 2/(12+1))
                ema26 = c * 2/(26+1) + ema26 * (1 - 2/(26+1))
                dif = ema12 - ema26
                macd_vals.append(dif)
                kline[i]['macd_dif'] = round(dif, 4)

            signal = macd_vals[0]
            for i, dif in enumerate(macd_vals):
                signal = dif * 2/(9+1) + signal * (1 - 2/(9+1))
                kline[i]['macd_hist'] = round(dif - signal, 4)

        # Bollinger (20, 2)
        if any(r.get('boll_lower', 0) == 0 for r in kline[-5:]):
            for i in range(19, len(kline)):
                window = closes[i-19:i+1]
                ma = sum(window) / 20
                variance = sum((c - ma)**2 for c in window) / 20
                std = variance ** 0.5
                kline[i]['boll_mid'] = round(ma, 2)
                kline[i]['boll_upper'] = round(ma + 2 * std, 2)
                kline[i]['boll_lower'] = round(ma - 2 * std, 2)
