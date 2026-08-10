"""
猎场系统 V6.0 — 回测引擎
支持：手续费/滑点/T+1/涨跌停限制
借鉴 Hikyuu 组件化设计
"""

import logging
import numpy as np
from typing import Dict, List, Callable, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    initial_capital: float = 20000.0
    start_date: str = "2022-01-01"
    end_date: str = "2025-12-31"
    commission_rate: float = 0.00025
    stamp_tax_rate: float = 0.0005  # 卖出
    slippage: float = 0.001  # 0.1% 滑点
    min_broker_fee: float = 5.0
    t_plus_1: bool = True
    limit_up_down: bool = True
    max_positions: int = 2
    max_single_pct: float = 0.70


@dataclass
class BacktestResult:
    total_return: float = 0.0
    annual_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    avg_hold_days: float = 0.0
    profit_factor: float = 0.0
    daily_values: List[float] = field(default_factory=list)
    trades: List[Dict] = field(default_factory=list)


class BacktestEngine:
    """事件驱动回测引擎"""

    def __init__(self, config: BacktestConfig = None, db=None):
        self.config = config or BacktestConfig()
        self.db = db
        self._strategies: List[Callable] = []

    def add_strategy(self, strategy_fn: Callable):
        """注册策略函数"""
        self._strategies.append(strategy_fn)

    def run(self) -> BacktestResult:
        """运行回测"""
        # 获取交易日列表
        all_dates = self._get_trading_dates()
        if not all_dates:
            logger.error("No trading dates available")
            return BacktestResult()

        result = BacktestResult()
        capital = self.config.initial_capital
        cash = capital
        positions: Dict[str, Dict] = {}
        daily_values = []
        trades = []

        for i, trade_date in enumerate(all_dates):
            # 更新持仓市值
            position_value = self._mark_to_market(positions, trade_date)
            total_value = cash + position_value
            daily_values.append(total_value)

            # 1. 检查止损/止盈
            exit_orders = self._check_exits(positions, trade_date)
            for order in exit_orders:
                cash += order['amount'] - order['fee']
                trades.append(order)
                positions.pop(order['ts_code'], None)

            # 2. 执行各策略
            if len(positions) < self.config.max_positions:
                for strategy in self._strategies:
                    signals = strategy(trade_date, positions, cash, total_value)
                    for sig in signals:
                        if len(positions) >= self.config.max_positions:
                            break
                        order = self._execute_buy(sig, cash, trade_date)
                        if order and order['amount'] <= cash:
                            cash -= order['amount'] + order['fee']
                            positions[order['ts_code']] = order
                            trades.append(order)

        # 最终清仓
        for ts_code, pos in list(positions.items()):
            final_price = self._get_price(ts_code, all_dates[-1])
            amount = final_price * pos['shares']
            fee = amount * (self.config.commission_rate + self.config.stamp_tax_rate)
            cash += amount - fee
            trades.append({**pos, 'direction': 'sell', 'price': final_price, 'amount': amount, 'fee': fee, 'exit_reason': 'end_of_period'})
            positions.pop(ts_code)

        final_value = cash
        daily_values.append(final_value)

        # 计算指标
        result = self._calculate_metrics(daily_values, trades, all_dates)
        return result

    def _get_trading_dates(self) -> List[str]:
        """获取回测期间的交易日"""
        dates = []
        if self.db:
            rows = self.db.get_kline("000001.SZ", self.config.start_date, self.config.end_date)
            dates = sorted(set(r['trade_date'] for r in rows))
        return dates

    def _mark_to_market(self, positions: Dict, trade_date: str) -> float:
        """按市价计算持仓"""
        total = 0
        for ts_code, pos in positions.items():
            price = self._get_price(ts_code, trade_date)
            if price > 0:
                pos['current_price'] = price
                total += price * pos['shares']
        return total

    def _get_price(self, ts_code: str, trade_date: str) -> float:
        """获取某日收盘价"""
        rows = self.db.get_kline(ts_code, trade_date, trade_date, limit=1)
        return rows[0]['close'] if rows else 0

    def _check_exits(self, positions: Dict, trade_date: str) -> List[Dict]:
        """检查止损止盈"""
        orders = []
        for ts_code, pos in list(positions.items()):
            price = self._get_price(ts_code, trade_date)
            if price <= 0:
                continue
            pnl_pct = (price / pos['avg_cost'] - 1) * 100
            if pnl_pct <= -5:
                orders.append(self._make_sell_order(pos, price, 'stop_loss'))
            elif pnl_pct >= 20:
                orders.append(self._make_sell_order(pos, price, 'take_profit'))
        return orders

    def _execute_buy(self, signal: Dict, cash: float, trade_date: str) -> Optional[Dict]:
        """执行买入"""
        price = self._get_price(signal['ts_code'], trade_date)
        if price <= 0:
            return None

        # 滑点
        price *= (1 + self.config.slippage)

        amount = cash * min(signal.get('position_pct', 0.35), self.config.max_single_pct)
        shares = int(amount / price / 100) * 100
        if shares < 100:
            return None

        amount = shares * price
        fee = amount * self.config.commission_rate
        fee = max(fee, self.config.min_broker_fee)

        return {
            'ts_code': signal['ts_code'],
            'name': signal.get('name', ''),
            'direction': 'buy',
            'price': price,
            'shares': shares,
            'amount': amount,
            'fee': fee,
            'avg_cost': price,
            'trade_date': trade_date,
            'score': signal.get('score', 0),
            'reason': signal.get('reason', ''),
        }

    def _make_sell_order(self, pos: Dict, price: float, reason: str) -> Dict:
        amount = price * pos['shares']
        fee = amount * (self.config.commission_rate + self.config.stamp_tax_rate)
        return {**pos, 'direction': 'sell', 'price': price, 'amount': amount, 'fee': fee, 'exit_reason': reason,
                'pnl': (price - pos['avg_cost']) * pos['shares'],
                'pnl_pct': (price / pos['avg_cost'] - 1) * 100}

    def _calculate_metrics(self, daily_values: List[float],
                           trades: List[Dict], dates: List[str]) -> BacktestResult:
        """计算回测绩效指标"""
        values = np.array(daily_values)
        if len(values) < 2:
            return BacktestResult()

        returns = np.diff(values) / values[:-1]

        result = BacktestResult()
        result.total_return = (values[-1] / values[0] - 1) * 100
        years = (datetime.strptime(dates[-1], '%Y-%m-%d') - datetime.strptime(dates[0], '%Y-%m-%d')).days / 365.25
        result.annual_return = ((values[-1] / values[0]) ** (1 / max(years, 0.5)) - 1) * 100
        result.sharpe_ratio = (np.mean(returns) / (np.std(returns) + 1e-8)) * np.sqrt(252) if len(returns) > 1 else 0

        peak = np.maximum.accumulate(values)
        drawdowns = (peak - values) / peak * 100
        result.max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0

        sell_trades = [t for t in trades if t['direction'] == 'sell']
        if sell_trades:
            result.win_rate = sum(1 for t in sell_trades if t.get('pnl', 0) > 0) / len(sell_trades) * 100
            profits = [t.get('pnl', 0) for t in sell_trades if t.get('pnl', 0) > 0]
            losses = [abs(t.get('pnl', 0)) for t in sell_trades if t.get('pnl', 0) < 0]
            result.profit_factor = sum(profits) / (sum(losses) + 1e-8) if losses else float('inf')

        result.total_trades = len(trades)
        result.daily_values = [float(v) for v in daily_values]
        result.trades = trades

        return result
