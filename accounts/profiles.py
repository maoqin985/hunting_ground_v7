"""
猎场系统 V7.0 — 预制账户策略配置
三种人格：攻击型 / 稳定型 / 成长猎手
"""
from typing import Dict


# ============================================================
# 进攻型 (Aggressive) — 高集中度、动量驱动、快进快出
# ============================================================
AGGRESSIVE_PROFILE: Dict = {
    'name': '进攻型',
    'description': '高集中度动量驱动，快进快出，追求短期爆发收益',
    'capital_allocation': {
        'a_share': 0.60,
        'convertible_bond': 0.30,
        'etf': 0.05,
        'cash_reserve': 0.05,
    },
    'strategy': {
        'score_threshold': 55,
        'position_pct': 0.35,
        'max_positions': 4,
        'max_single_pct': 0.30,
        'focus_market_cap': (10, 30),  # 亿 — 更小市值
        'rsi_threshold': 25,
        'volume_ratio_threshold': 1.5,
        'min_daily_amount': 1500,  # 万元
    },
    'risk': {
        'hard_stop_pct': -0.03,      # -3% 紧止损
        'trailing_stop_pct': -0.06,
        'trailing_activate_pct': 0.04,
        'take_profit_pct': 0.15,      # +15% 止盈
        'max_hold_days': 5,
        'daily_loss_limit': -0.05,    # 日亏5%熔断
        'weekly_loss_limit': -0.10,   # 周亏10%熔断
        'max_drawdown_limit': -0.20,  # 全局回撤20%暂停
    },
    'signal_weights': {
        'technical': 0.30,
        'fund_flow': 0.40,
        'sentiment': 0.15,
        'ai_enhanced': 0.15,
    },
    'model_weights': {
        'xgb_weight': 0.35,
        'lstm_weight': 0.30,
        'risk_weight': 0.20,
        'ppo_weight': 0.15,
    },
    'focus_sectors': [],  # 空=全行业，不限制
    'rebalance_days': 1,   # 每日再平衡
    'enable_factor_mining': False,
}


# ============================================================
# 稳定型 (Conservative) — 分散持仓、价值导向、长期持有
# ============================================================
CONSERVATIVE_PROFILE: Dict = {
    'name': '稳定型',
    'description': '分散持仓价值导向，长期持有，追求稳健复利',
    'capital_allocation': {
        'a_share': 0.45,
        'convertible_bond': 0.20,
        'etf': 0.20,
        'cash_reserve': 0.15,
    },
    'strategy': {
        'score_threshold': 45,
        'position_pct': 0.15,
        'max_positions': 8,
        'max_single_pct': 0.18,
        'focus_market_cap': (30, 50),  # 亿 — 偏大市值
        'rsi_threshold': 35,
        'volume_ratio_threshold': 2.0,
        'min_daily_amount': 5000,  # 万元 — 流动性要求更高
    },
    'risk': {
        'hard_stop_pct': -0.07,      # -7% 宽止损
        'trailing_stop_pct': -0.10,
        'trailing_activate_pct': 0.08,
        'take_profit_pct': 0.25,      # +25% 止盈
        'max_hold_days': 20,
        'daily_loss_limit': -0.03,    # 日亏3%熔断
        'weekly_loss_limit': -0.06,   # 周亏6%熔断
        'max_drawdown_limit': -0.15,  # 全局回撤15%暂停
    },
    'signal_weights': {
        'technical': 0.25,
        'fund_flow': 0.25,
        'sentiment': 0.25,
        'ai_enhanced': 0.25,
    },
    'model_weights': {
        'xgb_weight': 0.30,
        'lstm_weight': 0.25,
        'risk_weight': 0.30,
        'ppo_weight': 0.15,
    },
    'focus_sectors': [],  # 分散全行业
    'rebalance_days': 5,   # 每周再平衡
    'enable_factor_mining': False,
}


# ============================================================
# 成长猎手 (Growth Hunter) — 小盘突破发现、因子挖掘驱动
# ============================================================
GROWTH_HUNTER_PROFILE: Dict = {
    'name': '成长猎手',
    'description': 'AI驱动小盘突破发现，因子挖掘，专注高成长潜力的中小市值股票',
    'capital_allocation': {
        'a_share': 0.55,
        'convertible_bond': 0.20,
        'etf': 0.15,
        'cash_reserve': 0.10,
    },
    'strategy': {
        'score_threshold': 40,         # 低门槛以捕获更多候选
        'position_pct': 0.25,
        'max_positions': 6,
        'max_single_pct': 0.22,
        'focus_market_cap': (10, 30),  # 亿 — 中小市值
        'rsi_threshold': 30,
        'volume_ratio_threshold': 1.8,
        'min_daily_amount': 2000,  # 万元
    },
    'risk': {
        'hard_stop_pct': -0.05,
        'trailing_stop_pct': -0.08,
        'trailing_activate_pct': 0.05,
        'take_profit_pct': 0.20,
        'max_hold_days': 7,
        'daily_loss_limit': -0.04,
        'weekly_loss_limit': -0.08,
        'max_drawdown_limit': -0.18,
    },
    'signal_weights': {
        'technical': 0.20,
        'fund_flow': 0.25,
        'sentiment': 0.20,
        'ai_enhanced': 0.35,          # 重AI因子
    },
    'model_weights': {
        'xgb_weight': 0.25,
        'lstm_weight': 0.30,
        'risk_weight': 0.15,
        'ppo_weight': 0.30,           # 强化学习做仓位
    },
    'focus_sectors': [],  # 不限行业，让因子发现
    'rebalance_days': 1,   # 每日再平衡
    'enable_factor_mining': True,      # 启用因子挖掘
}


# 人格映射表
PROFILE_MAP = {
    'aggressive': AGGRESSIVE_PROFILE,
    'conservative': CONSERVATIVE_PROFILE,
    'growth_hunter': GROWTH_HUNTER_PROFILE,
}
