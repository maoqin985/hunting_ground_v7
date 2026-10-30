"""
猎场系统 V7.0 — 实时止损监控（per-account）
每60秒检查持仓，触发止损立即平仓
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class StopLossMonitor:
    """
    止损/止盈实时监控
    - 每60秒检查所有持仓
    - T+1 保护：当天买入的不触发止损
    - 支持硬止损 + 移动止损
    """

    def __init__(self, config: dict = None, db=None, data_gateway=None, circuit_breaker=None,
                 account_id: str = None):
        self.config = config or {}
        self.db = db
        self.gateway = data_gateway
        self.circuit_breaker = circuit_breaker
        self.account_id = account_id  # V7.0: per-account scope

        sl_cfg = config.get("stop_loss", {})
        a_share_cfg = sl_cfg.get("a_share", {})
        self.hard_stop = a_share_cfg.get("hard_stop", -0.05)
        self.trailing_stop = a_share_cfg.get("trailing_stop", -0.07)
        self.trailing_activate = a_share_cfg.get("trailing_activate", 0.05)

        cb_cfg = sl_cfg.get("convertible_bond", {})
        self.cb_hard_stop = cb_cfg.get("hard_stop", -0.03)
        self.cb_trailing_stop = cb_cfg.get("trailing_stop", -0.04)

        # 移动止损记录
        self._trailing_highs: Dict[str, float] = {}

    async def check_all(self) -> List[Dict]:
        """检查所有持仓，返回需平仓的列表"""
        positions = self.db.get_all_positions(account_id=self.account_id)
        if not positions:
            return []

        # 获取实时行情
        codes = [p['ts_code'] for p in positions]
        quotes = await self.gateway.get_realtime_quote(codes)

        exit_orders = []
        today = datetime.now().strftime('%Y-%m-%d')

        for pos in positions:
            ts_code = pos['ts_code']
            quote = quotes.get(ts_code, {})
            current_price = quote.get('price', pos.get('current_price', 0))
            if current_price <= 0:
                continue

            pnl_pct = (current_price / pos['avg_cost'] - 1) * 100 if pos['avg_cost'] > 0 else 0
            is_cb = ts_code.startswith('12')  # 可转债代码

            # T+1 保护：今天买入的不触发止损
            buy_date = pos.get('buy_date', '')
            if buy_date == today:
                continue

            # 移动止损最高价更新
            if ts_code not in self._trailing_highs:
                self._trailing_highs[ts_code] = current_price
            else:
                self._trailing_highs[ts_code] = max(self._trailing_highs[ts_code], current_price)

            exit_reason = None

            # 硬止损
            stop_pct = self.cb_hard_stop if is_cb else self.hard_stop
            if pnl_pct <= stop_pct * 100:
                exit_reason = f"硬止损 ({pnl_pct:.1f}% <= {stop_pct*100:.0f}%)"

            # 移动止损
            elif pnl_pct >= self.trailing_activate * 100:
                trail_from_high = (current_price / self._trailing_highs[ts_code] - 1) * 100
                trail_stop = self.cb_trailing_stop if is_cb else self.trailing_stop
                if trail_from_high <= trail_stop * 100:
                    exit_reason = f"移动止损 (从最高点{self._trailing_highs[ts_code]:.2f}回撤{abs(trail_from_high):.1f}%)"

            if exit_reason:
                exit_orders.append({
                    'ts_code': ts_code,
                    'name': pos.get('name', ''),
                    'direction': 'sell',
                    'type': 'stop_loss',
                    'price': current_price,
                    'shares': pos['volume'],
                    'amount': current_price * pos['volume'],
                    'pnl': (current_price - pos['avg_cost']) * pos['volume'],
                    'pnl_pct': pnl_pct,
                    'avg_cost': pos['avg_cost'],
                    'hold_days': pos.get('hold_days', 0),
                    'reasoning': exit_reason,
                    'timestamp': datetime.now().isoformat(),
                })

                # 触发 L1 熔断
                if self.circuit_breaker:
                    self.circuit_breaker.trigger_l1(ts_code, pnl_pct / 100)

                # 清理移动止损记录
                self._trailing_highs.pop(ts_code, None)

                logger.warning(f"STOP LOSS: {ts_code} — {exit_reason}")

        return exit_orders

    def reset_trailing_high(self, ts_code: str):
        """重置移动止损最高价"""
        self._trailing_highs.pop(ts_code, None)

    def get_trailing_status(self) -> Dict:
        return {
            ts_code: {
                'high': high,
                'current_drawdown': 0.0,  # 需实时查询
            }
            for ts_code, high in self._trailing_highs.items()
        }
