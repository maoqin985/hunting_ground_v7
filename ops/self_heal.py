"""
猎场系统 V6.0 — 自愈守护（每分钟运行）
检查核心进程：止损监控/持仓监控/Web面板，挂了自动拉起
"""

import asyncio
import logging
from typing import Dict, List, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


class SelfHeal:
    """自愈守护进程"""

    def __init__(self):
        self._watchdogs: Dict[str, Dict] = {}
        self._restart_count: Dict[str, int] = {}
        self._max_restarts = 5  # 5次失败后熔断
        self._restart_window = 300  # 5分钟窗口

    def register(self, name: str, check_fn: Callable, restart_fn: Callable = None):
        """注册守护对象"""
        self._watchdogs[name] = {
            'check': check_fn,
            'restart': restart_fn,
            'healthy': True,
            'last_check': None,
            'last_restart': None,
            'failure_count': 0,
        }
        self._restart_count[name] = 0
        logger.info(f"Watchdog registered: {name}")

    async def check_all(self) -> Dict:
        """检查所有守护对象"""
        now = datetime.now()
        status = {'healthy': True, 'details': {}}

        for name, wd in self._watchdogs.items():
            wd['last_check'] = now

            try:
                is_healthy = await wd['check']() if asyncio.iscoroutinefunction(wd['check']) else wd['check']()

                if not is_healthy:
                    wd['failure_count'] += 1
                    wd['healthy'] = False

                    # 熔断检查
                    if wd['failure_count'] > self._max_restarts:
                        logger.critical(f"SELF_HEAL CIRCUIT BREAKER: {name} failed {wd['failure_count']} times")
                        status['details'][name] = 'circuit_breaker_triggered'
                        status['healthy'] = False
                        continue

                    # 尝试重启
                    if wd['restart']:
                        logger.warning(f"Restarting {name} (attempt {wd['failure_count']})")
                        try:
                            if asyncio.iscoroutinefunction(wd['restart']):
                                await wd['restart']()
                            else:
                                wd['restart']()
                            wd['last_restart'] = now
                            wd['healthy'] = True
                            wd['failure_count'] = 0
                            status['details'][name] = 'restarted'
                        except Exception as e:
                            logger.error(f"Failed to restart {name}: {e}")
                            status['details'][name] = f'restart_failed: {e}'
                            status['healthy'] = False
                    else:
                        status['details'][name] = 'no_restart_handler'
                        status['healthy'] = False
                else:
                    wd['healthy'] = True
                    wd['failure_count'] = max(0, wd['failure_count'] - 1)
                    status['details'][name] = 'ok'

            except Exception as e:
                logger.error(f"Watchdog check error for {name}: {e}")
                status['details'][name] = f'check_error: {e}'

        if not any(wd['healthy'] for wd in self._watchdogs.values()):
            status['healthy'] = False

        return status

    def reset(self, name: str):
        """重置守护对象状态"""
        if name in self._watchdogs:
            self._watchdogs[name]['failure_count'] = 0
            self._watchdogs[name]['healthy'] = True

    def get_status(self) -> Dict:
        return {
            name: {
                'healthy': wd['healthy'],
                'failure_count': wd['failure_count'],
                'last_check': wd['last_check'].isoformat() if wd['last_check'] else None,
            }
            for name, wd in self._watchdogs.items()
        }
