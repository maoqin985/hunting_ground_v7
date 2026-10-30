"""
猎场系统 V6.0 — 信号验证器
继承 V5.2 十层防线 + 新增 2 层 AI 防线 = 十二层买入防线
"""

import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class ValidationResult:
    """验证结果"""

    def __init__(self):
        self.passed = True
        self.blocked_by: Optional[str] = None
        self.warnings: List[str] = []
        self.details: Dict = {}

    def reject(self, layer_name: str, reason: str):
        self.passed = False
        self.blocked_by = layer_name
        self.details['reject_reason'] = reason

    def warn(self, message: str):
        self.warnings.append(message)


class SignalValidator:
    """
    十二层买入防线（继承 V5.2 十层 + 新增 AI 防线）

    设计原则: Defense in Depth
    每层独立生效，任一层拦截都能阻止买入。
    """

    def __init__(self, config: dict = None, db=None, data_gateway=None):
        self.config = config or {}
        self.db = db
        self.gateway = data_gateway

        # 加载防线配置
        defense_cfg = config.get("signal_defense", {}).get("layers", [])
        self._layers = {layer['id']: layer for layer in defense_cfg}

        # 风控参数
        risk_cfg = config.get("stop_loss", config.get("circuit_breakers", {}))

    def validate(self, ts_code: str, signal: Dict, stock_info: Dict = None,
                 market_data: Dict = None,
                 ai_risk_prob: float = 0.0) -> ValidationResult:
        """
        执行十二层防线验证

        Args:
            ts_code: 股票代码
            signal: 信号数据（含评分/原因/详情/K线特征）
            stock_info: 股票基本信息
            market_data: 市场数据
            ai_risk_prob: AI模型预测的5日大跌概率

        Returns:
            ValidationResult
        """
        result = ValidationResult()

        # === L1: 市值过滤 ===
        if stock_info:
            mc = stock_info.get('market_cap', 0) or 0
            cap_min = self._layers.get(1, {}).get('market_cap_min', 10)
            cap_max = self._layers.get(1, {}).get('market_cap_max', 50)
            if mc < cap_min or mc > cap_max:
                result.reject("L1-市值过滤", f"市值{mc:.0f}亿不在{cap_min:g}-{cap_max:g}亿区间")
                return result

        # === L2: 数据完整性 ===
        required = ['rsi', 'turnover_rate', 'volume_ratio', 'max_drawdown']
        # 融合信号: details 可能在 signal['details'] 或嵌套在 signal['signals'][0]['details']
        details = signal.get('details', {})
        if not details and signal.get('signals'):
            # 从第一个子信号提取 details (已在 v7_launcher 中补全)
            first_sig = signal['signals'][0]
            details = first_sig.get('details', {})
            signal['details'] = details  # 缓存给后续层使用
        missing = []
        for field in required:
            if field not in details:
                missing.append(field)
        if missing:
            result.reject("L2-数据完整性", f"缺失指标: {','.join(missing)}")
            return result

        # === L3: 假数据检测 ===
        latest_kline = signal.get('details', {}).get('latest_kline', {})
        if latest_kline:
            rsi = latest_kline.get('rsi_14', None)
            tr = latest_kline.get('turnover_rate', None)
            vr = latest_kline.get('volume_ratio', None)
            if rsi == 50.0 and tr == 0.0 and vr == 1.0:
                result.reject("L3-假数据检测", "RSI=50,换手率=0,量比=1.0(占位值)")
                return result

        # === L4: 涨停过滤 ===
        change_pct = signal.get('details', {}).get('change_pct', 0)
        max_change_pct = self._layers.get(4, {}).get('max_change_pct', 9.0)
        if change_pct >= max_change_pct:
            result.reject("L4-涨停过滤", f"涨幅{change_pct:.1f}%不追涨停板")
            return result

        # === L5: 评分过滤 ===
        score = signal.get('score', 0)
        min_score = self._layers.get(5, {}).get('min_score', 5)
        if score < min_score:
            result.reject("L5-评分过滤", f"评分{score:.0f}<{min_score}禁止买入")
            return result

        # === L6: 宏观情绪 ===
        if market_data:
            fg = market_data.get('fear_greed', 50)
            l6 = self._layers.get(6, {})
            fg_min = l6.get('fear_greed_min', 20)
            fg_max = l6.get('fear_greed_max', 80)
            reject_mode = l6.get('action', 'warn') == 'reject'
            if fg < fg_min:
                msg = f"L6-宏观情绪: 极度恐惧({fg})，注意风险"
                if reject_mode:
                    result.reject("L6-宏观情绪", f"极度恐惧({fg})，暂停买入")
                    return result
                result.warn(msg)
            elif fg > fg_max:
                msg = f"L6-宏观情绪: 极度贪婪({fg})，暂停买入"
                if reject_mode:
                    result.reject("L6-宏观情绪", f"极度贪婪({fg})，暂停买入")
                    return result
                result.warn(msg)

        # === L7: 新闻研判 ===
        neg_stocks = []
        if self.db:
            neg_stocks = self.db.get_negative_stocks()
        if ts_code in neg_stocks:
            result.reject("L7-新闻研判", "近期存在利空新闻")
            return result

        # === L8: 基本面黑名单 ===
        if stock_info and stock_info.get('is_st'):
            result.reject("L8-基本面黑名单", "ST/*ST股票禁止交易")
            return result

        name = stock_info.get('name', '') if stock_info else ''
        blacklist_keywords = self._layers.get(8, {}).get('blacklist_keywords', ['ST', '*ST', '退市'])
        for kw in blacklist_keywords:
            if kw in name:
                result.reject("L8-基本面黑名单", f"名称含'{kw}'")
                return result

        # === L9: 缩量止跌 ===
        turnover = signal.get('details', {}).get('turnover_rate', 0)
        if turnover > 10.0 and change_pct < -2:
            result.reject("L9-缩量止跌", f"换手率{turnover:.1f}%放量下跌")
            return result

        # === L10: 底部横盘 ===
        new_low_3d = signal.get('details', {}).get('new_low_3d', False)
        if new_low_3d:
            result.reject("L10-底部横盘", "3日内创新低，未企稳")
            return result

        # === L11: AI风险分类（新增） ===
        ai_threshold = self._layers.get(11, {}).get('catboost_risk_threshold', 0.60)
        if ai_risk_prob >= ai_threshold:
            result.reject("L11-AI风险分类", f"AI预测5日大跌概率{ai_risk_prob:.0%}")
            return result

        # === L12: 流动性检查（新增） ===
        daily_amount = signal.get('details', {}).get('daily_amount', 0)
        min_daily_amount = self._layers.get(12, {}).get('min_daily_amount', 3000)
        if daily_amount < min_daily_amount:  # 日均成交额不足（万元）
            result.reject("L12-流动性检查", f"日均成交额{daily_amount:.0f}万，流动性不足")
            return result

        return result

    def quick_check(self, ts_code: str, score: float,
                    stock_info: Dict = None) -> Tuple[bool, str]:
        """快速检查（仅关键层）"""
        # L4: 涨停
        # L5: 评分
        if score < 5:
            return False, f"评分{score:.0f}<5"

        # L8: ST
        if stock_info and stock_info.get('is_st'):
            return False, "ST股票"

        return True, "ok"
