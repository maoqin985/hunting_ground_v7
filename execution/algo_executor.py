"""
猎场系统 V7.0 — 算法执行引擎
编排拆单 → 滑点预估 → 择时 → 流动性适配 全流程
通过 EventBus 发布执行事件
"""

import logging
import asyncio
import uuid
from typing import Dict, List, Optional
from datetime import datetime

from .order_splitter import OrderSplitter
from .slippage import SlippageEstimator
from .timing import TimingOptimizer
from .liquidity import LiquidityAdapter

logger = logging.getLogger(__name__)


class AlgoExecutor:
    """
    智能算法执行器

    执行管线:
    ORDER_SUBMITTED → ORDER_SPLIT → [ORDER_SLICE_FILLED × N] → ORDER_COMPLETED

    发布事件:
    - ORDER_SPLIT: 拆单完成
    - ORDER_SLICE_FILLED: 子单成交
    - ORDER_COMPLETED: 整单完成
    - ORDER_CANCELLED: 取消
    """

    def __init__(self, event_bus=None, db=None, account_id: str = None):
        self.event_bus = event_bus
        self.db = db
        self.account_id = account_id

        self.splitter = OrderSplitter()
        self.slippage = SlippageEstimator()
        self.timing = TimingOptimizer()
        self.liquidity = LiquidityAdapter()

        self._active_orders: Dict[str, Dict] = {}  # order_id -> order_state
        self._filled_orders: List[Dict] = []

    async def submit_order(self, order: Dict,
                            market_data: Dict = None) -> Optional[str]:
        """
        提交订单进行算法执行

        Args:
            order: {
                'ts_code', 'direction', 'price', 'shares',
                'algo': 'TWAP'|'VWAP'|'POV'|'Iceberg' (可选),
                'urgency': 'low'|'normal'|'high',
            }
            market_data: 实时市场数据

        Returns:
            order_id or None (被拒绝)
        """
        order_id = str(uuid.uuid4())[:8]
        ts_code = order.get('ts_code', '')
        shares = order.get('shares', 0)
        price = order.get('price', 0)
        direction = order.get('direction', 'buy')
        algo = order.get('algo', 'TWAP')
        urgency = order.get('urgency', 'normal')

        if shares <= 0 or price <= 0:
            logger.warning(f"Invalid order: shares={shares}, price={price}")
            return None

        # 1. 流动性检查
        daily_volume = market_data.get('daily_volume', 0) if market_data else 0
        daily_amount = market_data.get('daily_amount', 0) if market_data else 0
        recent_vol = market_data.get('volume_5min', 0) if market_data else 0

        liq = self.liquidity.check(
            ts_code, shares, recent_vol, daily_volume, daily_amount
        )
        adjusted_shares = liq['adjusted_shares']

        if adjusted_shares < 100:
            logger.warning(f"Order {order_id} rejected: liquidity too low "
                          f"(adjusted to {adjusted_shares} shares)")
            return None

        # 2. 滑点预估
        order_amount = adjusted_shares * price
        daily_amt = daily_volume * price if daily_volume > 0 else None
        slip = self.slippage.estimate(
            ts_code, price, order_amount, daily_amt,
            volatility=market_data.get('volatility', 0.02) if market_data else 0.02,
            urgency=urgency,
        )

        # 3. 选择算法
        if not order.get('algo'):
            algo = slip['recommended_algo']

        # 4. 拆单
        split = self.splitter.split(
            ts_code, adjusted_shares, price,
            algo=algo,
            daily_volume=daily_volume,
            participation_rate=0.10 if algo == 'POV' else 0,
        )

        if not split:
            return None

        # 5. 记录订单状态
        order_state = {
            'order_id': order_id,
            'ts_code': ts_code,
            'direction': direction,
            'algo': algo,
            'total_shares': adjusted_shares,
            'original_shares': shares,
            'filled_shares': 0,
            'avg_fill_price': 0.0,
            'price': price,
            'slippage_bps': slip['slippage_bps'],
            'estimated_cost_bps': slip['slippage_bps'],
            'split': split,
            'status': 'submitted',
            'reduction_pct': liq['reduction_pct'],
            'liquidity_level': liq['liquidity_level'],
            'submitted_at': datetime.now().isoformat(),
            'started_at': None,
            'completed_at': None,
            'slice_fills': [],
        }
        self._active_orders[order_id] = order_state

        # 发布 ORDER_SPLIT 事件
        if self.event_bus:
            from core.event_bus import Event, EventType
            await self.event_bus.publish(Event(
                EventType.ORDER_SUBMITTED,
                data=order_state,
                account_id=self.account_id,
            ))

        logger.info(f"Order {order_id}: {direction} {adjusted_shares}@{price:.2f} "
                    f"[{algo}, {split['slices']} slices, est.slip {slip['slippage_bps']}bps]")

        return order_id

    async def execute_slices(self, order_id: str,
                              fill_callback=None) -> Dict:
        """
        逐片执行（模拟）

        Args:
            order_id: 订单ID
            fill_callback: async callback(ts_code, shares, price) -> fill_result

        Returns:
            执行结果
        """
        order = self._active_orders.get(order_id)
        if not order:
            return {'status': 'not_found', 'order_id': order_id}

        if order['status'] != 'submitted':
            return {'status': order['status'], 'order_id': order_id}

        order['status'] = 'executing'
        order['started_at'] = datetime.now().isoformat()
        schedule = order['split']['schedule']

        total_filled = 0
        total_cost = 0.0
        fills = []

        for i, slc in enumerate(schedule):
            # 检查市场是否适合执行
            if not self.timing.should_execute_now():
                delay = self.timing.next_execution_time(60)
                if delay is None:
                    logger.warning(f"Order {order_id}: market closed mid-execution")
                    break
                if delay > 0:
                    await asyncio.sleep(min(delay, 5))  # 最多等5秒（模拟）

            shares = slc['shares']
            fill_price = order['price']  # 模拟：无真实执行引擎

            if fill_callback:
                result = await fill_callback(order['ts_code'], shares, fill_price)
                if result:
                    fill_price = result.get('price', fill_price)

            total_filled += shares
            total_cost += shares * fill_price

            fill_record = {
                'seq': slc['seq'],
                'shares': shares,
                'price': fill_price,
                'filled_at': datetime.now().isoformat(),
            }
            fills.append(fill_record)

            # 发布每片子单成交事件
            if self.event_bus:
                from core.event_bus import Event, EventType
                await self.event_bus.publish(Event(
                    EventType.ORDER_FILLED,
                    data={
                        'order_id': order_id,
                        'ts_code': order['ts_code'],
                        'slice': fill_record,
                        'progress': f"{i + 1}/{len(schedule)}",
                    },
                    account_id=self.account_id,
                ))

            # 模拟执行间隔
            delay = slc.get('delay_min', 1) * 0.1  # 模拟加速
            if delay > 0:
                await asyncio.sleep(min(delay, 0.5))

        # 完成
        avg_price = total_cost / total_filled if total_filled > 0 else 0
        vwap_slippage = (avg_price / order['price'] - 1) * 10000
        actual_slippage = vwap_slippage if order['direction'] == 'buy' else -vwap_slippage

        order['status'] = 'completed'
        order['completed_at'] = datetime.now().isoformat()
        order['filled_shares'] = total_filled
        order['avg_fill_price'] = round(avg_price, 4)
        order['actual_slippage_bps'] = round(actual_slippage, 2)
        order['slice_fills'] = fills

        # 记录到数据库
        if self.db:
            self.db.save_execution_log({
                'account_id': self.account_id or '',
                'order_id': order_id,
                'ts_code': order['ts_code'],
                'direction': order['direction'],
                'strategy': order['algo'],
                'total_shares': order['total_shares'],
                'filled_shares': total_filled,
                'slices_planned': order['split']['slices_planned'],
                'slices_filled': len(fills),
                'avg_fill_price': avg_price,
                'vwap_price': order['price'],
                'slippage_bps': round(actual_slippage, 2),
                'execution_cost': order['estimated_cost_bps'],
                'started_at': order['started_at'],
                'completed_at': order['completed_at'],
                'status': 'completed',
                'details_json': {},
            })

        # 发布订单完成事件
        if self.event_bus:
            from core.event_bus import Event, EventType
            await self.event_bus.publish(Event(
                EventType.POSITION_UPDATED,
                data=order,
                account_id=self.account_id,
            ))

        # 滑点校准
        self.slippage.record_actual(
            order['ts_code'],
            order['estimated_cost_bps'],
            actual_slippage,
            order['algo'],
        )

        logger.info(f"Order {order_id} COMPLETED: {total_filled}/{order['total_shares']} "
                    f"@{avg_price:.4f}, slip={actual_slippage:.1f}bps")

        self._filled_orders.append(order)
        del self._active_orders[order_id]

        return order

    async def cancel_order(self, order_id: str) -> bool:
        """取消活跃订单"""
        order = self._active_orders.get(order_id)
        if not order:
            return False
        if order['status'] not in ('submitted', 'executing'):
            return False

        order['status'] = 'cancelled'
        order['completed_at'] = datetime.now().isoformat()

        if self.event_bus:
            from core.event_bus import Event, EventType
            await self.event_bus.publish(Event(
                EventType.ORDER_CANCELLED,
                data=order,
                account_id=self.account_id,
            ))

        self._filled_orders.append(order)
        del self._active_orders[order_id]
        logger.info(f"Order {order_id} cancelled ({order['filled_shares']}/{order['total_shares']} filled)")
        return True

    def get_active_orders(self) -> List[Dict]:
        """获取活跃订单摘要"""
        return [
            {
                'order_id': oid,
                'ts_code': o['ts_code'],
                'direction': o['direction'],
                'algo': o['algo'],
                'progress': f"{o['filled_shares']}/{o['total_shares']}",
                'status': o['status'],
                'slippage_bps': o.get('actual_slippage_bps', o.get('estimated_cost_bps', 0)),
            }
            for oid, o in self._active_orders.items()
        ]

    def get_execution_stats(self) -> Dict:
        """执行统计"""
        completed = self._filled_orders
        if not completed:
            return {'total_orders': 0}

        avg_slip = sum(abs(o.get('actual_slippage_bps', 0)) for o in completed) / len(completed)
        fill_rates = [
            o['filled_shares'] / o['total_shares']
            for o in completed if o['total_shares'] > 0
        ]
        avg_fill_rate = sum(fill_rates) / len(fill_rates) if fill_rates else 0

        return {
            'total_orders': len(completed),
            'active_orders': len(self._active_orders),
            'avg_slippage_bps': round(avg_slip, 2),
            'avg_fill_rate': round(avg_fill_rate, 2),
            'algo_breakdown': {
                algo: sum(1 for o in completed if o.get('algo') == algo)
                for algo in set(o.get('algo', '') for o in completed)
            },
        }
