"""
猎场系统 V6.0 — 资金面信号扫描器
北向资金 / 量比异动 / 换手率异常 / 大单净流入
"""

import logging
from typing import List, Dict
from .base_scanner import BaseScanner

logger = logging.getLogger(__name__)


class FundFlowScanner(BaseScanner):
    """资金面信号扫描器"""

    def __init__(self, db, config: dict = None):
        super().__init__("fund_flow", db, config)
        cfg = self.config.get("technical_indicators", {})
        self.vr_threshold = cfg.get("volume_ratio", {}).get("anomaly_threshold", 2.0)
        self.turnover_max = cfg.get("turnover_rate", {}).get("max_daily", 10.0)
        self.turnover_min = cfg.get("turnover_rate", {}).get("min_daily", 0.5)

    async def scan(self, ts_codes: List[str]) -> List[Dict]:
        signals = []

        for ts_code in ts_codes:
            kline = self.db.get_kline(ts_code, limit=10)
            if len(kline) < 5:
                continue

            details = {}

            # ---- 量比异动 ----
            vr_sig = self._check_volume_ratio(kline)
            if vr_sig:
                details['volume_ratio_anomaly'] = vr_sig

            # ---- 换手率异常 ----
            tr_sig = self._check_turnover_rate(kline)
            if tr_sig:
                details['turnover_rate_anomaly'] = tr_sig

            # ---- 缩量止跌 ----
            sv_sig = self._check_shrink_volume(kline)
            if sv_sig:
                details['shrink_volume_bottom'] = sv_sig

            # ---- 量价配合 ----
            vp_sig = self._check_volume_price(kline)
            if vp_sig:
                details['volume_price_match'] = vp_sig

            if not details:
                continue

            score = self._calculate_score(details)
            signals.append({
                'ts_code': ts_code,
                'name': '',
                'signal_type': 'fund_flow',
                'score': score,
                'direction': 'buy',
                'reason': self._build_reason(details),
                'details': details,
            })

        signals.sort(key=lambda s: s['score'], reverse=True)
        return signals

    def _check_volume_ratio(self, kline: List[Dict]) -> Dict:
        """量比异动检查"""
        latest = kline[-1]
        vr = latest.get('volume_ratio', 1.0) or 1.0

        if vr >= self.vr_threshold and latest.get('change_pct', 0) or 0 > 0:
            return {'volume_ratio': vr, 'strength': min(vr * 15, 60)}
        return None

    def _check_turnover_rate(self, kline: List[Dict]) -> Dict:
        """换手率异常"""
        latest = kline[-1]
        turnover = latest.get('turnover_rate', 0) or 0

        # 换手率适中（2%-8%）且股价上涨
        if self.turnover_min < turnover < self.turnover_max:
            change_pct = latest.get('change_pct', 0) or 0
            if change_pct > 0:
                return {'turnover_rate': turnover, 'strength': min(turnover * 5, 50)}
        return None

    def _check_shrink_volume(self, kline: List[Dict]) -> Dict:
        """缩量止跌"""
        if len(kline) < 5:
            return None

        recent_volumes = [r.get('volume', 0) or 0 for r in kline[-5:]]
        avg_vol = sum(recent_volumes[:-1]) / max(len(recent_volumes) - 1, 1)
        latest_vol = recent_volumes[-1]

        latest_change = kline[-1].get('change_pct', 0) or 0

        # 缩量 + 跌幅收窄/止跌
        if latest_vol < avg_vol * 0.7 and latest_change > -2:
            return {'volume_shrink_pct': (1 - latest_vol / avg_vol) * 100, 'strength': 35}
        return None

    def _check_volume_price(self, kline: List[Dict]) -> Dict:
        """量价配合（放量上涨）"""
        if len(kline) < 3:
            return None

        latest = kline[-1]
        prev = kline[-2]

        latest_vol = latest.get('volume', 0) or 0
        prev_vol = prev.get('volume', 0) or 0
        latest_change = latest.get('change_pct', 0) or 0

        if latest_vol > prev_vol * 1.5 and latest_change > 0:
            return {'vol_increase_pct': (latest_vol / prev_vol - 1) * 100, 'strength': 40}
        return None

    def _calculate_score(self, details: Dict) -> float:
        weights = {
            'volume_ratio_anomaly': 0.35,
            'turnover_rate_anomaly': 0.30,
            'shrink_volume_bottom': 0.20,
            'volume_price_match': 0.15,
        }
        score = sum(details[k].get('strength', 20) * weights.get(k, 0) for k in details)
        return min(round(score, 1), 100)

    def _build_reason(self, details: Dict) -> str:
        reasons = []
        if 'volume_ratio_anomaly' in details:
            reasons.append(f"量比异动({details['volume_ratio_anomaly'].get('volume_ratio', '?')})")
        if 'turnover_rate_anomaly' in details:
            reasons.append(f"换手活跃({details['turnover_rate_anomaly'].get('turnover_rate', '?')}%)")
        if 'shrink_volume_bottom' in details:
            reasons.append("缩量止跌")
        if 'volume_price_match' in details:
            reasons.append("放量上涨")
        return ' + '.join(reasons)
