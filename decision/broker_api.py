"""
猎场系统 V6.0 — 券商API适配层
支持: 华泰(xtclient) / 国金 / 东方财富(EasyXT)
模式: 实盘 + 模拟
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class BrokerAPI:
    """
    券商API统一适配层
    当前支持: 模拟交易模式（paper trading）
    实盘预留: QMT(miniQMT) / 华泰xtclient / 国金
    """

    def __init__(self, config: dict = None, db=None):
        self.config = config or {}
        self.db = db
        self._mode = config.get('system', {}).get('mode', 'paper')
        self._broker = config.get('broker', {})

        if self._mode == 'live':
            logger.warning("LIVE TRADING MODE — orders will be sent to broker!")
        else:
            logger.info("PAPER TRADING MODE — orders are simulated only")

    async def submit_order(self, order: Dict) -> Dict:
        """
        提交订单到券商或模拟执行

        Returns:
            更新后的订单（含status/order_id/fill_price）
        """
        if self._mode == 'paper':
            return await self._paper_execute(order)
        elif self._mode == 'live':
            return await self._live_execute(order)
        else:
            order['status'] = OrderStatus.REJECTED
            order['error'] = f"Unknown mode: {self._mode}"
            return order

    async def cancel_order(self, order_id: str) -> bool:
        """撤销订单"""
        if self._mode == 'paper':
            logger.info(f"Paper cancel: {order_id}")
            return True
        return False  # 实盘需要对接

    async def get_positions(self) -> List[Dict]:
        """获取券商端持仓"""
        if self._mode == 'paper':
            return self.db.get_all_positions()
        return []

    async def get_account_info(self) -> Dict:
        """获取账户信息"""
        if self._mode == 'paper':
            positions = self.db.get_all_positions()
            position_value = sum(p['market_value'] for p in positions if p.get('market_value'))
            # 从数据库获取最新快照
            return {
                'total_value': 20000.0,
                'cash': 20000.0 - position_value,
                'position_value': position_value,
                'position_count': len(positions),
            }
        return {}

    async def _paper_execute(self, order: Dict) -> Dict:
        """模拟执行"""
        order['order_id'] = f"PAPER_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        order['status'] = OrderStatus.FILLED
        order['fill_price'] = order.get('price', 0)
        order['filled_at'] = datetime.now().isoformat()

        # 记录到数据库
        try:
            trade_record = {
                'ts_code': order['ts_code'],
                'name': order.get('name', ''),
                'direction': order['direction'],
                'price': order['fill_price'],
                'volume': order['shares'],
                'amount': order['fill_price'] * order['shares'],
                'fee': order['fill_price'] * order['shares'] * 0.00075,
                'trade_time': order['filled_at'],
                'signal_id': None,
                'decision_model': 'ensemble',
                'pnl': order.get('pnl_amount', 0),
                'pnl_pct': order.get('pnl_pct', 0),
                'notes': order.get('reasoning', ''),
            }
            self.db.save_trade(trade_record)

            # 更新持仓
            if order['direction'] == 'buy':
                self.db.update_position(
                    order['ts_code'], order.get('name', ''),
                    order['shares'], order['fill_price'],
                    order['fill_price'],
                    datetime.now().strftime('%Y-%m-%d'),
                )
            elif order['direction'] == 'sell':
                self.db.remove_position(order['ts_code'])

        except Exception as e:
            logger.error(f"Paper execute DB error: {e}")

        logger.info(f"PAPER FILLED: {order['direction']} {order['ts_code']} "
                    f"{order['shares']}shares @ {order['fill_price']:.2f}")
        return order

    async def _live_execute(self, order: Dict) -> Dict:
        """实盘执行（预留）"""
        order['status'] = OrderStatus.PENDING
        order['error'] = "Live trading not yet implemented"
        logger.warning(f"Live order stub: {order['direction']} {order['ts_code']}")
        return order
