"""
猎场系统 V7.0 — 金融数据预处理
对应能力维度一「高质量金融数据支撑体系」：
  - MAD 去极值（抗离群点，优于简单 3σ）
  - Z-score 标准化
  - 次新股过滤（上市不足 N 天剔除，避免无足够历史数据/上市爆炒噪声）
  - 未来数据防护（仅在 t 时刻用 <=t 的数据，杜绝 look-ahead）

所有函数为纯函数、无副作用，可被 stock_pool / evolution / backtest 复用。
"""

import numpy as np
from datetime import datetime
from typing import List, Dict, Optional


def winsorize_mad(values, n: float = 5.0):
    """
    MAD 去极值：将 |x - median| > n * 1.4826 * MAD 的值压缩到边界。

    1.4826 为 MAD → 标准差的无偏转换系数（正态分布假设）。
    n=5 默认比 3σ 更保守，可避免极端值主导因子打分。

    Returns:
        np.ndarray，形状与输入一致
    """
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return arr
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    if mad == 0:
        # 常数序列，无离群点
        return arr.copy()
    bound = n * 1.4826 * mad
    return np.clip(arr, med - bound, med + bound)


def zscore(values) -> np.ndarray:
    """Z-score 标准化：(x - mean) / std。std=0 时返回零向量。"""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return arr
    std = float(arr.std())
    if std == 0:
        return np.zeros_like(arr)
    return (arr - arr.mean()) / std


def standardize(values, n: float = 5.0) -> np.ndarray:
    """组合预处理：先 MAD 去极值，再 Z-score 标准化（因子计算标准流水线）。"""
    return zscore(winsorize_mad(values, n))


def is_new_stock(list_date, as_of: str, days: int = 60) -> bool:
    """
    次新股判定：上市距 as_of 不足 days 天。

    list_date 为空（数据缺失）时按「非次新」处理，避免误杀大量缺失字段的股票。
    """
    if not list_date:
        return False
    try:
        d = datetime.strptime(str(list_date)[:10], "%Y-%m-%d")
        a = datetime.strptime(str(as_of)[:10], "%Y-%m-%d")
        return (a - d).days < days
    except (ValueError, TypeError):
        return False


def filter_new_stocks(stocks: List[Dict], as_of: str, days: int = 60) -> List[Dict]:
    """剔除次新股（上市不足 days 天）。stocks 每条需含 list_date 字段。"""
    return [s for s in stocks if not is_new_stock(s.get('list_date'), as_of, days)]


def has_future_data(data: List[Dict], feature_time_field: str = 'trade_date') -> bool:
    """
    未来数据防护：检测时序数据是否已按时间升序排列。
    返回 True 表示发现时间乱序（可能混入未来数据），调用方应拒绝或排序。

    更严格的 look-ahead 校验需在因子计算处保证：t 时刻因子只使用 index<=t 的样本，
    由本函数作为兜底护栏。
    """
    if not data:
        return False
    prev = None
    for row in data:
        t = row.get(feature_time_field)
        if t is None:
            continue
        if prev is not None and str(t) < str(prev):
            return True
        prev = str(t)
    return False
