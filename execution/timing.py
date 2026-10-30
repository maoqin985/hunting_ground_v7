"""
猎场系统 V7.0 — 择时执行优化器
避开开盘/收盘高波动期，日内成交量加权择时
"""

import logging
from typing import Dict, Optional
from datetime import datetime, time

logger = logging.getLogger(__name__)


class TimingOptimizer:
    """
    择时执行优化器

    目标: 在执行窗口内选择最优的子单发送时刻
    - 避开开盘前30分钟 (9:30-10:00) 高波动
    - 避开收盘前15分钟 (14:45-15:00) 流动性紧张
    - 避开午休 (11:30-13:00)
    - 优先在成交量高峰期执行
    """

    # A股交易时段
    MORNING_START = time(9, 30)
    MORNING_END = time(11, 30)
    AFTERNOON_START = time(13, 0)
    AFTERNOON_END = time(15, 0)

    # 高波动/低流动性时段
    AVOID_AFTER_OPEN = 30    # 开盘后避开分钟数
    AVOID_BEFORE_CLOSE = 15  # 收盘前避开分钟数

    # 成交量高峰期（相对强度）
    VOLUME_PROFILE = {
        # 时段: (开始分钟, 结束分钟, 相对强度)
        'open_peak': (0, 30, 1.5),       # 开盘高峰 (9:30-10:00)
        'morning_steady': (30, 90, 0.8), # 上午平稳 (10:00-11:00)
        'pre_lunch': (90, 120, 0.6),     # 午前低量 (11:00-11:30)
        'post_lunch': (120, 150, 0.7),   # 午后恢复 (13:00-13:30)
        'afternoon_steady': (150, 195, 0.85),  # 下午平稳 (13:30-14:15)
        'close_rush': (195, 210, 1.3),   # 收盘冲刺 (14:15-14:45)
        'close_avoid': (210, 225, 1.0),  # 收盘前避开 (14:45-15:00)
    }

    def __init__(self):
        pass

    def get_optimal_slots(self, num_slices: int,
                           session_start: time = None) -> list:
        """
        获取最优执行时间槽

        Args:
            num_slices: 子单数量
            session_start: 交易时段开始时间（默认 9:30）

        Returns:
            [(delay_minutes, quality_score), ...]
        """
        start = session_start or self.MORNING_START
        total_minutes = 240  # 4小时交易时段

        # 构建每分钟的质量得分
        minute_scores = []
        for m in range(total_minutes):
            score = self._minute_quality(m)
            minute_scores.append((m, score))

        # 避开禁止时段
        avoid_start = 0  # 开盘后0-30分钟
        avoid_end_start = total_minutes - self.AVOID_BEFORE_CLOSE  # 收盘前15分钟

        valid_slots = [
            (m, s) for m, s in minute_scores
            if m >= self.AVOID_AFTER_OPEN
            and m < avoid_end_start
            and not self._is_lunch_break(m, start)
        ]

        if len(valid_slots) < num_slices:
            # 时间段不够，降低限制
            valid_slots = [
                (m, s) for m, s in minute_scores
                if not self._is_lunch_break(m, start)
            ]

        if len(valid_slots) < 2:
            # 均匀分布
            step = total_minutes // max(num_slices, 1)
            return [(i * step, 0.5) for i in range(num_slices)]

        # 按质量排序，取前N个并按时间排序
        valid_slots.sort(key=lambda x: x[1], reverse=True)
        selected = valid_slots[:num_slices]
        selected.sort(key=lambda x: x[0])  # 按时间排序

        return selected

    def _minute_quality(self, minute_of_day: int) -> float:
        """评估某个分钟的执行质量 (0-1)"""
        for profile_name, (start_m, end_m, intensity) in self.VOLUME_PROFILE.items():
            if start_m <= minute_of_day < end_m:
                # 高峰期得分高，但开盘/收盘风险也高
                if profile_name == 'open_peak':
                    return intensity * 0.6  # 量虽大但波动大，降分
                elif profile_name == 'close_avoid':
                    return intensity * 0.4  # 收盘前风险高
                else:
                    return intensity
        return 0.5

    def _is_lunch_break(self, minute_of_day: int, session_start: time) -> bool:
        """检查是否在午休时段"""
        morning_length = 120  # 9:30-11:30 = 120min
        return morning_length <= minute_of_day < morning_length + 90  # 11:30-13:00

    def should_execute_now(self) -> bool:
        """判断当前是否适合执行（不在高波动/休市时段）"""
        now = datetime.now().time()

        # 检查是否在交易时段
        if not (self.MORNING_START <= now <= self.MORNING_END or
                self.AFTERNOON_START <= now <= self.AFTERNOON_END):
            return False

        # 计算当前分钟
        minutes = self._time_to_minutes(now)
        total_session_minutes = 240

        # 开盘后30分钟内不执行
        if minutes < self.AVOID_AFTER_OPEN:
            return False

        # 收盘前15分钟不执行
        if minutes > total_session_minutes - self.AVOID_BEFORE_CLOSE:
            return False

        return True

    def next_execution_time(self, delay_seconds: int = 60) -> Optional[float]:
        """
        返回下一个合适的执行时刻（距现在的秒数）

        Returns:
            delay_seconds or None (今日已休市)
        """
        now = datetime.now()
        current_time = now.time()

        # 午休中 → 等到下午开盘
        if self.MORNING_END < current_time < self.AFTERNOON_START:
            afternoon = datetime.combine(now.date(), self.AFTERNOON_START)
            wait = (afternoon - now).total_seconds()
            return max(delay_seconds, wait + 60)  # 至少等1分钟

        # 收盘后 → 返回None
        if current_time >= self.AFTERNOON_END:
            return None

        # 开盘前 → 等开盘
        if current_time < self.MORNING_START:
            morning = datetime.combine(now.date(), self.MORNING_START)
            return (morning - now).total_seconds() + self.AVOID_AFTER_OPEN * 60

        return delay_seconds

    @staticmethod
    def _time_to_minutes(t: time) -> int:
        """时间转换为当日分钟偏移"""
        base = time(9, 30)
        delta = (t.hour * 60 + t.minute) - (base.hour * 60 + base.minute)
        return max(0, delta)

    def get_volume_quality(self, dt: datetime = None) -> float:
        """获取当前时刻的成交量质量得分 (0-1)"""
        dt = dt or datetime.now()
        minutes = self._time_to_minutes(dt.time())
        return self._minute_quality(minutes)
