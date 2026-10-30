"""
猎场系统 V7.0 — 灰度迭代部署器
Phase 1: 20% → Phase 2: 50% → Phase 3: 100% (自动回滚)
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class GrayScaleDeployer:
    """
    灰度发布部署器

    三阶段渐进部署：
    - Phase 1: 20% 资金试运行，观察 1 周
    - Phase 2: 50% 资金，观察 1 周
    - Phase 3: 100% 全量切换（需连续2周跑赢）

    自动回滚：
    - 任一周跑输 → 立即回退到旧参数
    - 连续2周观察期未改善 → 淘汰

    状态机：
      standby → Phase1(20%) → Phase2(50%) → Phase3(100% → active)
                        ↓              ↓
                      rollback       rollback
    """

    PHASES = {
        1: {'pct': 0.20, 'duration_days': 5, 'name': 'Phase1: 20% Pilot'},
        2: {'pct': 0.50, 'duration_days': 5, 'name': 'Phase2: 50% Scale'},
        3: {'pct': 1.00, 'duration_days': 10, 'name': 'Phase3: 100% Full'},
    }

    def __init__(self, db=None, account_id: str = None):
        self.db = db
        self.account_id = account_id
        self._deployments: Dict[str, Dict] = {}  # strategy_name -> deploy_state

    def propose(self, strategy_name: str,
                new_params: Dict,
                old_params: Dict,
                expected_improvement: float,
                backtest_sharpe: float = None,
                backtest_return: float = None) -> Dict:
        """
        提议新参数灰度部署

        Args:
            strategy_name: 策略名
            new_params: 新参数
            old_params: 当前参数
            expected_improvement: 预期改善幅度
            backtest_sharpe: Walk-Forward 夏普
            backtest_return: Walk-Forward 收益

        Returns:
            {approved, phase, reason, deploy_id}
        """
        # 部署前检查
        checks = self._pre_deploy_checks(new_params, old_params, expected_improvement)

        if not checks['passed']:
            return {
                'approved': False,
                'phase': 0,
                'reason': checks['reasons'][0] if checks['reasons'] else 'Unknown',
                'checks': checks,
            }

        # 创建部署记录
        deploy_id = f"{strategy_name}_{datetime.now().strftime('%Y%m%d_%H%M')}"
        deploy_state = {
            'deploy_id': deploy_id,
            'strategy_name': strategy_name,
            'phase': 1,
            'phase_start': datetime.now().isoformat(),
            'new_params': new_params,
            'old_params': old_params,
            'expected_improvement': expected_improvement,
            'backtest_sharpe': backtest_sharpe,
            'backtest_return': backtest_return,
            'status': 'running',
            'phase_history': [],
        }

        self._deployments[strategy_name] = deploy_state

        logger.info(f"GrayScale deploy PROPOSED: {strategy_name} Phase1 (20%) "
                    f"improvement={expected_improvement:.1%}")

        return {
            'approved': True,
            'phase': 1,
            'deploy_id': deploy_id,
            'reason': self.PHASES[1]['name'],
            'capital_pct': self.PHASES[1]['pct'],
            'checks': checks,
        }

    def evaluate_phase(self, strategy_name: str,
                        phase_pnl: float,
                        old_strategy_pnl: float) -> Dict:
        """
        评估当前阶段表现，决定晋升或回滚

        Returns:
            {action: 'advance'|'rollback'|'keep', next_phase, reason}
        """
        deploy = self._deployments.get(strategy_name)
        if not deploy or deploy['status'] != 'running':
            return {'action': 'keep', 'next_phase': deploy['phase'] if deploy else 0,
                    'reason': 'No active deployment'}

        current_phase = deploy['phase']
        phase_cfg = self.PHASES[current_phase]

        # 检查是否到了评估时间
        phase_start = datetime.fromisoformat(deploy['phase_start'])
        days_elapsed = (datetime.now() - phase_start).days
        if days_elapsed < phase_cfg['duration_days']:
            return {'action': 'keep', 'next_phase': current_phase,
                    'reason': f'Observation period ({days_elapsed}/{phase_cfg["duration_days"]} days)'}

        # 计算新策略相对表现
        outperformance = phase_pnl - old_strategy_pnl

        # 评估
        if outperformance > 0:
            # 跑赢旧策略
            if current_phase < 3:
                next_phase = current_phase + 1
                deploy['phase'] = next_phase
                deploy['phase_start'] = datetime.now().isoformat()
                deploy['phase_history'].append({
                    'phase': current_phase,
                    'result': 'passed',
                    'pnl': phase_pnl,
                    'old_pnl': old_strategy_pnl,
                    'outperformance': outperformance,
                })

                logger.info(f"GrayScale [{strategy_name}]: Phase {current_phase} PASSED → "
                           f"Phase {next_phase} ({self.PHASES[next_phase]['name']})")

                return {
                    'action': 'advance',
                    'next_phase': next_phase,
                    'capital_pct': self.PHASES[next_phase]['pct'],
                    'reason': f'Outperformed by {outperformance:+.2f}',
                }
            else:
                # Phase 3 完成 — 全量切换
                deploy['status'] = 'completed'
                deploy['phase_history'].append({
                    'phase': current_phase,
                    'result': 'completed',
                    'pnl': phase_pnl,
                    'outperformance': outperformance,
                })
                logger.info(f"GrayScale [{strategy_name}]: ALL phases PASSED → FULL deployment")

                return {
                    'action': 'complete',
                    'next_phase': 3,
                    'capital_pct': 1.0,
                    'reason': f'Full deployment approved: {outperformance:+.2f}',
                }
        else:
            # 跑输 — 回滚
            self._rollback(strategy_name, current_phase, outperformance)

            logger.warning(f"GrayScale [{strategy_name}]: Phase {current_phase} FAILED → ROLLBACK "
                          f"({outperformance:+.2f})")

            return {
                'action': 'rollback',
                'next_phase': 0,
                'capital_pct': 0,
                'reason': f'Underperformed by {outperformance:+.2f}, rolling back',
            }

    def _rollback(self, strategy_name: str, failed_phase: int, pnl_diff: float):
        """执行回滚"""
        deploy = self._deployments.get(strategy_name)
        if not deploy:
            return

        deploy['status'] = 'rolled_back'
        deploy['phase_history'].append({
            'phase': failed_phase,
            'result': 'rolled_back',
            'pnl_diff': pnl_diff,
        })

        # 记录回滚事件
        if self.db:
            self.db.save_lifecycle_record({
                'strategy_name': strategy_name,
                'period': 'deployment',
                'period_start': deploy.get('phase_start', ''),
                'period_end': datetime.now().strftime('%Y-%m-%d'),
                'sharpe_ratio': 0,
                'win_rate': 0,
                'max_drawdown': 0,
                'total_return': pnl_diff,
                'trade_count': 0,
                'avg_hold_days': 0,
                'parameters': {
                    'event': 'gray_rollback',
                    'failed_phase': failed_phase,
                    'pnl_diff': pnl_diff,
                    'deploy_id': deploy.get('deploy_id', ''),
                },
                'status': 'rolled_back',
            })

    def get_status(self, strategy_name: str = None) -> Dict:
        """获取灰度部署状态"""
        if strategy_name:
            deploy = self._deployments.get(strategy_name)
            if not deploy:
                return {'status': 'no_deployment', 'strategy_name': strategy_name}
            return {
                'strategy_name': strategy_name,
                'status': deploy['status'],
                'phase': deploy['phase'],
                'phase_name': self.PHASES[deploy['phase']]['name'],
                'capital_pct': self.PHASES[deploy['phase']]['pct'],
                'phase_start': deploy['phase_start'],
                'expected_improvement': deploy['expected_improvement'],
            }

        return {
            str_name: {
                'status': d['status'],
                'phase': d['phase'],
                'phase_name': self.PHASES[d['phase']]['name'],
            }
            for str_name, d in self._deployments.items()
        }

    def _pre_deploy_checks(self, new_params: Dict, old_params: Dict,
                            expected_improvement: float) -> Dict:
        """部署前检查"""
        reasons = []
        passed = True

        # 检查1: 必须有正预期改善
        if expected_improvement <= 0:
            reasons.append(f"Expected improvement {expected_improvement:.2%} <= 0")
            passed = False

        # 检查2: 参数变化不能超过20%（安全性）
        for key in new_params:
            if key in old_params and isinstance(old_params[key], (int, float)):
                old_val = old_params[key]
                new_val = new_params[key]
                if old_val != 0:
                    change = abs(new_val - old_val) / abs(old_val)
                    if change > 0.20:
                        reasons.append(f"Parameter '{key}' change {change:.0%} > 20% cap")
                        passed = False

        # 检查3: 不能有冲突的同策略部署
        # (简化：检查是否已有活跃部署)

        return {
            'passed': passed,
            'reasons': reasons if not passed else ['All checks passed'],
            'checks_run': ['improvement_check', 'change_limit_check'],
        }
