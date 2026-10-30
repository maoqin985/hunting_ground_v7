"""
猎场系统 V7.0 — 虚拟账户
每个 VirtualAccount 拥有独立的资金、策略参数、风险参数、持仓和AI模型
"""
import logging
from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class VirtualAccount:
    """独立虚拟交易账户"""

    account_id: str
    name: str
    profile_type: str  # aggressive | conservative | growth_hunter

    # 资金
    initial_capital: float
    cash: float
    total_value: float = 0.0

    # 盈亏追踪
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    weekly_pnl: float = 0.0
    weekly_pnl_pct: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0

    # 状态
    state: str = 'stopped'  # stopped | starting | running | paused | stopping | error

    # 配置覆盖 (JSON-serializable)
    strategy_params: Dict = field(default_factory=dict)
    risk_params: Dict = field(default_factory=dict)

    # 运行时组件引用 (由 AccountManager 注入)
    engine: object = field(default=None, repr=False)
    auto_trading: object = field(default=None, repr=False)
    circuit_breaker: object = field(default=None, repr=False)
    stop_loss_monitor: object = field(default=None, repr=False)
    position_monitor: object = field(default=None, repr=False)
    model_ensemble: object = field(default=None, repr=False)
    signal_fusion: object = field(default=None, repr=False)

    # 元数据
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # 日/周起始值 (用于计算 period PnL)
    _day_start_value: float = field(default=0.0, repr=False)
    _week_start_value: float = field(default=0.0, repr=False)
    _peak_value: float = field(default=0.0, repr=False)  # 用于回撤计算

    def to_dict(self) -> Dict:
        """序列化为字典 (持久化到 DB)"""
        return {
            'id': self.account_id,
            'name': self.name,
            'profile_type': self.profile_type,
            'initial_capital': self.initial_capital,
            'cash': self.cash,
            'total_value': self.total_value,
            'daily_pnl': self.daily_pnl,
            'daily_pnl_pct': self.daily_pnl_pct,
            'weekly_pnl': self.weekly_pnl,
            'weekly_pnl_pct': self.weekly_pnl_pct,
            'total_pnl': self.total_pnl,
            'total_pnl_pct': self.total_pnl_pct,
            'state': self.state,
            'strategy_params': self.strategy_params,
            'risk_params': self.risk_params,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'VirtualAccount':
        """从字典反序列化"""
        import json
        strat = data.get('strategy_params', {})
        risk = data.get('risk_params', {})
        if isinstance(strat, str):
            strat = json.loads(strat)
        if isinstance(risk, str):
            risk = json.loads(risk)

        return cls(
            account_id=data['id'],
            name=data.get('name', ''),
            profile_type=data.get('profile_type', ''),
            initial_capital=data.get('initial_capital', 0),
            cash=data.get('cash', data.get('initial_capital', 0)),
            total_value=data.get('total_value', 0),
            daily_pnl=data.get('daily_pnl', 0),
            daily_pnl_pct=data.get('daily_pnl_pct', 0),
            weekly_pnl=data.get('weekly_pnl', 0),
            weekly_pnl_pct=data.get('weekly_pnl_pct', 0),
            total_pnl=data.get('total_pnl', 0),
            total_pnl_pct=data.get('total_pnl_pct', 0),
            state=data.get('state', 'stopped'),
            strategy_params=strat,
            risk_params=risk,
            created_at=data.get('created_at', ''),
            updated_at=data.get('updated_at', ''),
        )

    def reset_pnl_tracking(self):
        """重置盈亏追踪 (新交易日/周开始时调用)"""
        self.daily_pnl = 0.0
        self.daily_pnl_pct = 0.0
        self.weekly_pnl = 0.0
        self.weekly_pnl_pct = 0.0
        self._day_start_value = self.total_value
        self._week_start_value = self.total_value

    def update_total_value(self, cash: float, position_value: float):
        """更新总资产并计算 PnL"""
        old_total = self.total_value
        self.cash = cash
        self.total_value = cash + position_value
        self.total_pnl = self.total_value - self.initial_capital
        self.total_pnl_pct = (self.total_value / self.initial_capital - 1) * 100 if self.initial_capital > 0 else 0

        if self._day_start_value > 0:
            self.daily_pnl = self.total_value - self._day_start_value
            self.daily_pnl_pct = (self.total_value / self._day_start_value - 1) * 100

        if self._week_start_value > 0:
            self.weekly_pnl = self.total_value - self._week_start_value
            self.weekly_pnl_pct = (self.total_value / self._week_start_value - 1) * 100

        if self.total_value > self._peak_value:
            self._peak_value = self.total_value

    def get_drawdown_pct(self) -> float:
        """当前回撤百分比"""
        if self._peak_value <= 0:
            return 0.0
        return (self.total_value / self._peak_value - 1) * 100

    def __repr__(self) -> str:
        return (f"VirtualAccount(id={self.account_id}, name={self.name}, "
                f"type={self.profile_type}, capital={self.initial_capital:,.0f}, "
                f"state={self.state}, pnl={self.total_pnl_pct:+.2f}%)")
