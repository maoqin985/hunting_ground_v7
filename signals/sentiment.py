"""
猎场系统 V6.0 — 情绪面信号扫描器
NLP新闻评分 / 恐贪指数 / 涨跌家数 / 散户情绪
"""

import logging
from typing import List, Dict
from .base_scanner import BaseScanner

logger = logging.getLogger(__name__)


class SentimentScanner(BaseScanner):
    """市场情绪信号扫描器"""

    def __init__(self, db, config: dict = None):
        super().__init__("sentiment", db, config)
        self.fear_greed = 50  # 默认中性
        self.advance_decline = 1.0
        self.limit_up_count = 0
        self.limit_down_count = 0

    async def update_market_sentiment(self, fear_greed: float = None,
                                       advance_decline: float = None,
                                       limit_up: int = 0, limit_down: int = 0):
        """更新市场情绪数据（由数据层定期调用）"""
        if fear_greed is not None:
            self.fear_greed = fear_greed
        if advance_decline is not None:
            self.advance_decline = advance_decline
        self.limit_up_count = limit_up
        self.limit_down_count = limit_down

    async def scan(self, ts_codes: List[str]) -> List[Dict]:
        signals = []

        # 全局市场情绪信号
        market_signal = self._market_sentiment_signal()
        if market_signal:
            signals.append(market_signal)

        # 个股情绪（基于新闻）
        negative_stocks = set(self.db.get_negative_stocks())

        for ts_code in ts_codes:
            # 负面新闻股票跳过
            if ts_code in negative_stocks:
                continue

            # 检查近期新闻情绪
            details = {}

            # 从数据库获取近期新闻
            with self.db._get_conn() as conn:
                rows = conn.execute(
                    """SELECT AVG(sentiment) as avg_sent, COUNT(*) as cnt
                       FROM news_sentiment
                       WHERE ts_code = ? AND fetched_at > datetime('now', '-3 days')""",
                    [ts_code]
                ).fetchone()

                if rows and rows['cnt'] > 0:
                    avg_sent = rows['avg_sent']
                    if avg_sent > 0.2:
                        details['positive_news'] = {
                            'avg_sentiment': avg_sent,
                            'count': rows['cnt'],
                            'strength': min(avg_sent * 40 + 20, 50),
                        }

            if not details:
                continue

            score = self._calculate_score(details)
            signals.append({
                'ts_code': ts_code,
                'name': '',
                'signal_type': 'sentiment',
                'score': score,
                'direction': 'buy',
                'reason': '正面新闻情绪',
                'details': details,
            })

        return signals

    def _market_sentiment_signal(self) -> Dict:
        """市场整体情绪信号"""
        sentiment_config = self.config.get("market_sentiment", {})

        # 恐贪指数
        fg_cfg = sentiment_config.get("fear_greed", {})
        extreme_fear = fg_cfg.get("extreme_fear", 20)
        extreme_greed = fg_cfg.get("extreme_greed", 80)

        if self.fear_greed <= extreme_fear:
            return {
                'ts_code': 'MARKET',
                'name': '全市场',
                'signal_type': 'sentiment',
                'score': 25,
                'direction': 'caution',
                'reason': f'极度恐惧({self.fear_greed})—机会浮现但需谨慎',
                'details': {'fear_greed': self.fear_greed, 'status': 'extreme_fear'},
            }
        elif self.fear_greed >= extreme_greed:
            return {
                'ts_code': 'MARKET',
                'name': '全市场',
                'signal_type': 'sentiment',
                'score': 10,
                'direction': 'sell',
                'reason': f'极度贪婪({self.fear_greed})—减仓信号',
                'details': {'fear_greed': self.fear_greed, 'status': 'extreme_greed'},
            }
        return None

    def _calculate_score(self, details: Dict) -> float:
        score = 0
        if 'positive_news' in details:
            score += details['positive_news'].get('strength', 20)
        return min(round(score, 1), 100)
