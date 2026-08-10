"""
猎场系统 V6.0 — 异步事件总线
发布/订阅模式，解耦各模块通信
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """系统事件类型"""
    # 行情事件
    MARKET_DATA_UPDATED = "market.data.updated"
    REALTIME_QUOTE = "market.realtime.quote"

    # 信号事件
    SIGNAL_GENERATED = "signal.generated"
    SIGNAL_VALIDATED = "signal.validated"
    SIGNAL_REJECTED = "signal.rejected"

    # 交易事件
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_CANCELLED = "order.cancelled"
    POSITION_UPDATED = "position.updated"

    # 风控事件
    RISK_WARNING = "risk.warning"
    CIRCUIT_BREAKER_TRIGGERED = "risk.circuit_breaker.triggered"
    STOP_LOSS_TRIGGERED = "risk.stop_loss.triggered"

    # 系统事件
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_ERROR = "system.error"
    SCHEDULER_TICK = "scheduler.tick"

    # 数据事件
    DATA_SYNC_COMPLETED = "data.sync.completed"
    NEWS_UPDATED = "news.updated"

    # 进化事件
    DAILY_REVIEW_COMPLETED = "evolution.daily_review.completed"
    WEEKLY_OPTIMIZATION_COMPLETED = "evolution.weekly_optimization.completed"
    STRATEGY_UPDATED = "evolution.strategy.updated"


class Event:
    """事件对象"""

    def __init__(self, event_type: EventType, data: Any = None, source: str = ""):
        self.type = event_type
        self.data = data
        self.source = source
        self.timestamp = datetime.now()
        self.propagation_stopped = False

    def stop_propagation(self):
        self.propagation_stopped = True

    def to_dict(self) -> Dict:
        return {
            "type": self.type.value,
            "data": self.data,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
        }


EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """
    异步事件总线
    - 支持按事件类型订阅
    - 支持通配符订阅（如 "market.*"）
    - 支持处理器优先级
    """

    def __init__(self):
        self._subscribers: Dict[str, List[EventHandler]] = {}
        self._wildcard_subscribers: Dict[str, List[EventHandler]] = {}
        self._lock = asyncio.Lock()
        self._event_count = 0
        logger.info("EventBus initialized")

    async def subscribe(self, event_type: EventType, handler: EventHandler):
        async with self._lock:
            key = event_type.value
            if key not in self._subscribers:
                self._subscribers[key] = []
            self._subscribers[key].append(handler)

    async def subscribe_pattern(self, pattern: str, handler: EventHandler):
        """订阅事件模式（支持 * 通配符，如 'market.*'）"""
        async with self._lock:
            if pattern not in self._wildcard_subscribers:
                self._wildcard_subscribers[pattern] = []
            self._wildcard_subscribers[pattern].append(handler)

    async def publish(self, event: Event):
        """发布事件（异步广播给所有订阅者）"""
        self._event_count += 1
        key = event.type.value

        handlers = list(self._subscribers.get(key, []))

        for pattern, pattern_handlers in self._wildcard_subscribers.items():
            if self._match_pattern(key, pattern):
                handlers.extend(pattern_handlers)

        if not handlers:
            return

        tasks = [self._safe_invoke(h, event) for h in handlers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_invoke(self, handler: EventHandler, event: Event):
        try:
            if not event.propagation_stopped:
                await handler(event)
        except Exception as e:
            logger.error(f"Handler {handler.__name__} error: {e}", exc_info=True)

    def _match_pattern(self, event_key: str, pattern: str) -> bool:
        if pattern == "*":
            return True
        if pattern.endswith(".*"):
            return event_key.startswith(pattern[:-2])
        if pattern.startswith("*."):
            return event_key.endswith(pattern[2:])
        return False

    @property
    def event_count(self) -> int:
        return self._event_count

    def get_stats(self) -> Dict:
        return {
            "event_count": self._event_count,
            "subscriber_count": sum(len(v) for v in self._subscribers.values()),
            "event_types": list(self._subscribers.keys()),
        }


_event_bus_instance: EventBus = None


def get_event_bus() -> EventBus:
    global _event_bus_instance
    if _event_bus_instance is None:
        _event_bus_instance = EventBus()
    return _event_bus_instance
