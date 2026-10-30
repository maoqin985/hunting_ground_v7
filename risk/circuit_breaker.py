"""
猎场系统 V7.0 — 五层递进熔断系统（per-account）
L1: 个股硬止损 -5%  →  立即平仓
L2: 个股日损 -3%    →  次日禁止该股
L3: 组合日损 -4%    →  禁止新开仓
L4: 组合周损 -8%    →  全部平仓+本周停止
L5: 大盘熔断        →  清仓+暂停
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """五层递进熔断控制器（per-account）"""

    def __init__(self, config: dict = None, db=None, account_id: str = None):
        self.config = config or {}
        self.db = db
        self.account_id = account_id  # V7.0: per-account scope
        self._active_breakers: Dict[int, Dict] = {}
        self._stock_ban_list: Dict[str, datetime] = {}  # 个股禁买截止时间

        # 加载配置
        risk_cfg = config.get("circuit_breakers", [])
        self._breakers_cfg = {cb['level']: cb for cb in risk_cfg}

        # 组合状态
        self._daily_pnl_start = 0.0
        self._weekly_pnl_start = 0.0
        self._daily_reset_time = datetime.now().replace(hour=0, minute=0, second=0)
        self._weekly_reset_time = datetime.now().replace(hour=0, minute=0, second=0)

    def check_all(self, account_id: str = None) -> Dict:
        """
        检查所有熔断层

        Returns:
            {
                'block_new_positions': bool,
                'block_all_trading': bool,
                'active_breakers': [{level, name, action, remaining_cooldown}],
            }
        """
        result = {
            'block_new_positions': False,
            'block_all_trading': False,
            'active_breakers': [],
        }

        now = datetime.now()
        expired = []

        for level, breaker in list(self._active_breakers.items()):
            cooldown_end = breaker.get('cooldown_end')
            if cooldown_end and now >= cooldown_end:
                expired.append(level)
                # 解析熔断
                if self.db:
                    self.db.resolve_circuit_breaker(breaker.get('db_id'))
                logger.info(f"[{self.account_id}] Circuit breaker L{level} ({breaker['name']}) COOLDOWN EXPIRED — auto resumed")
                continue

            result['active_breakers'].append({
                'level': level,
                'name': breaker['name'],
                'action': breaker['action'],
                'triggered_at': breaker['triggered_at'],
                'remaining_cooldown': str(cooldown_end - now) if cooldown_end else 'manual_resume',
            })

            if breaker['action'] in ('no_new_position', 'close_all_halt', 'close_all_pause'):
                result['block_new_positions'] = True
            if breaker['action'] in ('close_all_halt', 'close_all_pause'):
                result['block_all_trading'] = True

        for level in expired:
            del self._active_breakers[level]

        # 检查个股禁买
        expired_stocks = [c for c, t in self._stock_ban_list.items() if now >= t]
        for c in expired_stocks:
            del self._stock_ban_list[c]

        return result

    def trigger_l1(self, ts_code: str, pnl_pct: float) -> bool:
        """L1: 个股硬止损"""
        if pnl_pct <= -0.05:
            self._activate(1, f"{ts_code}亏损{pnl_pct:.1%}", "close_position")
            logger.warning(f"CIRCUIT BREAKER L1: {ts_code} stop loss @ {pnl_pct:.1%}")
            return True
        return False

    def trigger_l2(self, ts_code: str, daily_pnl_pct: float) -> bool:
        """L2: 个股日度亏损"""
        if daily_pnl_pct <= -0.03:
            self._activate(2, f"{ts_code}日亏{daily_pnl_pct:.1%}", "ban_stock_1day")
            self._stock_ban_list[ts_code] = datetime.now() + timedelta(days=1)
            logger.warning(f"CIRCUIT BREAKER L2: {ts_code} banned for 1 day")
            return True
        return False

    def trigger_l3(self, daily_pnl_pct: float) -> bool:
        """L3: 组合日度亏损"""
        if daily_pnl_pct <= -0.04:
            self._activate(3, f"组合日亏{daily_pnl_pct:.1%}", "no_new_position",
                           cooldown_hours=24)
            logger.warning(f"CIRCUIT BREAKER L3: No new positions for 24h")
            return True
        return False

    def trigger_l4(self, weekly_pnl_pct: float) -> bool:
        """L4: 组合周度亏损"""
        if weekly_pnl_pct <= -0.08:
            self._activate(4, f"组合周亏{weekly_pnl_pct:.1%}", "close_all_halt",
                           cooldown_hours=168)  # 7天
            logger.warning(f"CIRCUIT BREAKER L4: Close all + halt for 1 week!")
            return True
        return False

    def trigger_l5(self, hs300_change: float, limit_down_count: int = 0) -> bool:
        """L5: 大盘熔断"""
        if hs300_change <= -0.05 or limit_down_count >= 100:
            reason = f"沪深300跌{hs300_change:.1%}" if hs300_change <= -0.05 else f"跌停{limit_down_count}家"
            self._activate(5, reason, "close_all_pause", cooldown_hours=24)
            logger.warning(f"CIRCUIT BREAKER L5: MARKET MELTDOWN — {reason}")
            return True
        return False

    def _activate(self, level: int, reason: str, action: str, cooldown_hours: float = 0):
        """激活熔断"""
        now = datetime.now()
        cooldown_end = now + timedelta(hours=cooldown_hours) if cooldown_hours > 0 else None

        breaker = {
            'level': level,
            'name': self._breakers_cfg.get(level, {}).get('name', f'L{level}'),
            'trigger_reason': reason,
            'action': action,
            'triggered_at': now,
            'cooldown_end': cooldown_end,
        }

        # 记数据库
        if self.db:
            db_id = self.db.log_circuit_breaker(level, breaker['name'], reason, action,
                                                account_id=self.account_id)
            breaker['db_id'] = db_id

        self._active_breakers[level] = breaker

    def is_stock_banned(self, ts_code: str) -> bool:
        """检查个股是否在禁买期"""
        if ts_code in self._stock_ban_list:
            if datetime.now() < self._stock_ban_list[ts_code]:
                return True
            else:
                del self._stock_ban_list[ts_code]
        return False

    def manual_resume(self, level: int):
        """人工恢复熔断"""
        if level in self._active_breakers:
            breaker = self._active_breakers.pop(level)
            if self.db and breaker.get('db_id'):
                self.db.resolve_circuit_breaker(breaker['db_id'])
            logger.info(f"Circuit breaker L{level} manually resumed")

    def get_status(self) -> Dict:
        return {
            'active_count': len(self._active_breakers),
            'breakers': [
                {'level': l, 'name': b['name'], 'action': b['action'],
                 'triggered_at': b['triggered_at'].isoformat()}
                for l, b in self._active_breakers.items()
            ],
            'banned_stocks': list(self._stock_ban_list.keys()),
        }

    def get_dashboard_status(self) -> Dict:
        """供 Web 面板的完整熔断状态（含冷却倒计时、禁买名单、触发原因）。"""
        now = datetime.now()
        return {
            'active_count': len(self._active_breakers),
            'breakers': [
                {
                    'level': l,
                    'name': b['name'],
                    'action': b['action'],
                    'trigger_reason': b.get('trigger_reason', ''),
                    'triggered_at': b['triggered_at'].isoformat() if b.get('triggered_at') else None,
                    'remaining_cooldown_seconds': (
                        max(0, int((b['cooldown_end'] - now).total_seconds()))
                        if b.get('cooldown_end') else None
                    ),
                }
                for l, b in self._active_breakers.items()
            ],
            'banned_stocks': [
                {'ts_code': c, 'until': t.isoformat()}
                for c, t in self._stock_ban_list.items()
            ],
        }
