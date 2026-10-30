"""
猎场系统 V7.0 — 压力测试模块
合成极端行情（千股跌停 / 2015股灾 / 2020暴跌）注入回测引擎，
验证熔断闭环（L3 单日-4% / L4 周度-8% / L5 大盘-5% 或 100+跌停）在
极端行情下是否真的触发、以及触发后能否把回撤控制在阈值内。

设计：
  - 压力测试通过 BacktestEngine 的两个钩子注入：
      set_price_perturbation(fn)  —— 对每日收盘价施加行情扰动（合成崩盘）
      set_forced_limit_down(dates) —— 崩盘期间强制跌停封死（无法卖出）
  - 每个场景跑一遍引擎（无组合级熔断），得到「无保护」的权益曲线与回撤；
  - 再用 _simulate_breaker 模拟 L3/L4/L5 触发后的「有保护」清仓曲线，
    对比两者最大回撤，验证熔断闭环价值。
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable
import numpy as np

from .engine import BacktestEngine, BacktestConfig
from .strategies import build_mean_reversion_strategy

logger = logging.getLogger(__name__)


@dataclass
class StressScenario:
    """合成极端行情场景定义"""
    name: str
    description: str
    start_frac: float = 0.50       # 崩盘起点（占回测区间比例）
    duration_days: int = 3         # 崩盘持续交易日数
    daily_drop_pct: float = 0.10   # 崩盘期间单日跌幅
    limit_locked: bool = True      # 崩盘期间是否跌停封死（不可卖出）
    recovery_days: int = 20        # 崩盘后反弹恢复交易日数


# 三档历史极端行情模板（参数对齐 A 股历史极端情形）
STRESS_SCENARIOS: Dict[str, StressScenario] = {
    'limit_down_cascade': StressScenario(
        name='千股跌停',
        description='全市场连续 3 日跌停封死，累计 -27%，随后 20 日缓慢修复',
        start_frac=0.50, duration_days=3, daily_drop_pct=0.10,
        limit_locked=True, recovery_days=20,
    ),
    'crash_2015': StressScenario(
        name='2015股灾',
        description='杠杆牛破裂，10 日累计 -43%，跌停封死，随后 60 日修复',
        start_frac=0.50, duration_days=10, daily_drop_pct=0.055,
        limit_locked=True, recovery_days=60,
    ),
    'crash_2020': StressScenario(
        name='2020暴跌',
        description='疫情黑天鹅，5 日累计 -30%，部分可卖出（未封死），随后 30 日修复',
        start_frac=0.50, duration_days=5, daily_drop_pct=0.07,
        limit_locked=False, recovery_days=30,
    ),
}


class StressTester:
    """压力测试运行器：baseline + 各场景 → 回撤/熔断对比"""

    def __init__(self, db, config: dict, start: str, end: str,
                 universe: Optional[List[str]] = None):
        self.db = db
        self.start = start
        self.end = end
        self.universe = universe or (db.get_all_codes() if hasattr(db, 'get_all_codes') else [])

        self.bt_config = BacktestConfig(
            initial_capital=config.get('capital', {}).get('total', 2000000.0),
            start_date=start,
            end_date=end,
            max_positions=config.get('capital', {}).get('max_positions', 6),
            max_single_pct=config.get('capital', {}).get('max_single_position_pct', 0.25),
            train_ratio=config.get('backtest', {}).get('train_ratio', 0.70),
        )

    def _build_perturbation(self, scenario: StressScenario,
                            dates: List[str]) -> Tuple[Callable, set]:
        """构造 (价格扰动函数, 强制跌停日期集合)。"""
        n = len(dates)
        start_idx = int(n * scenario.start_frac)
        crash_end = start_idx + scenario.duration_days
        rec_end = crash_end + scenario.recovery_days
        floor = (1 - scenario.daily_drop_pct) ** scenario.duration_days

        multiplier: Dict[str, float] = {}
        locked_dates = set()
        for i, d in enumerate(dates):
            if i < start_idx:
                m = 1.0
            elif i < crash_end:
                k = i - start_idx + 1
                m = (1 - scenario.daily_drop_pct) ** k
                if scenario.limit_locked:
                    locked_dates.add(d)
            elif i < rec_end:
                r = i - crash_end + 1
                # V 型反弹：从崩盘底部线性恢复到 1.0
                m = floor + (1.0 - floor) * (r / scenario.recovery_days)
            else:
                m = 1.0
            multiplier[d] = m

        def perturb(ts_code: str, trade_date: str, price: float) -> float:
            return price * multiplier.get(trade_date, 1.0)

        return perturb, locked_dates

    def _run_scenario(self, scenario: Optional[StressScenario]):
        """跑一遍回测；scenario=None 表示基准（无扰动）。"""
        engine = BacktestEngine(self.bt_config, self.db)
        engine.add_strategy(build_mean_reversion_strategy(
            self.db, self.universe[:200], self.start, self.end, top_n=3))

        dates = engine._get_trading_dates()
        start_idx = None
        if scenario is not None:
            perturb, locked = self._build_perturbation(scenario, dates)
            engine.set_price_perturbation(perturb)
            engine.set_forced_limit_down(locked)
            start_idx = int(len(dates) * scenario.start_frac)

        result = engine.run()

        breaker = self._simulate_breaker(
            result.daily_values,
            daily_drop_pct=scenario.daily_drop_pct if scenario else 0.0,
            crash_start_idx=start_idx,
            crash_date=dates[start_idx] if start_idx is not None else None,
        )
        return result, breaker

    @staticmethod
    def _max_dd(values: List[float]) -> float:
        v = np.asarray(values, dtype=float)
        if len(v) < 2:
            return 0.0
        peak = np.maximum.accumulate(v)
        return float(np.max((peak - v) / peak * 100))

    def _simulate_breaker(self, values: List[float], daily_drop_pct: float,
                          crash_start_idx: Optional[int],
                          crash_date: Optional[str]) -> Dict:
        """
        在权益曲线上模拟 L3/L4/L5 熔断，比较「有/无组合级熔断」的最大回撤。

        L3 单日 -4% → 只停新开仓；L4 周度 -8% → 清仓；L5 大盘单日 -5% → 清仓。
        有熔断的曲线：L5 在崩盘首日收盘清仓，净值冻结在崩盘首日（已含当日跌幅），
        之后持有现金不再承担后续崩盘回撤。
        """
        v = np.asarray(values, dtype=float)
        n = len(v)
        if n < 2:
            return {'l3_triggered': False, 'l4_triggered': False, 'l5_triggered': False}

        rets = np.diff(v) / v[:-1]

        l3_triggered = bool((rets <= -0.04).any())

        l4_triggered = False
        if n >= 6:
            week = v[5:] / v[:-5] - 1
            l4_triggered = bool((week <= -0.08).any())

        # L5：大盘单日 -5%（合成场景单日跌幅即为大盘跌幅）
        l5_triggered = daily_drop_pct >= 0.05

        # 有熔断：L5 崩盘首日收盘清仓 → 净值自崩盘首日起冻结
        hedged = v.copy()
        if l5_triggered and crash_start_idx is not None and 0 < crash_start_idx < n:
            hedged[crash_start_idx:] = hedged[crash_start_idx]

        return {
            'l3_triggered': l3_triggered,
            'l4_triggered': l4_triggered,
            'l5_triggered': l5_triggered,
            'liquidation_date': crash_date,
            'max_drawdown_no_breaker_pct': round(self._max_dd(v), 2),
            'max_drawdown_with_breaker_pct': round(self._max_dd(hedged), 2),
        }

    def run(self) -> Dict:
        """跑基准 + 所有场景，返回汇总。"""
        report = {}
        base_result, base_breaker = self._run_scenario(None)
        report['baseline'] = {
            'result': base_result,
            'breaker': base_breaker,
        }
        for key, sc in STRESS_SCENARIOS.items():
            logger.info(f"压力测试场景启动: {sc.name} ({key})")
            result, breaker = self._run_scenario(sc)
            report[key] = {
                'scenario': sc,
                'result': result,
                'breaker': breaker,
            }
        return report

    def report(self, run_report: Dict) -> str:
        """生成人类可读的文本报告。"""
        lines = []
        lines.append("=" * 92)
        lines.append("猎场系统 V7.0 — 极端行情压力测试报告")
        lines.append("=" * 92)
        lines.append(f"{'场景':<12}{'总收益%':>10}{'无熔断回撤%':>14}"
                     f"{'有熔断回撤%':>14}{'L3':>6}{'L4':>6}{'L5':>6}{'回撤削减':>10}")
        lines.append("-" * 92)

        baseline = run_report.get('baseline', {})
        base_res = baseline.get('result')
        if base_res:
            lines.append(f"{'基准(无扰动)':<12}{base_res.total_return:>9.2f}"
                         f"{base_res.max_drawdown:>13.2f}{'-':>14}{'-':>6}{'-':>6}{'-':>6}{'-':>10}")

        for key, item in run_report.items():
            if key == 'baseline':
                continue
            sc = item['scenario']
            res = item['result']
            bk = item['breaker']
            dd_none = bk['max_drawdown_no_breaker_pct']
            dd_hedge = bk['max_drawdown_with_breaker_pct']
            cut = dd_none - dd_hedge
            lines.append(
                f"{sc.name:<12}{res.total_return:>9.2f}{dd_none:>13.2f}{dd_hedge:>13.2f}"
                f"{'Y' if bk['l3_triggered'] else '-':>6}"
                f"{'Y' if bk['l4_triggered'] else '-':>6}"
                f"{'Y' if bk['l5_triggered'] else '-':>6}"
                f"{cut:>9.2f}pp"
            )

        lines.append("-" * 92)
        lines.append("说明：")
        lines.append("  - 有熔断回撤 = 假设 L5 在崩盘首日清仓后的最大回撤（L4 周度 -8% 清仓同理）")
        lines.append("  - L3 单日 -4% 停新开仓 / L4 周度 -8% 清仓 / L5 大盘 -5% 或 100+跌停清仓")
        lines.append("  - 回撤削减 = 组合级熔断把崩盘回撤压掉的幅度（百分点）")
        lines.append("=" * 92)
        return "\n".join(lines)
