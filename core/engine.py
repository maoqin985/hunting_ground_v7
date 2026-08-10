"""
猎场系统 V6.0 — 异步事件驱动主引擎
"""

import asyncio
import logging
import signal
from typing import Optional

from .event_bus import EventBus, Event, EventType, get_event_bus
from .scheduler import Scheduler

logger = logging.getLogger(__name__)


class Engine:
    """系统主引擎 — 管理所有子系统生命周期"""

    def __init__(self, config: dict):
        self.config = config
        self.state = "stopped"
        self.event_bus = get_event_bus()
        self.scheduler = Scheduler(event_bus=self.event_bus)
        self._subsystems = {}
        self._startup_time = None

    def register_subsystem(self, name: str, instance):
        self._subsystems[name] = instance
        logger.info(f"Subsystem registered: {name}")

    async def start(self):
        if self.state == "running":
            return
        self.state = "starting"

        mode = self.config.get('system', {}).get('mode', 'paper')
        capital = self.config.get('capital', {}).get('total', 0)
        logger.info("=" * 60)
        logger.info(f"  猎场系统 V6.0 'Genesis' 启动")
        logger.info(f"  模式: {mode}  |  资金: {capital:,.0f} 元")
        logger.info("=" * 60)

        self._setup_signal_handlers()

        await self.event_bus.publish(Event(EventType.SYSTEM_STARTUP, data={"config": self.config}))

        asyncio.create_task(self.scheduler.start())

        self.state = "running"
        self._startup_time = asyncio.get_event_loop().time()
        logger.info("Engine started")

    async def stop(self):
        if self.state != "running":
            return
        self.state = "stopping"
        await self.event_bus.publish(Event(EventType.SYSTEM_SHUTDOWN, data={"reason": "user_requested"}))
        await self.scheduler.stop()
        self.state = "stopped"
        logger.info("Engine stopped")

    async def pause(self):
        if self.state == "running":
            self.state = "paused"
            logger.warning("Engine PAUSED — trading suspended")

    async def resume(self):
        if self.state == "paused":
            self.state = "running"
            logger.info("Engine resumed")

    def _setup_signal_handlers(self):
        try:
            loop = asyncio.get_event_loop()
            loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(self.stop()))
            loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.stop()))
        except NotImplementedError:
            pass

    def get_status(self) -> dict:
        uptime = 0
        if self._startup_time:
            uptime = asyncio.get_event_loop().time() - self._startup_time
        return {
            "state": self.state,
            "uptime_seconds": round(uptime, 1),
            "event_bus": self.event_bus.get_stats(),
            "scheduler": self.scheduler.get_stats(),
            "subsystems": list(self._subsystems.keys()),
            "trading_session": self.scheduler.is_trading_session(),
        }

    async def run_forever(self):
        await self.start()
        try:
            while self.state == "running":
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            await self.stop()
        except Exception as e:
            self.state = "error"
            logger.critical(f"Fatal error: {e}", exc_info=True)
            await self.stop()
            raise
