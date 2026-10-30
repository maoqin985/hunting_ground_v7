"""
猎场系统 V7.0 — AI因子挖掘器
基于量价/情绪/基本面表达式，自动发现有效因子
支持遗传算法搜索因子表达式空间
"""

import logging
import random
import itertools
import numpy as np
from typing import Dict, List, Optional, Callable
from datetime import datetime

from .factor_evaluator import FactorEvaluator
from .factor_registry import FactorRegistry

logger = logging.getLogger(__name__)


class FactorMiner:
    """
    自动因子挖掘器

    流程:
    1. 表达式生成 — 基于运算符模板+字段随机组合
    2. IC评估 — Rank IC + ICIR
    3. 分层回测 — Top/Bottom 分组收益
    4. 去重 — 与已有因子相关性检查
    5. 入库 — 有效因子写入 factor_registry
    """

    # 运算符
    OPERATORS_UNARY = ['ABS', 'LOG', 'SIGN', 'RANK', 'DELTA', 'PCT_CHG']
    OPERATORS_BINARY = ['+', '-', '*', '/', 'MIN', 'MAX', 'TS_CORR']
    OPERATORS_ROLLING = ['MA', 'STD', 'SUM', 'MAX', 'MIN', 'CORR', 'SLOPE']

    # 字段
    FIELDS_PRICE = ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VWAP']
    FIELDS_VOLUME = ['VOL', 'AMOUNT', 'TURN']
    FIELDS_DERIVED = ['RETURN', 'HIGH_LOW_RATIO', 'GAP', 'LOG_RETURN']

    # 默认窗口
    ROLLING_WINDOWS = [5, 10, 20, 60]

    def __init__(self, db=None, registry: FactorRegistry = None,
                 evaluator: FactorEvaluator = None):
        self.db = db
        self.registry = registry or FactorRegistry(db)
        self.evaluator = evaluator or FactorEvaluator(db)
        self._discovered_count = 0

    async def mine_from_kline(self, kline_data: Dict[str, List[Dict]],
                               forward_days: int = 5,
                               max_candidates: int = 100) -> List[Dict]:
        """
        从K线数据挖掘因子

        Args:
            kline_data: {ts_code: [{open, high, low, close, vol, amount}, ...]}
            forward_days: 未来N日收益窗口
            max_candidates: 最大候选因子数

        Returns:
            有效因子列表
        """
        if len(kline_data) < 30:
            logger.warning("Not enough stocks for factor mining (min 30)")
            return []

        # 1. 生成候选因子表达式
        candidates = self._generate_candidates(max_candidates)
        logger.info(f"Generated {len(candidates)} factor candidates")

        # 2. 计算每个因子的值和未来收益
        discovered = []
        existing_vals = {}  # 已有因子的计算值，用于去重

        for candidate in candidates[:max_candidates]:
            name = candidate['name']
            expression = candidate['expr']
            category = candidate['category']

            # 计算因子面板（含截面 Rank IC 序列，修复方向颠倒 + 空 IC 实现）
            factor_values, forward_returns, ic_series = self._compute_panel(
                kline_data, expression, forward_days
            )

            if len(factor_values) < 30:
                continue

            # 评估
            result = self.evaluator.evaluate_factor(
                name, factor_values, forward_returns,
                ic_series=ic_series,
                category=category,
                expression=expression,
            )

            if result is None:
                continue

            if result['is_valid']:
                # 去重
                redundant = self.evaluator.find_redundant(
                    factor_values, existing_vals
                )
                if redundant:
                    logger.debug(f"Factor '{name}' redundant with {redundant}, skipping")
                    continue

                # 注册
                factor_record = {
                    'factor_name': name,
                    'factor_type': 'generated',
                    'category': category,
                    'expression': expression,
                    'ic_value': result['ic_value'],
                    'ir_value': result['icir'],
                    'sharpe': result['sharpe'],
                    'effectiveness': result['effectiveness'],
                    'weight': result['recommended_weight'],
                    'status': 'active',
                    'discovered_at': datetime.now().strftime('%Y-%m-%d'),
                    'last_evaluated': datetime.now().strftime('%Y-%m-%d'),
                    'metadata_json': {
                        'stratified': result['stratified'],
                        'ic_mean': result['ic_value'],
                    },
                }
                fid = self.registry.register(factor_record)
                if fid:
                    discovered.append(result)
                    existing_vals[name] = factor_values
                    self._discovered_count += 1
                    logger.info(f"Discovered factor: {name} IC={result['ic_value']:.4f} "
                                f"IR={result['icir']:.2f} eff={result['effectiveness']:.2f}")

        logger.info(f"Factor mining complete: {len(discovered)} new factors found")
        return discovered

    def _generate_candidates(self, max_n: int = 100) -> List[Dict]:
        """生成候选因子表达式"""
        candidates = []

        # 已有因子名，避免重复
        existing_names = set()
        if self.registry:
            existing_names = {f['factor_name'] for f in self.registry.get_active()}

        # 1. 滚动窗口因子: WINDOW_OP(FIELD)
        gen = 0
        for field in self.FIELDS_PRICE + self.FIELDS_VOLUME + self.FIELDS_DERIVED:
            for op in self.OPERATORS_ROLLING:
                for w in self.ROLLING_WINDOWS:
                    name = f"{op.lower()}_{field.lower()}_{w}"
                    if name in existing_names:
                        continue
                    candidates.append({
                        'name': name,
                        'category': 'volume_price',
                        'expr': f"{op}({field},{w})",
                    })
                    gen += 1
                    if gen >= max_n:
                        break
                if gen >= max_n:
                    break
            if gen >= max_n:
                break

        # 2. 二元操作因子: MA(FIELD_a,W) OP MA(FIELD_b,W)
        if gen < max_n:
            for (fa, fb) in itertools.combinations(
                self.FIELDS_PRICE + self.FIELDS_VOLUME, 2
            ):
                for op in self.OPERATORS_BINARY[:4]:  # + - * /
                    for w in self.ROLLING_WINDOWS[:2]:  # 5, 10
                        op_name = {'+': 'add', '-': 'sub', '*': 'mul', '/': 'div'}[op]
                        name = f"{op_name}_{fa.lower()}{fb.lower()}_{w}"
                        if name in existing_names:
                            continue
                        candidates.append({
                            'name': name,
                            'category': 'volume_price',
                            'expr': f"MA({fa},{w}){op}MA({fb},{w})",
                        })
                        gen += 1
                        if gen >= max_n:
                            break
                if gen >= max_n:
                    break

        # 3. 变化率因子: DELTA(FIELD,W)/REF(FIELD,W)
        if gen < max_n:
            for field in self.FIELDS_PRICE + self.FIELDS_VOLUME:
                for w in self.ROLLING_WINDOWS:
                    name = f"pct_{field.lower()}_{w}"
                    if name in existing_names:
                        continue
                    candidates.append({
                        'name': name,
                        'category': 'volume_price',
                        'expr': f"({field}-REF({field},{w}))/REF({field},{w})",
                    })
                    gen += 1
                    if gen >= max_n:
                        break
                if gen >= max_n:
                    break

        # 4. 相关性因子
        if gen < max_n:
            for (fa, fb) in itertools.combinations(self.FIELDS_PRICE, 2):
                for w in self.ROLLING_WINDOWS[1:3]:  # 10, 20
                    name = f"corr_{fa.lower()}{fb.lower()}_{w}"
                    if name in existing_names:
                        continue
                    candidates.append({
                        'name': name,
                        'category': 'volume_price',
                        'expr': f"CORR({fa},{fb},{w})",
                    })
                    gen += 1
                    if gen >= max_n:
                        break
                if gen >= max_n:
                    break

        random.shuffle(candidates)
        return candidates

    def _eval_expression_series(self, expr: str, close: np.ndarray,
                                 open_: np.ndarray, high: np.ndarray,
                                 low: np.ndarray, vol: np.ndarray) -> Optional[np.ndarray]:
        """计算因子表达式的完整时间序列（与 close 等长，预热期用 NaN）。

        关键修正：滚动算子改为同长度输出（左侧 NaN 预热），修复原 MA/SUM 卷积
        导致的序列错位，使因子值能与价格序列按时间对齐。
        """
        try:
            n = len(close)

            def _rolling(x, w, func):
                out = np.full(n, np.nan)
                for i in range(w - 1, n):
                    out[i] = func(x[i - w + 1:i + 1])
                return out

            def _shift(x, k):
                # 滞后 k 期（右移），前 k 期为 NaN（修复 np.roll 的环绕错误）
                if k <= 0:
                    return np.asarray(x, dtype=float)
                out = np.full(n, np.nan)
                out[k:] = x[:-k]
                return out

            def _rolling_pair(x, y, w):
                out = np.full(n, np.nan)
                for i in range(w - 1, n):
                    xs = x[i - w + 1:i + 1]
                    ys = y[i - w + 1:i + 1]
                    if np.std(xs) < 1e-12 or np.std(ys) < 1e-12:
                        out[i] = 0.0
                    else:
                        out[i] = np.corrcoef(xs, ys)[0, 1]
                return out

            scope = {
                'CLOSE': close, 'OPEN': open_, 'HIGH': high, 'LOW': low, 'VOL': vol,
                'VWAP': close,
                'AMOUNT': vol * close,
                'TURN': vol / 1e8,
                'RETURN': np.concatenate(([0.0], np.diff(close) / (close[:-1] + 1e-8))),
                'HIGH_LOW_RATIO': (high - low) / (close + 1e-8),
                'GAP': np.concatenate(([0.0], (open_[1:] - close[:-1]) / (close[:-1] + 1e-8))),
                'LOG_RETURN': np.concatenate(([0.0], np.log(close[1:] / (close[:-1] + 1e-8)))),
                'MA': lambda x, w: _rolling(x, w, np.mean),
                'STD': lambda x, w: _rolling(x, w, lambda a: np.std(a, ddof=0)),
                'SUM': lambda x, w: _rolling(x, w, np.sum),
                'MAX': lambda x, w: _rolling(x, w, np.max),
                'MIN': lambda x, w: _rolling(x, w, np.min),
                'SLOPE': lambda x, w: _rolling(x, w, lambda a: np.polyfit(np.arange(len(a)), a, 1)[0]),
                'CORR': lambda x, y, w: _rolling_pair(x, y, w),
                'REF': lambda x, k: _shift(x, k),
                'DELTA': lambda x, k: x - _shift(x, k),
                'PCT_CHG': lambda x, k: x / (_shift(x, k) + 1e-8) - 1,
                'ABS': np.abs, 'LOG': lambda x: np.log(np.maximum(x, 1e-10)),
                'SIGN': np.sign, 'RANK': lambda x: np.argsort(np.argsort(x)).astype(float) / max(len(x), 1),
            }

            result = eval(expr, {"__builtins__": {}}, scope)
            result = np.asarray(result, dtype=float)
            if result.ndim == 0:
                return np.full(n, float(result))
            if len(result) != n:
                # 长度不一致时按右对齐（保留最近端），左侧补 NaN
                res = np.full(n, np.nan)
                m = min(len(result), n)
                res[-m:] = result[-m:]
                return res
            return result
        except Exception:
            return None

    def _compute_panel(self, kline_data: Dict[str, List[Dict]],
                       expression: str, forward_days: int) -> tuple:
        """计算因子面板，返回 (factor_values, forward_returns, ic_series)。

        - factor_values / forward_returns: 跨股票 × 跨时点的 (因子值, 未来N日收益) 对
        - ic_series: 每个截面时点的 Rank IC 序列（供 ICIR 计算）

        修复原缺陷：
        1. 前向收益方向颠倒（原用过去收益 closes[-1]/closes[-1-N]-1）
        2. 滚动 IC 空实现（现逐截面计算 Spearman Rank IC）
        """
        # 1. 预计算每只股票的因子序列（与价格等长，NaN 表示预热/无效）
        series_map: Dict[str, tuple] = {}
        for ts_code, rows in kline_data.items():
            if len(rows) < 60 + forward_days:
                continue
            closes = np.array([r.get('close', 0) for r in rows], dtype=float)
            if closes[-1] <= 0:
                continue
            opens = np.array([r.get('open', 0) for r in rows], dtype=float)
            highs = np.array([r.get('high', 0) for r in rows], dtype=float)
            lows = np.array([r.get('low', 0) for r in rows], dtype=float)
            vols = np.array([r.get('vol', 0) for r in rows], dtype=float)
            vals = self._eval_expression_series(expression, closes, opens, highs, lows, vols)
            if vals is None:
                continue
            series_map[ts_code] = (closes, vals)

        if len(series_map) < 30:
            return np.array([]), np.array([]), np.array([])

        # 2. 确定共同评估时点范围（保证所有股票都有前向收益）
        min_len = min(len(c) for c, _ in series_map.values())
        eval_start = 60
        eval_end = min_len - 1 - forward_days
        if eval_end <= eval_start:
            return np.array([]), np.array([]), np.array([])

        all_factor_vals = []
        all_fwd_rets = []
        ic_series = []

        for t in range(eval_start, eval_end):
            fv_t = []
            fr_t = []
            for closes, vals in series_map.values():
                v = vals[t]
                if not np.isfinite(v):
                    continue
                fwd = closes[t + forward_days] / closes[t] - 1
                if not np.isfinite(fwd):
                    continue
                fv_t.append(v)
                fr_t.append(fwd)
            if len(fv_t) >= 30:
                ic_t = self.evaluator.compute_rank_ic(np.array(fv_t), np.array(fr_t))
                ic_series.append(ic_t)
                all_factor_vals.extend(fv_t)
                all_fwd_rets.extend(fr_t)

        return np.array(all_factor_vals), np.array(all_fwd_rets), np.array(ic_series)

    @property
    def discovered_count(self) -> int:
        return self._discovered_count
