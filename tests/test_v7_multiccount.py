"""
猎场系统 V7.0 — 多账户集成测试
"""
import sys
import os
import tempfile
import pytest
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def db():
    """创建临时测试数据库"""
    from data.storage import Database
    db_path = os.path.join(tempfile.gettempdir(), 'test_v7_pytest.db')
    if os.path.exists(db_path):
        os.unlink(db_path)
    database = Database(db_path)
    database.apply_migration('v7.0_add_account_id')
    database.apply_migration('v7.0_add_new_tables')
    yield database
    os.unlink(db_path)


class TestAccountManager:
    """账户管理器测试"""

    def test_create_account(self, db):
        from accounts.manager import AccountManager
        from accounts.account import VirtualAccount

        mgr = AccountManager(db)
        acct = mgr.create_account('aggressive', 700000, account_id='agg_test', name='进攻型测试')

        assert acct.account_id == 'agg_test'
        assert acct.name == '进攻型测试'
        assert acct.profile_type == 'aggressive'
        assert acct.initial_capital == 700000
        assert acct.cash == 700000
        assert acct.state == 'stopped'

    def test_create_all_profiles(self, db):
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        mgr.create_account('aggressive', 700000, account_id='agg')
        mgr.create_account('conservative', 700000, account_id='con')
        mgr.create_account('growth_hunter', 600000, account_id='gh')

        assert len(mgr.list_accounts()) == 3

    def test_duplicate_account(self, db):
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        a1 = mgr.create_account('aggressive', 700000, account_id='dup')
        a2 = mgr.create_account('conservative', 700000, account_id='dup')

        assert a1.account_id == a2.account_id  # 返回已存在的
        assert a1.profile_type == 'aggressive'  # 保持原始类型

    def test_account_crud(self, db):
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        mgr.create_account('aggressive', 700000, account_id='crud_test')

        # Read
        acct = mgr.get_account('crud_test')
        assert acct is not None

        # Update
        mgr.update_account('crud_test', cash=690000)
        assert mgr.get_account('crud_test').cash == 690000

        # Summary
        summaries = mgr.get_accounts_summary()
        assert len(summaries) == 1
        assert summaries[0]['account_id'] == 'crud_test'

        # Delete (soft)
        mgr.delete_account('crud_test', soft=True)
        assert mgr.get_account('crud_test').state == 'archived'

    def test_invalid_profile(self, db):
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        with pytest.raises(ValueError, match='Unknown profile_type'):
            mgr.create_account('invalid_type', 100000)

    def test_total_capital(self, db):
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        mgr.create_account('aggressive', 700000, account_id='a1')
        mgr.create_account('conservative', 700000, account_id='a2')
        mgr.create_account('growth_hunter', 600000, account_id='a3')

        assert mgr.get_total_capital() == 2_000_000

    def test_account_repr(self, db):
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        acct = mgr.create_account('aggressive', 700000, account_id='repr_test')

        r = repr(acct)
        assert 'repr_test' in r
        assert 'aggressive' in r
        assert '700,000' in r


class TestAccountPersistence:
    """账户持久化测试"""

    def test_save_and_load(self, db):
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        acct = mgr.create_account('growth_hunter', 600000, account_id='gh', name='成长猎手')

        # Verify DB retrieval
        row = db.get_account('gh')
        assert row is not None
        assert row['name'] == '成长猎手'
        assert row['profile_type'] == 'growth_hunter'

    def test_account_update_persists(self, db):
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        acct = mgr.create_account('aggressive', 700000, account_id='persist')
        acct.total_value = 750000
        acct.total_pnl = 50000
        acct.total_pnl_pct = 7.14

        mgr.update_account('persist', total_value=750000, total_pnl=50000, total_pnl_pct=7.14)

        loaded = mgr.get_account('persist')
        assert loaded.total_value == 750000
        assert loaded.total_pnl == 50000

    def test_list_accounts_empty(self, db):
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        assert mgr.list_accounts() == []


class TestPositionSizer:
    """仓位计算器测试"""

    def test_kelly_criterion(self):
        from decision.position_sizer import PositionSizer

        sizer = PositionSizer(total_capital=2_000_000, max_positions=6, max_single_pct=0.25)
        result = sizer.calculate('000001.SZ', 15.0, signal_score=60,
                                 volatility=0.02, win_rate=0.55,
                                 avg_win_loss_ratio=1.5, current_positions=2)

        assert result['shares'] >= 0
        assert result['position_pct'] <= 0.25
        assert 'Kelly' in result['method']

    def test_position_capped_by_max(self):
        from decision.position_sizer import PositionSizer

        sizer = PositionSizer(total_capital=2_000_000, max_positions=4, max_single_pct=0.25)
        pct_capped = PositionSizer(total_capital=2_000_000, max_positions=4, max_single_pct=0.10)
        r1 = sizer.calculate('000001.SZ', 15.0, signal_score=90)
        r2 = pct_capped.calculate('000001.SZ', 15.0, signal_score=90)
        assert r2['position_pct'] <= 0.10

    def test_minimum_position(self):
        from decision.position_sizer import PositionSizer

        sizer = PositionSizer(total_capital=100_000, max_positions=6, max_single_pct=0.25)
        result = sizer.calculate('000001.SZ', 15.0, signal_score=30,
                                 ai_suggested_pct=0.01, current_positions=3)
        assert result['shares'] == 0  # 低于最低门槛

    def test_rebalance_skip(self):
        from decision.position_sizer import PositionSizer

        sizer = PositionSizer(max_positions=4)
        result = sizer.calculate_rebalance(
            {'000001.SZ': {'score': 80}, '000002.SZ': {'score': 75},
             '000003.SZ': {'score': 70}, '000004.SZ': {'score': 65}},
            {'score': 60}
        )
        assert result['action'] == 'skip'

    def test_rebalance_replace(self):
        from decision.position_sizer import PositionSizer

        sizer = PositionSizer(max_positions=4)
        result = sizer.calculate_rebalance(
            {'000001.SZ': {'score': 80}, '000002.SZ': {'score': 75},
             '000003.SZ': {'score': 70}, '000004.SZ': {'score': 50}},
            {'score': 62}
        )
        assert result['action'] == 'replace'
        assert result['replace_code'] == '000004.SZ'


class TestEventBus:
    """事件总线测试"""

    async def test_publish_subscribe(self):
        from core.event_bus import EventBus, Event, EventType

        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        await bus.subscribe(EventType.SYSTEM_STARTUP, handler)
        await bus.publish(Event(EventType.SYSTEM_STARTUP, data={'test': True}))

        assert len(received) == 1
        assert received[0].data == {'test': True}

    async def test_account_id_filter(self):
        from core.event_bus import EventBus, Event, EventType

        bus = EventBus()
        agg_events = []
        con_events = []
        all_events = []

        async def agg_handler(event):
            agg_events.append(event)

        async def con_handler(event):
            con_events.append(event)

        async def all_handler(event):
            all_events.append(event)

        await bus.subscribe(EventType.SYSTEM_STARTUP, agg_handler, account_id='agg')
        await bus.subscribe(EventType.SYSTEM_STARTUP, con_handler, account_id='con')
        await bus.subscribe(EventType.SYSTEM_STARTUP, all_handler)  # broadcast

        await bus.publish(Event(EventType.SYSTEM_STARTUP, account_id='agg'))

        assert len(agg_events) == 1
        assert len(con_events) == 0  # filtered out
        assert len(all_events) == 1  # broadcast received

    async def test_wildcard_pattern(self):
        from core.event_bus import EventBus, Event, EventType

        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        await bus.subscribe_pattern('market.*', handler)
        await bus.publish(Event(EventType.MARKET_DATA_UPDATED))
        await bus.publish(Event(EventType.REALTIME_QUOTE))

        assert len(received) == 2


class TestFactorEvaluator:
    """因子评估器测试"""

    def test_spearman(self):
        import numpy as np
        from stock_pool.factor_evaluator import FactorEvaluator

        eval_ = FactorEvaluator()
        np.random.seed(42)
        x = np.random.randn(100)
        y = 0.5 * x + np.random.randn(100) * 0.5

        ic = eval_.compute_rank_ic(x, y)
        assert -1 <= ic <= 1
        assert abs(ic) > 0  # 应有正相关

    def test_stratified_backtest(self):
        import numpy as np
        from stock_pool.factor_evaluator import FactorEvaluator

        eval_ = FactorEvaluator()
        np.random.seed(42)
        factor_values = np.random.randn(500)
        forward_returns = 0.3 * factor_values + np.random.randn(500) * 0.1

        result = eval_.stratified_backtest(factor_values, forward_returns, n_groups=5)
        assert len(result['group_returns']) == 5
        assert result['monotonicity'] >= 0.5  # 应大体单调

    def test_factor_correlation(self):
        import numpy as np
        from stock_pool.factor_evaluator import FactorEvaluator

        eval_ = FactorEvaluator()
        np.random.seed(42)
        fv1 = np.random.randn(200)
        fv2 = fv1 * 0.9 + np.random.randn(200) * 0.1

        corr = eval_.compute_factor_correlation(fv1, fv2)
        assert corr > 0.7  # 高度相关

    def test_evaluate_factor(self):
        import numpy as np
        from stock_pool.factor_evaluator import FactorEvaluator

        eval_ = FactorEvaluator()
        np.random.seed(42)
        fv = np.random.randn(300)
        fr = 0.2 * fv + np.random.randn(300) * 0.1
        ic_series = np.array([0.02, 0.03, 0.025, 0.028, 0.022, 0.026, 0.024, 0.027, 0.025, 0.03])

        result = eval_.evaluate_factor('test_factor', fv, fr, ic_series=ic_series)
        assert result is not None
        assert 'ic_value' in result
        assert 'effectiveness' in result
        assert 0 <= result['effectiveness'] <= 1


class TestExecution:
    """执行引擎测试"""

    def test_twap_split(self):
        from execution.order_splitter import OrderSplitter

        splitter = OrderSplitter()
        result = splitter.split('000001.SZ', 10000, 15.0, algo='TWAP',
                                session_duration_minutes=30)

        assert result['algo'] == 'TWAP'
        assert result['slices'] >= 1
        total = sum(s['shares'] for s in result['schedule'])
        assert total == 10000

    def test_slippage_estimate(self):
        from execution.slippage import SlippageEstimator

        slip = SlippageEstimator()
        result = slip.estimate('000001.SZ', 15.0, 50000, daily_volume=5000000,
                               volatility=0.02, urgency='normal')

        assert result['slippage_bps'] > 0
        assert result['recommended_algo'] in ('TWAP', 'VWAP', 'POV', 'Iceberg')
        assert 0 <= result['confidence'] <= 1

    def test_liquidity_check(self):
        from execution.liquidity import LiquidityAdapter

        adapter = LiquidityAdapter()
        result = adapter.check('000001.SZ', 5000, recent_volume=10000,
                               daily_volume=100000, daily_amount=50_000_000)

        assert result['adjusted_shares'] > 0
        assert result['liquidity_level'] in ('high', 'normal', 'low', 'illiquid')

    def test_timing_quality(self):
        from execution.timing import TimingOptimizer

        timing = TimingOptimizer()
        slots = timing.get_optimal_slots(10)
        assert len(slots) <= 10

        # 不应返回午休时段
        for delay_min, quality in slots:
            assert 0 <= delay_min <= 240


class TestGrayDeployer:
    """灰度部署器测试"""

    def test_propose_and_advance(self):
        from evolution.gray_deployer import GrayScaleDeployer
        from datetime import timedelta, datetime

        deployer = GrayScaleDeployer(account_id='test')

        # Phase 1 proposal
        result = deployer.propose(
            'test_strategy',
            new_params={'score_threshold': 50, 'position_pct': 0.30},
            old_params={'score_threshold': 45, 'position_pct': 0.25},
            expected_improvement=0.05,
        )
        assert result['approved']
        assert result['phase'] == 1
        assert result['capital_pct'] == 0.20

        # Simulate time passing by backdating phase_start
        deployer._deployments['test_strategy']['phase_start'] = (
            datetime.now() - timedelta(days=7)
        ).isoformat()

        # Phase 1 passed → advance to Phase 2
        eval_result = deployer.evaluate_phase('test_strategy', phase_pnl=10500, old_strategy_pnl=10000)
        assert eval_result['action'] == 'advance'
        assert eval_result['next_phase'] == 2

    def test_rollback_on_loss(self):
        from evolution.gray_deployer import GrayScaleDeployer
        from datetime import timedelta, datetime

        deployer = GrayScaleDeployer(account_id='test')
        deployer.propose(
            'fail_strategy',
            new_params={'score_threshold': 52},  # 45→52 = 15.6% change (under 20% cap)
            old_params={'score_threshold': 45},
            expected_improvement=0.05,
        )

        assert 'fail_strategy' in deployer._deployments

        # Simulate time passing
        deployer._deployments['fail_strategy']['phase_start'] = (
            datetime.now() - timedelta(days=7)
        ).isoformat()

        result = deployer.evaluate_phase('fail_strategy', phase_pnl=9000, old_strategy_pnl=10000)
        assert result['action'] == 'rollback'

    def test_pre_deploy_checks_negative(self):
        from evolution.gray_deployer import GrayScaleDeployer

        deployer = GrayScaleDeployer(account_id='test')
        result = deployer.propose(
            'bad_strategy',
            new_params={'score_threshold': 50},
            old_params={'score_threshold': 45},
            expected_improvement=-0.05,  # 负预期 → 拒绝
        )
        assert not result['approved']

    def test_change_limit(self):
        from evolution.gray_deployer import GrayScaleDeployer

        deployer = GrayScaleDeployer(account_id='test')
        result = deployer.propose(
            'big_change',
            new_params={'score_threshold': 80},  # 从默认45变到80，变化>20%
            old_params={'score_threshold': 45},
            expected_improvement=0.10,
        )
        assert not result['approved']  # 超过20%限制


class TestBayesianTuner:
    """贝叶斯优化器测试"""

    def test_optimize_simple(self):
        from evolution.bayesian_tuner import BayesianTuner

        bounds = {'x': (0, 10)}
        tuner = BayesianTuner(bounds)

        def objective(params):
            x = params['x']
            return -(x - 5) ** 2 + 10  # 最大值在 x=5

        result = tuner.optimize(objective, n_iterations=15, n_initial_random=3)
        assert 'best_params' in result
        assert 'best_value' in result
        assert result['best_value'] > -1

    def test_compare_with_ga(self):
        from evolution.bayesian_tuner import BayesianTuner

        tuna = BayesianTuner()
        ga_result = {'best_params': {'x': 50}, 'best_fitness': 0.8}
        bayes_result = {'best_params': {'x': 52}, 'best_value': 0.85}

        result = tuna.compare_with_ga(ga_result, bayes_result)
        assert result['winner'] == 'bayesian'
        assert result['improvement'] > 0


class TestAssetAllocator:
    """资产配置器测试"""

    def test_get_target_allocation(self, db):
        from accounts.allocator import AssetAllocator
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        acct = mgr.create_account('aggressive', 700000, account_id='alloc_test')

        alloc = AssetAllocator.get_target_allocation(acct)
        assert 'a_share' in alloc
        assert alloc['a_share'] > 0

    def test_compute_drift(self, db):
        from accounts.allocator import AssetAllocator
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        acct = mgr.create_account('aggressive', 700000, account_id='drift_test')

        # 全部现金 → 大幅低配
        current = {'a_share': 0, 'convertible_bond': 0, 'etf': 0, 'cash_reserve': 700000}
        drift = AssetAllocator.compute_drift(acct, current)
        assert drift['a_share'] < 0  # 低配

    def test_suggest_rebalance(self, db):
        from accounts.allocator import AssetAllocator
        from accounts.manager import AccountManager

        mgr = AccountManager(db)
        acct = mgr.create_account('aggressive', 700000, account_id='rb_test')

        current = {'a_share': 0, 'convertible_bond': 0, 'etf': 0, 'cash_reserve': 700000}
        suggestions = AssetAllocator.suggest_rebalance(acct, current, threshold=0.05)
        assert suggestions['a_share'] == 'buy'  # 低配 → 买入
