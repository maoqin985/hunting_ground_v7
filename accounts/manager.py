"""
猎场系统 V7.0 — 账户管理器
创建/管理/编排多个虚拟账户
"""
import logging
import asyncio
from typing import Dict, List, Optional
from datetime import datetime

from .account import VirtualAccount
from .profiles import PROFILE_MAP

logger = logging.getLogger(__name__)


class AccountManager:
    """
    多账户管理器
    - 创建/加载/配置虚拟账户
    - 编排 per-account 交易周期
    - 跨账户汇总
    """

    def __init__(self, db, config: dict = None):
        self.db = db
        self.config = config or {}
        self._accounts: Dict[str, VirtualAccount] = {}
        self._initialized = False

    # ---- 账户生命周期 ----

    async def initialize(self):
        """从数据库加载已有账户"""
        rows = self.db.list_accounts()
        for row in rows:
            acct = VirtualAccount.from_dict(row)
            self._accounts[acct.account_id] = acct
            logger.info(f"Loaded account: {acct}")
        self._initialized = True

    def create_account(self, profile_type: str, initial_capital: float,
                       account_id: str = None, name: str = None) -> VirtualAccount:
        """
        创建新虚拟账户

        Args:
            profile_type: 'aggressive' | 'conservative' | 'growth_hunter'
            initial_capital: 初始资金
            account_id: 账户ID (可选，默认用 profile_type)
            name: 显示名称 (可选)

        Returns:
            VirtualAccount
        """
        if profile_type not in PROFILE_MAP:
            raise ValueError(f"Unknown profile_type: {profile_type}. Choose from {list(PROFILE_MAP.keys())}")

        profile = PROFILE_MAP[profile_type]
        acct_id = account_id or profile_type

        # 检查是否已存在
        if acct_id in self._accounts:
            logger.warning(f"Account {acct_id} already exists, returning existing")
            return self._accounts[acct_id]

        acct = VirtualAccount(
            account_id=acct_id,
            name=name or profile['name'],
            profile_type=profile_type,
            initial_capital=initial_capital,
            cash=initial_capital,
            total_value=initial_capital,
            strategy_params=profile['strategy'],
            risk_params=profile['risk'],
            state='stopped',
        )
        acct._day_start_value = initial_capital
        acct._week_start_value = initial_capital
        acct._peak_value = initial_capital

        # 持久化
        self.db.save_account(acct.to_dict())
        self._accounts[acct_id] = acct
        logger.info(f"Created account: {acct}")
        return acct

    def get_account(self, account_id: str) -> Optional[VirtualAccount]:
        """获取账户"""
        return self._accounts.get(account_id)

    def list_accounts(self) -> List[VirtualAccount]:
        """列出所有账户"""
        return list(self._accounts.values())

    def get_accounts_summary(self) -> List[Dict]:
        """所有账户摘要"""
        return [
            {
                'account_id': a.account_id,
                'name': a.name,
                'profile_type': a.profile_type,
                'initial_capital': a.initial_capital,
                'total_value': a.total_value,
                'cash': a.cash,
                'total_pnl': a.total_pnl,
                'total_pnl_pct': a.total_pnl_pct,
                'daily_pnl': a.daily_pnl,
                'daily_pnl_pct': a.daily_pnl_pct,
                'state': a.state,
            }
            for a in self._accounts.values()
        ]

    def update_account(self, account_id: str, **kwargs) -> bool:
        """更新账户字段"""
        acct = self._accounts.get(account_id)
        if not acct:
            return False
        for key, value in kwargs.items():
            if hasattr(acct, key):
                setattr(acct, key, value)
        acct.updated_at = datetime.now().isoformat()
        self.db.save_account(acct.to_dict())
        return True

    def delete_account(self, account_id: str, soft: bool = True) -> bool:
        """
        删除账户
        soft=True: 软删除 (state='archived')
        soft=False: 硬删除 (从DB移除)
        """
        if account_id not in self._accounts:
            return False
        acct = self._accounts[account_id]
        if soft:
            acct.state = 'archived'
            self.db.save_account(acct.to_dict())
        else:
            self.db.delete_account(account_id)
            del self._accounts[account_id]
        logger.info(f"Deleted account {account_id} (soft={soft})")
        return True

    # ---- 账户控制 ----

    async def start_account(self, account_id: str):
        """启动单个账户"""
        acct = self._accounts.get(account_id)
        if not acct:
            raise ValueError(f"Account not found: {account_id}")
        if acct.state == 'running':
            return
        acct.state = 'starting'
        self.db.save_account(acct.to_dict())
        # 实际组件初始化由调用方完成
        logger.info(f"Account {account_id} starting")

    async def pause_account(self, account_id: str):
        acct = self._accounts.get(account_id)
        if acct and acct.state == 'running':
            acct.state = 'paused'
            self.db.save_account(acct.to_dict())
            if acct.engine:
                await acct.engine.pause()

    async def resume_account(self, account_id: str):
        acct = self._accounts.get(account_id)
        if acct and acct.state == 'paused':
            acct.state = 'running'
            self.db.save_account(acct.to_dict())
            if acct.engine:
                await acct.engine.resume()

    async def stop_account(self, account_id: str):
        acct = self._accounts.get(account_id)
        if acct and acct.state in ('running', 'paused'):
            acct.state = 'stopping'
            self.db.save_account(acct.to_dict())
            if acct.engine:
                await acct.engine.stop()
            acct.state = 'stopped'
            self.db.save_account(acct.to_dict())

    async def pause_all(self):
        for acct_id in self._accounts:
            await self.pause_account(acct_id)

    async def resume_all(self):
        for acct_id in self._accounts:
            await self.resume_account(acct_id)

    # ---- 跨账户汇总 ----

    def get_total_capital(self) -> float:
        """跨账户总资金"""
        return sum(a.total_value for a in self._accounts.values()
                   if a.state != 'archived')

    def get_total_pnl(self) -> Dict:
        """跨账户总盈亏"""
        total = 0.0
        total_capital = 0.0
        for a in self._accounts.values():
            if a.state != 'archived':
                total += a.total_pnl
                total_capital += a.initial_capital
        return {
            'total_pnl': total,
            'total_pnl_pct': (total / total_capital * 100) if total_capital > 0 else 0,
            'total_capital': total_capital,
            'account_count': sum(1 for a in self._accounts.values() if a.state != 'archived'),
        }

    # ---- 运行时组件注册 ----

    def register_engine(self, account_id: str, engine):
        """注册账户引擎实例"""
        acct = self._accounts.get(account_id)
        if acct:
            acct.engine = engine

    def register_trading(self, account_id: str, auto_trading, circuit_breaker,
                         stop_loss_monitor, position_monitor, model_ensemble, signal_fusion):
        """注册账户交易组件"""
        acct = self._accounts.get(account_id)
        if acct:
            acct.auto_trading = auto_trading
            acct.circuit_breaker = circuit_breaker
            acct.stop_loss_monitor = stop_loss_monitor
            acct.position_monitor = position_monitor
            acct.model_ensemble = model_ensemble
            acct.signal_fusion = signal_fusion

    @property
    def is_initialized(self) -> bool:
        return self._initialized
