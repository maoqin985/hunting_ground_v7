"""
猎场系统 V6.0 — 持仓实时监控
追踪持仓变化、计算组合风险指标
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import numpy as np

logger = logging.getLogger(__name__)


class PositionMonitor:
    """持仓监控与组合风险分析"""

    def __init__(self, config: dict = None, db=None, data_gateway=None):
        self.config = config or {}
        self.db = db
        self.gateway = data_gateway

        cap_cfg = config.get("capital", {})
        self.total_capital = cap_cfg.get("total", 20000.0)

        # 当日/当周初始资金（用于计算日/周盈亏）
        self._day_start_value = self.total_capital
        self._week_start_value = self.total_capital
        self._peak_value = self.total_capital

        # 历史快照
        self._snapshots: List[Dict] = []

    async def update(self) -> Dict:
        """更新持仓状态并返回组合摘要"""
        positions = self.db.get_all_positions()

        # 获取实时行情
        codes = [p['ts_code'] for p in positions]
        quotes = await self.gateway.get_realtime_quote(codes) if self.gateway else {}

        total_position_value = 0
        updated_positions = []

        for pos in positions:
            ts_code = pos['ts_code']
            quote = quotes.get(ts_code, {})
            current_price = quote.get('price', pos.get('current_price', 0))

            if current_price > 0:
                market_value = current_price * pos['volume']
                pnl = (current_price - pos['avg_cost']) * pos['volume']
                pnl_pct = (current_price / pos['avg_cost'] - 1) * 100 if pos['avg_cost'] > 0 else 0

                # 更新数据库
                self.db.update_position(
                    ts_code, pos.get('name', ''),
                    pos['volume'], pos['avg_cost'],
                    current_price, pos.get('buy_date', ''),
                )

                total_position_value += market_value
                updated_positions.append({
                    'ts_code': ts_code,
                    'name': pos.get('name', ''),
                    'volume': pos['volume'],
                    'avg_cost': pos['avg_cost'],
                    'current_price': current_price,
                    'market_value': market_value,
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl_pct, 2),
                    'hold_days': pos.get('hold_days', 0),
                })

        # 计算组合指标
        cash = self._calculate_cash(updated_positions)
        total_value = cash + total_position_value

        # 更新峰值
        if total_value > self._peak_value:
            self._peak_value = total_value

        daily_pnl = total_value - self._day_start_value
        daily_pnl_pct = (daily_pnl / self._day_start_value * 100) if self._day_start_value > 0 else 0
        weekly_pnl = total_value - self._week_start_value
        weekly_pnl_pct = (weekly_pnl / self._week_start_value * 100) if self._week_start_value > 0 else 0
        total_pnl = total_value - self.total_capital
        total_pnl_pct = (total_pnl / self.total_capital * 100) if self.total_capital > 0 else 0
        drawdown = (self._peak_value - total_value) / self._peak_value * 100 if self._peak_value > 0 else 0

        summary = {
            'total_value': round(total_value, 2),
            'cash': round(cash, 2),
            'position_value': round(total_position_value, 2),
            'position_count': len(updated_positions),
            'daily_pnl': round(daily_pnl, 2),
            'daily_pnl_pct': round(daily_pnl_pct, 2),
            'weekly_pnl': round(weekly_pnl, 2),
            'weekly_pnl_pct': round(weekly_pnl_pct, 2),
            'total_pnl': round(total_pnl, 2),
            'total_pnl_pct': round(total_pnl_pct, 2),
            'max_drawdown': round(drawdown, 2),
            'peak_value': round(self._peak_value, 2),
            'positions': updated_positions,
            'updated_at': datetime.now().isoformat(),
        }

        # 保存快照
        self._snapshots.append(summary)
        if len(self._snapshots) > 1000:
            self._snapshots = self._snapshots[-500:]

        # 持久化
        if self.db:
            self.db.save_portfolio_snapshot({
                'total_value': summary['total_value'],
                'cash': summary['cash'],
                'position_value': summary['position_value'],
                'daily_pnl': summary['daily_pnl'],
                'daily_pnl_pct': summary['daily_pnl_pct'],
                'weekly_pnl': summary['weekly_pnl'],
                'weekly_pnl_pct': summary['weekly_pnl_pct'],
                'total_pnl': summary['total_pnl'],
                'total_pnl_pct': summary['total_pnl_pct'],
                'position_count': summary['position_count'],
            })

        return summary

    def reset_daily(self):
        """重置日内基准"""
        self._day_start_value = self._peak_value

    def reset_weekly(self):
        """重置周度基准"""
        self._week_start_value = self._peak_value

    def _calculate_cash(self, positions: List[Dict]) -> float:
        """计算剩余现金（从交易记录推算）"""
        trades = self.db.get_trades()
        cash = self.total_capital
        for t in trades:
            if t['direction'] == 'buy':
                cash -= t['amount'] + t.get('fee', 0)
            else:
                cash += t['amount'] - t.get('fee', 0)
        return max(cash, 0)

    def get_snapshot_history(self, n: int = 20) -> List[Dict]:
        """获取历史快照"""
        return self._snapshots[-n:]

    def get_risk_metrics(self) -> Dict:
        """计算风险指标"""
        if len(self._snapshots) < 5:
            return {'var_95': 0, 'sharpe': 0, 'max_drawdown_pct': 0}

        values = [s['total_value'] for s in self._snapshots[-100:]]
        if len(values) < 2:
            return {'var_95': 0, 'sharpe': 0, 'max_drawdown_pct': 0}

        returns = np.diff(values) / values[:-1]

        var_95 = np.percentile(returns, 5) if len(returns) > 0 else 0
        sharpe = (np.mean(returns) / (np.std(returns) + 1e-8)) * np.sqrt(252) if len(returns) > 1 else 0
        max_dd = max(s['max_drawdown'] for s in self._snapshots[-100:]) / 100

        return {
            'var_95': round(float(var_95), 4),
            'sharpe': round(float(sharpe), 2),
            'max_drawdown_pct': round(float(max_dd), 2),
            'sample_size': len(returns),
        }
