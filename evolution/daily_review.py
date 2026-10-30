"""
猎场系统 V7.0 — 每日复盘（per-account，收盘后自动运行）
绩效归因 / 信号回溯 / 因子有效性检测 / AI复盘报告
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class DailyReview:
    """每日自动复盘（per-account）"""

    def __init__(self, db, macro_llm=None, data_gateway=None,
                 account_id: str = None, initial_capital: float = 2_000_000):
        self.db = db
        self.llm = macro_llm
        self.gateway = data_gateway
        self.account_id = account_id  # V7.0: per-account
        self.initial_capital = initial_capital  # V7.0: actual capital

    async def run(self) -> Dict:
        """执行每日复盘"""
        today = datetime.now().strftime('%Y-%m-%d')
        acct_tag = f" [{self.account_id}]" if self.account_id else ""
        logger.info(f"Daily review started: {today}{acct_tag}")

        report = {
            'date': today,
            'account_id': self.account_id,
            'performance': {},
            'signal_accuracy': {},
            'trade_attribution': [],
            'risk_events': [],
            'factor_effectiveness': {},
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

        # 5. 因子有效性检测
        report['factor_effectiveness'] = self._check_factor_effectiveness()

        # 6. AI 复盘报告
        if self.llm:
            report['ai_summary'] = await self.llm.generate_daily_review(
                trades=report['trade_attribution'],
                pnl=report['performance'],
                signals=report['signal_accuracy'].get('signals', []),
            )

        # 7. 持久化报告
        self.db.save_review_report({
            'account_id': self.account_id or '',
            'date': today,
            'daily_pnl': report['performance'].get('daily_pnl', 0),
            'daily_pnl_pct': report['performance'].get('daily_pnl_pct', 0),
            'signal_accuracy': report['signal_accuracy'].get('accuracy', 0),
            'benchmark_return': 0,
            'factor_performance': report['factor_effectiveness'],
            'trades_summary': report['trade_attribution'][:20],
            'llm_summary': report.get('ai_summary', ''),
        })

        logger.info(f"Daily review completed: P&L={report['performance'].get('daily_pnl', 0):.2f}")
        return report

    def _performance_attribution(self, trade_date: str) -> Dict:
        """当日绩效（使用实际账户资金计算百分比）"""
        trades = self.db.get_trades(
            account_id=self.account_id,
            start_date=trade_date, end_date=trade_date
        )
        total_pnl = sum(t.get('pnl', 0) or 0 for t in trades if t['direction'] == 'sell')

        return {
            'daily_pnl': total_pnl,
            'daily_pnl_pct': (total_pnl / self.initial_capital * 100) if self.initial_capital > 0 else 0,
            'trade_count': len(trades),
            'buy_count': sum(1 for t in trades if t['direction'] == 'buy'),
            'sell_count': sum(1 for t in trades if t['direction'] == 'sell'),
        }

    def _signal_backtest(self, trade_date: str) -> Dict:
        """信号回溯：今天的信号 vs 实际走势"""
        signals = self.db.get_signals_by_date(trade_date, account_id=self.account_id)
        if not signals:
            return {'total_signals': 0, 'accuracy': 0, 'signals': []}

        correct = 0
        for sig in signals:
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
            'accuracy': round(accuracy, 1),
            'signals': [{'ts_code': s['ts_code'], 'score': s['score'],
                         'type': s['signal_type']} for s in signals[:10]],
        }

    def _trade_attribution(self, trade_date: str) -> List[Dict]:
        """交易归因"""
        return self.db.get_trades(
            account_id=self.account_id,
            start_date=trade_date, end_date=trade_date
        )

    def _get_risk_events(self, trade_date: str) -> List[Dict]:
        """获取风控事件"""
        acct_filter = f" AND account_id = '{self.account_id}'" if self.account_id else ""
        with self.db._get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM circuit_breaker_log WHERE triggered_at LIKE ?{acct_filter}",
                [f"{trade_date}%"]
            ).fetchall()
        return [dict(r) for r in rows]

    def _check_factor_effectiveness(self) -> Dict:
        """V7.0: 检测因子有效性"""
        factors = self.db.get_active_factors()
        if not factors:
            return {'total_factors': 0, 'decaying': [], 'strong': []}

        decaying = []
        strong = []
        for f in factors:
            effectiveness = f.get('effectiveness', 0)
            if effectiveness < 0.3:
                decaying.append(f['factor_name'])
            elif effectiveness > 0.7:
                strong.append(f['factor_name'])

        return {
            'total_factors': len(factors),
            'decaying': decaying,
            'strong': strong,
            'avg_effectiveness': round(
                sum(f.get('effectiveness', 0) for f in factors) / len(factors), 3
            ),
        }
