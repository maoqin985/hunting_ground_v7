"""
猎场系统 V7.0 — 主交易决策引擎（per-account）
整合 信号扫描 → AI评分 → 信号验证 → 仓位计算 → 订单执行 全流程
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime
import asyncio

from .validator import SignalValidator
from .position_sizer import PositionSizer

logger = logging.getLogger(__name__)


class AutoTradingEngine:
    """
    AI 自主交易决策引擎

    决策流程:
    1. 接收融合信号 → 2. AI 模型评分 → 3. 十二层验证
    4. 仓位计算 → 5. 熔断检查 → 6. 输出订单
    """

    def __init__(self, config: dict, db, data_gateway,
                 signal_fusion, model_ensemble, circuit_breaker,
                 account_id: str = None, account=None):
        self.config = config
        self.db = db
        self.gateway = data_gateway
        self.signal_fusion = signal_fusion
        self.model_ensemble = model_ensemble
        self.circuit_breaker = circuit_breaker
        self.account_id = account_id  # V7.0: per-account isolation
        self.account = account  # V7.0: VirtualAccount reference

        # V7.0: per-account params from VirtualAccount
        total_capital = account.initial_capital if account else None
        strat_params = account.strategy_params if account else {}
        max_positions = strat_params.get('max_positions') if account else None
        max_single_pct = strat_params.get('max_single_pct') if account else None

        self.validator = SignalValidator(config, db, data_gateway)
        self.position_sizer = PositionSizer(
            config,
            total_capital=total_capital,
            max_positions=max_positions,
            max_single_pct=max_single_pct,
        )

        # 止损/止盈阈值（从 config 读取，避免与 risk.yaml 硬编码漂移）
        sl_cfg = config.get('stop_loss', {})
        tp_cfg = config.get('take_profit', {})
        self.hard_stop_pct = float(sl_cfg.get('a_share', {}).get('hard_stop', -0.05)) * 100
        self.take_profit_pct = float(tp_cfg.get('a_share', {}).get('target_pct', 0.20)) * 100

        self._daily_trades = []
        self._daily_pnl = 0.0
        self._trading_enabled = True
        self._manual_confirm_required = config.get('system', {}).get('mode') == 'live'

    async def run_cycle(self, signal_groups: Dict[str, List[Dict]],
                        kline_map: Dict[str, List[Dict]],
                        feature_map: Dict = None,
                        market_data: Dict = None):
        """
        执行一次完整的交易决策周期

        Returns:
            (订单列表, AI 评估列表)
        """
        orders = []
        evaluations = []

        # 检查熔断状态
        if not self._trading_enabled:
            logger.info("Trading disabled (circuit breaker active)")
            return orders, evaluations

        breaker_status = self.circuit_breaker.check_all()
        if breaker_status['block_new_positions']:
            logger.warning(f"Circuit breaker blocking new positions: {breaker_status['active_breakers']}")
            # 只处理平仓指令
            return await self._check_exits(market_data), evaluations

        # 1. 融合信号
        fused = self.signal_fusion.fuse(signal_groups)
        top_signals = self.signal_fusion.get_top_signals(fused, top_n=50, min_score=5)

        if not top_signals:
            logger.info(f"[{self.account_id}] 融合后无有效信号 (min_score=5)")
            return orders, evaluations

        logger.info(f"[{self.account_id}] 融合信号: {len(top_signals)}个, 最佳: {top_signals[0]['ts_code']} ({top_signals[0]['total_score']:.1f}分)")

        # 2. 获取当前持仓
        positions = self.db.get_all_positions(account_id=self.account_id)
        position_count = len(positions)
        current_codes = {p['ts_code'] for p in positions}

        # 3. 逐信号评估
        for sig in top_signals[:10]:  # 每次最多评估10个
            ts_code = sig['ts_code']

            # 标准化: 融合信号用 total_score，校验器/后续用 score
            sig['score'] = sig.get('total_score', sig.get('score', 0))

            # 跳过已持仓
            if ts_code in current_codes:
                evaluations.append({'ts_code': ts_code, 'action': 'skip', 'score': sig.get('score', 0),
                                    'confidence': 0, 'risk_level': None, 'model_votes': {},
                                    'reasoning': '已持仓，跳过', 'blocked_by': 'already_held'})
                continue

            # L2 禁买名单：该股当日已触发个股日亏熔断
            if self.circuit_breaker.is_stock_banned(ts_code):
                logger.info(f"[{self.account_id}] {ts_code} 在 L2 禁买期，跳过")
                evaluations.append({'ts_code': ts_code, 'action': 'skip', 'score': sig.get('score', 0),
                                    'confidence': 0, 'risk_level': None, 'model_votes': {},
                                    'reasoning': 'L2 禁买期', 'blocked_by': 'banned'})
                continue

            # 获取K线和特征
            kline = kline_map.get(ts_code, [])
            feature = feature_map.get(ts_code) if feature_map else None
            stock_info = self.db.get_stock_info(ts_code)

            # 4. AI 模型评分
            ensemble_decision = await self.model_ensemble.decide(
                ts_code, feature, kline, sig['total_score'],
                market_data=market_data,
            )

            eval_entry = {
                'ts_code': ts_code,
                'action': ensemble_decision.get('action', 'hold'),
                'score': ensemble_decision.get('score', sig.get('score', 0)),
                'confidence': ensemble_decision.get('confidence', 0),
                'risk_level': ensemble_decision.get('risk_level', 'medium'),
                'model_votes': ensemble_decision.get('model_votes', {}),
                'reasoning': ensemble_decision.get('reasoning', ''),
                'blocked_by': None,
            }

            # 5. 十二层验证
            validation = self.validator.validate(
                ts_code, sig, stock_info, market_data,
                ai_risk_prob=ensemble_decision.get('model_votes', {}).get('risk_prob', 0.0),
            )

            if not validation.passed:
                eval_entry['blocked_by'] = getattr(validation, 'blocked_by', 'validation')
                evaluations.append(eval_entry)
                logger.info(f"[{self.account_id}] {ts_code} 被拒: {validation.blocked_by} — {validation.details.get('reject_reason')}")
                # 记录被拒绝信号
                self.db.save_signal(ts_code, datetime.now().strftime('%Y-%m-%d'),
                                    'rejected', sig['total_score'],
                                    {'reason': validation.blocked_by,
                                     'detail': validation.details.get('reject_reason', '')},
                                    account_id=self.account_id)
                continue

            # 6. 仓位计算
            if ensemble_decision['action'] != 'buy':
                eval_entry['blocked_by'] = 'model_hold'
                evaluations.append(eval_entry)
                logger.info(f"[{self.account_id}] {ts_code} 评分{ensemble_decision['score']:.1f}分 -> 模型决策: {ensemble_decision['action']}")
                continue

            evaluations.append(eval_entry)  # 买入意图已记录（后续价格/仓位失败仍保留 AI 评估）

            # 获取实时价格
            quotes = await self.gateway.get_realtime_quote([ts_code])
            price = quotes.get(ts_code, {}).get('price', 0)
            if price <= 0:
                # 用最近K线收盘价fallback
                price = kline[-1]['close'] if kline else 0
                if price <= 0:
                    continue

            sizing = self.position_sizer.calculate(
                ts_code, price,
                signal_score=ensemble_decision['score'],
                ai_suggested_pct=ensemble_decision['suggested_position_pct'],
                current_positions=position_count,
            )

            if sizing['shares'] <= 0:
                logger.debug(f"{ts_code} position size = 0 ({sizing['method']})")
                continue

            # 7. 检查是否替换末位（持仓已满时）
            if position_count >= self.position_sizer.max_positions:
                rebalance = self.position_sizer.calculate_rebalance(
                    {p['ts_code']: {'score': p.get('score', 0)} for p in positions},
                    sig,
                )
                if rebalance['action'] == 'replace':
                    # 卖出末位
                    exit_order = self._create_order(
                        rebalance['replace_code'], 'sell',
                        positions, 'replace_exit'
                    )
                    orders.append(exit_order)
                    position_count -= 1
                elif rebalance['action'] == 'skip':
                    continue

            # 8. 创建买入订单
            order = self._create_order(
                ts_code, 'buy', None, 'ai_signal',
                price=price,
                shares=sizing['shares'],
                score=ensemble_decision['score'],
                confidence=ensemble_decision['confidence'],
                stop_loss_pct=ensemble_decision['stop_loss_pct'],
                reasoning=ensemble_decision['reasoning'],
                position_pct=sizing['position_pct'],
                decision_source=ensemble_decision.get('decision_source', 'rule_baseline'),
            )
            orders.append(order)

            # 9. 记录信号
            self.db.save_signal(ts_code, datetime.now().strftime('%Y-%m-%d'),
                                'buy', ensemble_decision['score'],
                                {'reason': ensemble_decision['reasoning'],
                                 'confidence': ensemble_decision['confidence'],
                                 'position_pct': sizing['position_pct']},
                                account_id=self.account_id)

            position_count += 1

            # 持仓已满
            if position_count >= self.position_sizer.max_positions:
                break

        # 10. 检查是否需要止盈止损平仓
        exit_orders = await self._check_exits(market_data)
        orders.extend(exit_orders)

        if orders:
            logger.info(f"Cycle generated {len(orders)} orders: "
                        f"{[f'{o['direction']} {o['ts_code']}' for o in orders]}")

        return orders, evaluations

    async def _check_exits(self, market_data: Dict = None) -> List[Dict]:
        """检查持仓是否需要止盈止损（阈值从 config 读取，T+1 当日买入不触发）"""
        orders = []
        positions = self.db.get_all_positions(account_id=self.account_id)

        if not positions:
            return orders

        # 获取实时行情
        codes = [p['ts_code'] for p in positions]
        quotes = await self.gateway.get_realtime_quote(codes)

        today = datetime.now().strftime('%Y-%m-%d')

        for pos in positions:
            ts_code = pos['ts_code']
            quote = quotes.get(ts_code, {})
            current_price = quote.get('price', pos.get('current_price', 0))
            if current_price <= 0:
                continue

            # T+1保护：当天买入的不触发止损/止盈（次日生效）
            if pos.get('buy_date', '') == today:
                continue

            pnl_pct = (current_price / pos['avg_cost'] - 1) * 100 if pos['avg_cost'] > 0 else 0

            # 止损
            if pnl_pct <= self.hard_stop_pct:
                orders.append(self._create_order(
                    ts_code, 'sell', [pos], 'stop_loss',
                    price=current_price,
                    shares=pos['volume'],
                    pnl_pct=pnl_pct,
                    reasoning=f'触发{self.hard_stop_pct:.0f}%硬止损（当前盈亏{pnl_pct:.1f}%）',
                ))
                logger.warning(f"STOP LOSS: {ts_code} @ {pnl_pct:.1f}%")

            # 止盈
            elif pnl_pct >= self.take_profit_pct:
                orders.append(self._create_order(
                    ts_code, 'sell', [pos], 'take_profit',
                    price=current_price,
                    shares=pos['volume'],
                    pnl_pct=pnl_pct,
                    reasoning=f'触发+{self.take_profit_pct:.0f}%止盈（当前盈亏{pnl_pct:.1f}%）',
                ))
                logger.info(f"TAKE PROFIT: {ts_code} @ {pnl_pct:.1f}%")

        return orders

    def _create_order(self, ts_code: str, direction: str,
                      position: Optional[List] = None,
                      order_type: str = 'ai_signal',
                      **kwargs) -> Dict:
        """创建标准订单"""
        order = {
            'ts_code': ts_code,
            'direction': direction,
            'type': order_type,
            'price': kwargs.get('price', 0),
            'shares': kwargs.get('shares', 0),
            'amount': kwargs.get('price', 0) * kwargs.get('shares', 0),
            'score': kwargs.get('score', 0),
            'confidence': kwargs.get('confidence', 0),
            'stop_loss_pct': kwargs.get('stop_loss_pct', -0.05),
            'reasoning': kwargs.get('reasoning', ''),
            'timestamp': datetime.now().isoformat(),
            'status': 'pending',
            'pnl_pct': kwargs.get('pnl_pct', 0),
            'decision_source': kwargs.get('decision_source', ''),
        }

        if direction == 'sell' and position:
            order['avg_cost'] = position[0].get('avg_cost', 0) if position else 0
            order['hold_days'] = position[0].get('hold_days', 0) if position else 0

        return order

    def enable_trading(self):
        self._trading_enabled = True
        logger.info("Trading ENABLED")

    def disable_trading(self):
        self._trading_enabled = False
        logger.warning("Trading DISABLED")
