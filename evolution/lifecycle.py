"""
猎场系统 V6.0 — 策略生命周期管理
记录绩效 → 评估排名 → 自动晋升/降级/淘汰
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import json

logger = logging.getLogger(__name__)


class StrategyLifecycle:
    """
    策略生命周期管理

    策略状态流转:
      active → observing (连续3周表现不佳)
      observing → degraded (连续4周观察)
      degraded → active (连续4周模拟盘跑赢)
    """

    def __init__(self, db, config: dict = None):
        self.db = db
        self.config = config or {}

    def record_performance(self, strategy_name: str, period: str,
                           metrics: Dict, parameters: Dict = None):
        """记录策略绩效"""
        start = datetime.now()
        if period == 'daily':
            start = start - timedelta(days=1)
        elif period == 'weekly':
            start = start - timedelta(days=7)
        elif period == 'monthly':
            start = start - timedelta(days=30)

        record = {
            'strategy_name': strategy_name,
            'period': period,
            'period_start': start.strftime('%Y-%m-%d'),
            'period_end': datetime.now().strftime('%Y-%m-%d'),
            'sharpe_ratio': metrics.get('sharpe', 0),
            'win_rate': metrics.get('win_rate', 0),
            'max_drawdown': metrics.get('max_drawdown', 0),
            'total_return': metrics.get('total_return', 0),
            'annual_return': metrics.get('annual_return', 0),
            'trade_count': metrics.get('trade_count', 0),
            'avg_hold_days': metrics.get('avg_hold_days', 0),
            'parameters': parameters or {},
            'status': 'active',
        }

        self.db.save_lifecycle_record(record)
        logger.debug(f"Lifecycle recorded: {strategy_name} [{period}]")

    def evaluate(self, strategy_name: str) -> Dict:
        """评估策略，返回当前状态和建议"""
        # 获取最近4周绩效
        cutoff = (datetime.now() - timedelta(days=28)).strftime('%Y-%m-%d')
        with self.db._get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM strategy_lifecycle
                   WHERE strategy_name = ? AND period_start >= ?
                   ORDER BY period_start DESC""",
                [strategy_name, cutoff]
            ).fetchall()

        if not rows:
            return {'status': 'active', 'action': 'keep', 'reason': 'insufficient_data'}

        records = [dict(r) for r in rows]
        recent = records[:4]

        # 评估条件
        avg_sharpe = sum(r['sharpe_ratio'] for r in recent) / len(recent)
        avg_return = sum(r['total_return'] for r in recent) / len(recent)
        avg_win_rate = sum(r['win_rate'] for r in recent) / len(recent)

        current_status = records[0].get('status', 'active')

        if current_status == 'active':
            if avg_sharpe < 0 or avg_return < 0:
                return {
                    'status': 'observing',
                    'action': 'downgrade_to_observing',
                    'reason': f'连续表现不佳 (Sharpe={avg_sharpe:.2f}, Return={avg_return:.2f}%)',
                    'metrics': {'avg_sharpe': avg_sharpe, 'avg_return': avg_return, 'avg_win_rate': avg_win_rate},
                }

        elif current_status == 'observing':
            observing_weeks = sum(1 for r in records if r.get('status') == 'observing')
            if observing_weeks >= 4:
                return {
                    'status': 'degraded',
                    'action': 'downgrade_to_degraded',
                    'reason': f'连续{observing_weeks}周观察期未改善',
                    'metrics': {'observing_weeks': observing_weeks},
                }
            elif avg_sharpe > 0.3 and avg_return > 0:
                return {
                    'status': 'active',
                    'action': 'promote_to_active',
                    'reason': '绩效改善，恢复活跃',
                    'metrics': {'avg_sharpe': avg_sharpe, 'avg_return': avg_return},
                }

        elif current_status == 'degraded':
            # 检查模拟盘表现
            sim_records = [r for r in records if r.get('status') == 'degraded']
            if len(sim_records) >= 4 and avg_sharpe > 0.5:
                return {
                    'status': 'active',
                    'action': 'promote_from_degraded',
                    'reason': f'模拟盘连续4周跑赢 (Sharpe={avg_sharpe:.2f})',
                    'metrics': {'avg_sharpe': avg_sharpe},
                }

        return {'status': current_status, 'action': 'keep', 'reason': 'conditions_met',
                'metrics': {'avg_sharpe': avg_sharpe, 'avg_return': avg_return, 'avg_win_rate': avg_win_rate}}

    def get_active_strategies(self) -> List[Dict]:
        """获取活跃策略列表"""
        with self.db._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT strategy_name, status FROM strategy_lifecycle WHERE status = 'active'"
            ).fetchall()
        return [dict(r) for r in rows]
