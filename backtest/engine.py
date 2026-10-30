"""
猎场系统 V6.0 — 回测引擎
支持：手续费/滑点/T+1/涨跌停限制/训练-测试(样本外)分段
借鉴 Hikyuu 组件化设计

维度二「策略生成与防过拟合回测引擎」落地点：
  - 真实交易环境模拟：佣金 + 印花税 + 滑点 + 最低手续费
  - T+1 强制：当日买入不可当日卖出
  - 涨跌停强制：涨停封死不可买、跌停封死不可卖（按板块 10%/20%/30%）
  - 分段样本外验证：train_ratio 切分样本内/样本外，分别输出绩效
"""

import logging
import numpy as np
from typing import Dict, List, Callable, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    initial_capital: float = 2000000.0
    start_date: str = "2022-01-01"
    end_date: str = "2025-12-31"
    commission_rate: float = 0.00025
    stamp_tax_rate: float = 0.0005  # 卖出（印花税 0.05%）
    transfer_fee_rate: float = 0.00001  # 过户费 0.001%（买卖双向）
    slippage: float = 0.001  # 0.1% 滑点
    min_broker_fee: float = 5.0
    t_plus_1: bool = True
    limit_up_down: bool = True
    max_positions: int = 6
    max_single_pct: float = 0.25
    stop_loss_pct: float = 5.0     # 个股止损 -5%
    take_profit_pct: float = 20.0  # 个股止盈 +20%
    train_ratio: float = 0.70  # 样本内比例，其余为样本外


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
    turnover_rate: float = 0.0            # 单边换手（总买入额/初始资金）
    return_attribution: Dict = field(default_factory=dict)  # 收益来源拆解
    daily_values: List[float] = field(default_factory=list)
    trades: List[Dict] = field(default_factory=list)
    # 样本外验证（分段）
    in_sample: Optional['BacktestResult'] = None
    out_of_sample: Optional['BacktestResult'] = None


class BacktestEngine:
    """事件驱动回测引擎"""

    def __init__(self, config: BacktestConfig = None, db=None):
        self.config = config or BacktestConfig()
        self.db = db
        self._strategies: List[Callable] = []
        self._row_cache: Dict = {}  # (ts_code, trade_date) -> kline row
        # 压力测试钩子：合成极端行情注入
        self._price_perturbation = None  # (ts_code, trade_date, price) -> 扰动后价格
        self._forced_limit_down_dates = set()  # 强制跌停封死日期（无法卖出）

    def add_strategy(self, strategy_fn: Callable):
        """注册策略函数: fn(trade_date, positions, cash, total_value) -> List[signal]"""
        self._strategies.append(strategy_fn)

    def run(self) -> BacktestResult:
        """运行回测（含样本内/样本外分段）"""
        all_dates = self._get_trading_dates()
        if not all_dates:
            logger.error("No trading dates available")
            return BacktestResult()

        self._row_cache = {}
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

            # 1. 检查止损/止盈（受 T+1 / 跌停限制）
            exit_orders = self._check_exits(positions, trade_date)
            for order in exit_orders:
                cash += order['amount'] - order['fee']
                trades.append(order)
                positions.pop(order['ts_code'], None)

            # 2. 执行各策略（受涨停限制）
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
            if final_price <= 0:
                # 停牌/无末日报价：退回最近一次可交易市价，避免误记为 100% 亏损
                final_price = pos.get('current_price', 0) or pos['avg_cost']
            order = self._make_sell_order(pos, final_price, 'end_of_period', all_dates[-1])
            cash += order['amount'] - order['fee']
            trades.append(order)
            positions.pop(ts_code)

        final_value = cash
        daily_values.append(final_value)

        # 计算全期指标
        result = self._calculate_metrics(daily_values, trades, all_dates)
        result.trades = trades

        # 分段样本外验证
        split = int(len(all_dates) * self.config.train_ratio)
        if 0 < split < len(all_dates):
            is_dates = all_dates[:split]
            oos_dates = all_dates[split:]
            # daily_values 有 n+1 项，values[i] 为第 i 日开盘前市值，values[n] 为期末清仓后
            is_values = daily_values[:split + 1]
            oos_values = daily_values[split:]

            is_trades = [t for t in trades if self._trade_date(t) < oos_dates[0]]
            oos_trades = [t for t in trades if self._trade_date(t) >= oos_dates[0]]

            result.in_sample = self._calculate_metrics(is_values, is_trades, is_dates)
            result.out_of_sample = self._calculate_metrics(oos_values, oos_trades, oos_dates)

        return result

    def _get_trading_dates(self) -> List[str]:
        """获取回测期间的交易日（全市场去重，不依赖单只股票代码）"""
        if self.db and hasattr(self.db, 'get_trading_dates'):
            return self.db.get_trading_dates(self.config.start_date, self.config.end_date)
        return []

    def _get_row(self, ts_code: str, trade_date: str) -> Optional[Dict]:
        """获取某日完整 K 线行（带缓存）"""
        key = (ts_code, trade_date)
        if key in self._row_cache:
            return self._row_cache[key]
        rows = self.db.get_kline(ts_code, trade_date, trade_date, limit=1)
        row = rows[0] if rows else None
        self._row_cache[key] = row
        return row

    def _get_price(self, ts_code: str, trade_date: str) -> float:
        """获取某日收盘价（压力测试钩子：可对价格施加扰动）"""
        row = self._get_row(ts_code, trade_date)
        price = row['close'] if row else 0
        if price > 0 and self._price_perturbation:
            price = self._price_perturbation(ts_code, trade_date, price)
        return price

    def set_price_perturbation(self, fn: Callable):
        """压力测试：注入价格扰动函数 (ts_code, trade_date, price) -> perturbed_price"""
        self._price_perturbation = fn

    def set_forced_limit_down(self, dates):
        """压力测试：强制这些日期跌停封死（不可卖出）"""
        self._forced_limit_down_dates = set(dates or [])

    def _limit_pct(self, ts_code: str) -> float:
        """涨跌停幅度：创业板/科创板 20%，北交所 30%，其余 10%"""
        if ts_code.startswith(('30', '68')):
            return 0.20
        if ts_code.startswith(('8', '4')):
            return 0.30
        return 0.10

    def _is_limit_up(self, ts_code: str, trade_date: str) -> bool:
        """涨停封死（不可买入）"""
        if not self.config.limit_up_down:
            return False
        row = self._get_row(ts_code, trade_date)
        if not row or row.get('change_pct') is None:
            return False
        limit = self._limit_pct(ts_code) * 100
        return row['change_pct'] >= limit - 0.3

    def _is_limit_down(self, ts_code: str, trade_date: str) -> bool:
        """跌停封死（不可卖出，压力测试可强制封死）"""
        if not self.config.limit_up_down:
            return False
        if trade_date in self._forced_limit_down_dates:
            return True
        row = self._get_row(ts_code, trade_date)
        if not row or row.get('change_pct') is None:
            return False
        limit = self._limit_pct(ts_code) * 100
        return row['change_pct'] <= -(limit - 0.3)

    def _mark_to_market(self, positions: Dict, trade_date: str) -> float:
        """按市价计算持仓"""
        total = 0
        for ts_code, pos in positions.items():
            price = self._get_price(ts_code, trade_date)
            if price > 0:
                pos['current_price'] = price
                total += price * pos['shares']
        return total

    def _check_exits(self, positions: Dict, trade_date: str) -> List[Dict]:
        """检查止损止盈（T+1 / 跌停限制内）"""
        orders = []
        for ts_code, pos in list(positions.items()):
            # T+1：当日买入不可当日卖出
            if self.config.t_plus_1 and pos.get('trade_date') == trade_date:
                continue
            # 跌停封死：无法卖出，延后
            if self._is_limit_down(ts_code, trade_date):
                continue

            price = self._get_price(ts_code, trade_date)
            if price <= 0:
                continue
            pnl_pct = (price / pos['avg_cost'] - 1) * 100
            if pnl_pct <= -self.config.stop_loss_pct:
                orders.append(self._make_sell_order(pos, price, 'stop_loss', trade_date))
            elif pnl_pct >= self.config.take_profit_pct:
                orders.append(self._make_sell_order(pos, price, 'take_profit', trade_date))
        return orders

    def _execute_buy(self, signal: Dict, cash: float, trade_date: str) -> Optional[Dict]:
        """执行买入（涨停封死则放弃）"""
        ts_code = signal['ts_code']
        if self._is_limit_up(ts_code, trade_date):
            return None
        price = self._get_price(ts_code, trade_date)
        if price <= 0:
            return None

        # 滑点
        price *= (1 + self.config.slippage)

        amount = cash * min(signal.get('position_pct', 0.35), self.config.max_single_pct)
        shares = int(amount / price / 100) * 100
        if shares < 100:
            return None

        amount = shares * price
        fee = amount * (self.config.commission_rate + self.config.transfer_fee_rate)
        fee = max(fee, self.config.min_broker_fee)

        return {
            'ts_code': ts_code,
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

    def _make_sell_order(self, pos: Dict, price: float, reason: str, exit_date: str) -> Dict:
        # 卖出侧滑点：成交价向下偏移
        fill_price = price * (1 - self.config.slippage)
        amount = fill_price * pos['shares']
        fee = amount * (self.config.commission_rate + self.config.stamp_tax_rate
                        + self.config.transfer_fee_rate)
        return {**pos, 'direction': 'sell', 'price': fill_price, 'amount': amount, 'fee': fee,
                'exit_reason': reason, 'exit_date': exit_date,
                'pnl': (fill_price - pos['avg_cost']) * pos['shares'],
                'pnl_pct': (fill_price / pos['avg_cost'] - 1) * 100}

    def _trade_date(self, trade: Dict) -> str:
        """统一取交易日：卖出用 exit_date，买入用 trade_date"""
        return trade.get('exit_date') or trade.get('trade_date', '')

    def _calculate_metrics(self, daily_values: List[float],
                           trades: List[Dict], dates: List[str]) -> BacktestResult:
        """计算回测绩效指标（含换手率/收益来源拆解，不含分段）"""
        values = np.array(daily_values)
        if len(values) < 2:
            return BacktestResult()

        returns = np.diff(values) / values[:-1]

        result = BacktestResult()
        result.total_return = (values[-1] / values[0] - 1) * 100
        if len(dates) >= 2:
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

        # 换手率（单边）：总买入金额 / 初始资金
        total_buy = sum(t['amount'] for t in trades if t['direction'] == 'buy')
        result.turnover_rate = total_buy / self.config.initial_capital if self.config.initial_capital > 0 else 0

        # 收益来源拆解：毛盈利 / 毛亏损 / 总成本 / 净盈亏
        gross_profit = sum(t.get('pnl', 0) for t in sell_trades if t.get('pnl', 0) > 0)
        gross_loss = sum(t.get('pnl', 0) for t in sell_trades if t.get('pnl', 0) < 0)
        total_fees = sum(t.get('fee', 0) for t in trades)
        result.return_attribution = {
            'gross_profit': round(gross_profit, 2),
            'gross_loss': round(gross_loss, 2),
            'total_fees': round(total_fees, 2),
            'net_pnl': round(gross_profit + gross_loss - total_fees, 2),
        }

        # 平均持仓天数
        hold_days = []
        for t in sell_trades:
            bd, ed = t.get('trade_date'), t.get('exit_date')
            if bd and ed:
                try:
                    hold_days.append((datetime.strptime(ed, '%Y-%m-%d') - datetime.strptime(bd, '%Y-%m-%d')).days)
                except (ValueError, TypeError):
                    pass
        result.avg_hold_days = float(np.mean(hold_days)) if hold_days else 0.0

        return result
