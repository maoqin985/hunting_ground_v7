"""
猎场系统 V6.0 — 每日复盘（收盘后自动运行）
绩效归因 / 信号回溯 / AI复盘报告
"""

import logging
from typing import Dict, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class DailyReview:
    """每日自动复盘"""

    def __init__(self, db, macro_llm=None, data_gateway=None):
        self.db = db
        self.llm = macro_llm
        self.gateway = data_gateway

    async def run(self) -> Dict:
        """执行每日复盘"""
        today = datetime.now().strftime('%Y-%m-%d')
        logger.info(f"Daily review started: {today}")

        report = {
            'date': today,
            'performance': {},
            'signal_accuracy': {},
            'trade_attribution': [],
            'risk_events': [],
            'ai_summary': '',
        }

        # 1. 绩效归因
        report['performance'] = self._performance_attribution(today)

        # 2. 信号回溯
        report['signal_accuracy'] = self._signal_backtest(today)

        # 3. 交易归因
        report['trade_attribution'] = self._trade_attribution(today)

        # 4. 熔断事件
        report['risk_events'] = self._get_risk_events(today)

        # 5. AI 复盘报告
        if self.llm:
            report['ai_summary'] = await self.llm.generate_daily_review(
                trades=report['trade_attribution'],
                pnl=report['performance'],
                signals=report['signal_accuracy'].get('signals', []),
            )

        logger.info(f"Daily review completed: P&L={report['performance'].get('daily_pnl', 0):.2f}")
        return report

    def _performance_attribution(self, trade_date: str) -> Dict:
        """当日绩效"""
        trades = self.db.get_trades(trade_date, trade_date)
        total_pnl = sum(t.get('pnl', 0) or 0 for t in trades if t['direction'] == 'sell')

        return {
            'daily_pnl': total_pnl,
            'daily_pnl_pct': total_pnl / 20000 * 100,
            'trade_count': len(trades),
            'buy_count': sum(1 for t in trades if t['direction'] == 'buy'),
            'sell_count': sum(1 for t in trades if t['direction'] == 'sell'),
        }

    def _signal_backtest(self, trade_date: str) -> Dict:
        """信号回溯：今天的信号 vs 实际走势"""
        signals = self.db.get_signals_by_date(trade_date)
        if not signals:
            return {'total_signals': 0, 'accuracy': 0, 'signals': []}

        correct = 0
        for sig in signals:
            # 检查信号发出后1-5日的走势
            kline = self.db.get_kline(sig['ts_code'],
                                      start_date=trade_date,
                                      limit=6)
            if len(kline) >= 2:
                entry_price = kline[0]['close'] if kline else 0
                exit_price = kline[-1]['close'] if kline else 0
                if exit_price > entry_price:
                    correct += 1

        accuracy = correct / len(signals) * 100 if signals else 0
        return {
            'total_signals': len(signals),
            'accuracy': accuracy,
            'signals': [{'ts_code': s['ts_code'], 'score': s['score'],
                         'type': s['signal_type']} for s in signals[:10]],
        }

    def _trade_attribution(self, trade_date: str) -> List[Dict]:
        """交易归因"""
        return self.db.get_trades(trade_date, trade_date)

    def _get_risk_events(self, trade_date: str) -> List[Dict]:
        """获取风控事件"""
        with self.db._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM circuit_breaker_log WHERE triggered_at LIKE ?",
                [f"{trade_date}%"]
            ).fetchall()
        return [dict(r) for r in rows]
