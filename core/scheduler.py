"""
猎场系统 V6.0 — 内置异步调度器
替代 V5.2 的 crontab，支持事件驱动 + 定时任务
"""

import asyncio
import logging
from typing import Callable, Coroutine, Dict, Optional
from datetime import datetime, time, timedelta
from dataclasses import dataclass
from enum import IntEnum

logger = logging.getLogger(__name__)


class TaskPriority(IntEnum):
    LOW = 10
    NORMAL = 5
    HIGH = 1
    CRITICAL = 0


@dataclass
class ScheduledTask:
    name: str
    callback: Callable[[], Coroutine]
    interval_seconds: float
    priority: TaskPriority = TaskPriority.NORMAL
    enabled: bool = True
    run_on_start: bool = False
    trading_hours_only: bool = False
    _last_run: Optional[datetime] = None
    _next_run: Optional[datetime] = None
    _run_count: int = 0
    _error_count: int = 0

    def should_run(self, now: datetime, is_trading: bool) -> bool:
        if not self.enabled:
            return False
        if self.trading_hours_only and not is_trading:
            return False
        if self._next_run is None:
            return True
        return now >= self._next_run

    def record_run(self):
        self._last_run = datetime.now()
        self._next_run = self._last_run + timedelta(seconds=self.interval_seconds)
        self._run_count += 1

    def record_error(self):
        self._error_count += 1


class Scheduler:
    """异步任务调度器"""

    def __init__(self, event_bus=None, trading_calendar=None):
        self._tasks: Dict[str, ScheduledTask] = {}
        self._running = False
        self._tick_interval = 1.0
        self._event_bus = event_bus
        self._trading_calendar = trading_calendar  # 交易日历（含节假日）

        self.trading_sessions = [
            (time(9, 30), time(11, 30)),
            (time(13, 0), time(15, 0)),
        ]

    def set_trading_calendar(self, trading_calendar):
        """注入交易日历对象（需提供 is_trading_day(date) 方法）"""
        self._trading_calendar = trading_calendar

    def add_task(
        self,
        name: str,
        callback: Callable[[], Coroutine],
        interval_seconds: float,
        priority: TaskPriority = TaskPriority.NORMAL,
        run_on_start: bool = False,
        enabled: bool = True,
        trading_hours_only: bool = False,
    ) -> ScheduledTask:
        task = ScheduledTask(
            name=name,
            callback=callback,
            interval_seconds=interval_seconds,
            priority=priority,
            run_on_start=run_on_start,
            enabled=enabled,
            trading_hours_only=trading_hours_only,
        )
        self._tasks[name] = task
        log_msg = f"Task '{name}': interval={interval_seconds}s"
        if trading_hours_only:
            log_msg += " [trading hours only]"
        logger.info(log_msg)
        return task

    def remove_task(self, name: str):
        if name in self._tasks:
            del self._tasks[name]

    def enable_task(self, name: str):
        if name in self._tasks:
            self._tasks[name].enabled = True

    def disable_task(self, name: str):
        if name in self._tasks:
            self._tasks[name].enabled = False

    def set_task_interval(self, name: str, interval_seconds: float) -> bool:
        """运行时动态修改任务间隔（下次调度立即生效）"""
        task = self._tasks.get(name)
        if not task:
            logger.warning(f"Task '{name}' not found, cannot change interval")
            return False
        task.interval_seconds = float(interval_seconds)
        task._next_run = datetime.now() + timedelta(seconds=task.interval_seconds)
        logger.info(f"Task '{name}' interval changed to {task.interval_seconds}s")
        return True

    def get_task_interval(self, name: str) -> Optional[float]:
        task = self._tasks.get(name)
        return task.interval_seconds if task else None

    def is_trading_session(self, now: Optional[datetime] = None) -> bool:
        if now is None:
            now = datetime.now()
        # A股周末 + 法定节假日休市
        if not self.is_trading_day(now):
            return False
        ct = now.time()
        for start, end in self.trading_sessions:
            if start <= ct <= end:
                return True
        return False

    def is_weekday(self, now: Optional[datetime] = None) -> bool:
        if now is None:
            now = datetime.now()
        return now.weekday() < 5

    def is_trading_day(self, now: Optional[datetime] = None) -> bool:
        """周末 + 法定节假日休市。未注入日历则退化为仅周末判断。"""
        if now is None:
            now = datetime.now()
        if now.weekday() >= 5:
            return False
        if self._trading_calendar is not None:
            return self._trading_calendar.is_trading_day(now.date())
        return True

    async def start(self):
        self._running = True
        logger.info("Scheduler started")

        for task in sorted(self._tasks.values(), key=lambda t: t.priority.value):
            if task.run_on_start and task.enabled:
                try:
                    await task.callback()
                    task.record_run()
                except Exception as e:
                    logger.error(f"Task '{task.name}' init error: {e}")
                    task.record_error()

        while self._running:
            await self._tick()
            await asyncio.sleep(self._tick_interval)

    async def stop(self):
        self._running = False
        logger.info("Scheduler stopped")

    async def _tick(self):
        now = datetime.now()
        is_trading = self.is_trading_session(now)
        sorted_tasks = sorted(
            self._tasks.values(),
            key=lambda t: (t.priority.value, t._next_run or datetime.min)
        )
        for task in sorted_tasks:
            if task.should_run(now, is_trading):
                try:
                    await task.callback()
                    task.record_run()
                except Exception as e:
                    logger.error(f"Task '{task.name}' error: {e}", exc_info=True)
                    task.record_error()

    def get_stats(self) -> Dict:
        now = datetime.now()
        cal = self._trading_calendar
        source = getattr(cal, 'source', 'none') if cal else 'none'
        if source == 'api':
            cal_days = len(cal.trading_days) if hasattr(cal, 'trading_days') else 0
        else:
            cal_days = len(cal.holidays) if (cal and hasattr(cal, 'holidays')) else 0
        return {
            "total_tasks": len(self._tasks),
            "enabled_tasks": sum(1 for t in self._tasks.values() if t.enabled),
            "running": self._running,
            "trading_day": self.is_trading_day(now),
            "calendar_source": source,
            "calendar_days": cal_days,
            "tasks": {
                name: {
                    "enabled": t.enabled,
                    "interval_seconds": t.interval_seconds,
                    "run_count": t._run_count,
                    "error_count": t._error_count,
                    "last_run": t._last_run.isoformat() if t._last_run else None,
                }
                for name, t in self._tasks.items()
            }
        }
