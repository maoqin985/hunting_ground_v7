"""
猎场系统 V7.0 — 因子注册中心
管理因子库：注册、去重、衰减检测、权重调整
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class FactorRegistry:
    """因子库管理器"""

    # 基础因子模板（量价类）
    BASE_TEMPLATES_VOLUME_PRICE = [
        # RSI 变体
        {"name": "rsi_14", "category": "volume_price", "expression": "RSI(14)","description": "14日RSI"},
        {"name": "rsi_6", "category": "volume_price", "expression": "RSI(6)","description": "6日快速RSI"},
        {"name": "rsi_divergence", "category": "volume_price",
         "expression": "RSI(14) - RSI(6)", "description": "短期vs长期RSI差异（动量确认）"},
        # MACD 变体
        {"name": "macd_dif", "category": "volume_price", "expression": "MACD_DIF(12,26,9)","description": "MACD快慢线差值"},
        {"name": "macd_hist", "category": "volume_price",
         "expression": "MACD_HIST(12,26,9)", "description": "MACD柱（加速度）"},
        # 波动率
        {"name": "volatility_10", "category": "volume_price",
         "expression": "STD(RETURN,10)", "description": "10日收益率波动率"},
        {"name": "volatility_ratio", "category": "volume_price",
         "expression": "STD(RETURN,5)/STD(RETURN,20)", "description": "短期/长期波动率比值"},
        # 成交量
        {"name": "volume_ratio_5", "category": "volume_price",
         "expression": "MA(VOL,5)/MA(VOL,20)", "description": "5日/20日均量比"},
        {"name": "volume_breakout", "category": "volume_price",
         "expression": "VOL/MA(VOL,20)", "description": "当日量/20日均量（放量突破）"},
        {"name": "turnover_rate", "category": "volume_price",
         "expression": "VOL/SHARES*100", "description": "换手率"},
        # 动量
        {"name": "momentum_5", "category": "volume_price",
         "expression": "CLOSE/REF(CLOSE,5)-1", "description": "5日动量"},
        {"name": "momentum_20", "category": "volume_price",
         "expression": "CLOSE/REF(CLOSE,20)-1", "description": "20日动量"},
        {"name": "relative_strength", "category": "volume_price",
         "expression": "CLOSE/MA(CLOSE,20)", "description": "相对20日均价强度"},
        # 价格形态
        {"name": "gap_up_pct", "category": "volume_price",
         "expression": "OPEN/REF(CLOSE,1)-1", "description": "跳空高开幅度"},
        {"name": "upper_shadow_ratio", "category": "volume_price",
         "expression": "(HIGH-MAX(OPEN,CLOSE))/MAX(HIGH-LOW,0.001)", "description": "上影线占比"},
        # 均线
        {"name": "ma_cross_5_20", "category": "volume_price",
         "expression": "MA(CLOSE,5)/MA(CLOSE,20)-1", "description": "5日vs20日均线交叉"},
        {"name": "ma_dispersion", "category": "volume_price",
         "expression": "(MAX(MA5,MA10,MA20)-MIN(MA5,MA10,MA20))/MA20", "description": "均线离散度"},
    ]

    # 情绪/资金类因子
    BASE_TEMPLATES_SENTIMENT = [
        {"name": "buy_sell_ratio", "category": "sentiment",
         "expression": "BUY_VOL/SELL_VOL", "description": "买卖盘比"},
        {"name": "large_order_ratio", "category": "sentiment",
         "expression": "LARGE_ORDER_AMT/TOTAL_AMT", "description": "大单成交占比"},
        {"name": "institution_flow", "category": "sentiment",
         "expression": "INSTITUTION_NET_FLOW/MARKET_CAP", "description": "机构净流入/市值"},
        {"name": "retail_flow_5d", "category": "sentiment",
         "expression": "SUM(RETAIL_NET,5)/SUM(TOTAL_AMT,5)", "description": "5日散户净买入占比"},
        {"name": "north_flow_ratio", "category": "sentiment",
         "expression": "NORTH_BOUND_HOLD/MARKET_CAP", "description": "北向资金持仓占比"},
    ]

    # 基本面因子
    BASE_TEMPLATES_FUNDAMENTAL = [
        {"name": "pe_percentile", "category": "fundamental",
         "expression": "PERCENTRANK(PE_TTM,250)", "description": "PE_TTM 一年分位"},
        {"name": "pb_percentile", "category": "fundamental",
         "expression": "PERCENTRANK(PB,250)", "description": "PB 一年分位"},
        {"name": "roe_ttm", "category": "fundamental", "expression": "ROE_TTM","description": "ROE（TTM）"},
        {"name": "revenue_growth_yoy", "category": "fundamental",
         "expression": "REVENUE/REF(REVENUE,4)-1", "description": "营收同比增长"},
        {"name": "profit_growth_yoy", "category": "fundamental",
         "expression": "NET_PROFIT/REF(NET_PROFIT,4)-1", "description": "净利润同比增长"},
        {"name": "debt_ratio", "category": "fundamental",
         "expression": "TOTAL_LIABILITY/TOTAL_ASSET", "description": "资产负债率"},
        {"name": "market_cap_log", "category": "fundamental",
         "expression": "LOG(TOTAL_MV)", "description": "对数市值"},
    ]

    def __init__(self, db):
        self.db = db
        self._cache: Dict[str, Dict] = {}  # factor_name -> metadata
        self._loaded = False

    async def initialize(self):
        """加载因子库"""
        rows = self.db.get_active_factors()
        for r in rows:
            self._cache[r['factor_name']] = r
        self._loaded = True
        logger.info(f"FactorRegistry loaded: {len(self._cache)} active factors")

    def register(self, factor: Dict) -> Optional[int]:
        """
        注册因子（含去重检查）

        Args:
            factor: {
                factor_name, factor_type, category, expression,
                ic_value, ir_value, sharpe, effectiveness, weight, metadata_json
            }

        Returns:
            factor_id or None if duplicate
        """
        name = factor.get('factor_name', '')

        # 去重检查
        if name in self._cache:
            existing = self._cache[name]
            logger.debug(f"Factor '{name}' already registered, updating")
            # 更新绩效
            self.db.update_factor_performance(
                name, factor.get('ic_value', 0),
                factor.get('ir_value', 0),
                factor.get('effectiveness', 1.0),
            )
            return None

        # 相关性去重（与已有因子IC > 0.7 视为冗余）
        for cached_name, cached in self._cache.items():
            if self._is_redundant(factor, cached):
                logger.info(f"Factor '{name}' redundant with '{cached_name}', merged")
                return None

        factor.setdefault('status', 'active')
        factor.setdefault('weight', 1.0)
        factor.setdefault('discovered_at', datetime.now().strftime('%Y-%m-%d'))
        factor.setdefault('last_evaluated', datetime.now().strftime('%Y-%m-%d'))

        fid = self.db.save_factor(factor)
        if fid:
            self._cache[name] = factor
            logger.info(f"Factor registered: {name} (IC={factor.get('ic_value', 0):.4f})")
        return fid

    def get_active(self, category: str = None) -> List[Dict]:
        """获取活跃因子列表"""
        factors = list(self._cache.values())
        if category:
            factors = [f for f in factors if f.get('category') == category]
        # 按 effectiveness 排序
        factors.sort(key=lambda f: f.get('effectiveness', 0), reverse=True)
        return factors

    def get_best(self, top_n: int = 20) -> List[Dict]:
        """获取最优因子"""
        return self.get_active()[:top_n]

    def decay_weights(self, decay_rate: float = 0.05):
        """
        衰减所有因子权重（周度自然衰减）
        weights *= (1 - decay_rate)
        """
        for name, factor in self._cache.items():
            old_weight = factor.get('weight', 1.0)
            new_weight = old_weight * (1 - decay_rate)
            factor['weight'] = new_weight
            self.db.save_factor(factor)
        logger.info(f"Applied decay {decay_rate:.1%} to {len(self._cache)} factors")

    def mark_decaying(self, name: str):
        """标记因子为衰退状态"""
        if name in self._cache:
            self._cache[name]['status'] = 'decaying'
            self.db.save_factor(self._cache[name])
            logger.warning(f"Factor '{name}' marked as decaying")

    def retire(self, name: str):
        """淘汰因子"""
        if name in self._cache:
            self._cache[name]['status'] = 'retired'
            self.db.save_factor(self._cache[name])
            logger.warning(f"Factor '{name}' retired")

    def _is_redundant(self, new_factor: Dict, existing: Dict) -> bool:
        """检查新因子是否与已有因子冗余（简单规则：同类别+近似的IC值）"""
        if new_factor.get('category') != existing.get('category'):
            return False
        ic_diff = abs(new_factor.get('ic_value', 0) - existing.get('ic_value', 0))
        return ic_diff < 0.005  # IC差异极小视为冗余

    @property
    def count(self) -> int:
        return len(self._cache)
