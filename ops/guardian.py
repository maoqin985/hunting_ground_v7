"""
猎场系统 V6.0 — 系统巡检报告（每15分钟）
"""

import logging
from typing import Dict
from datetime import datetime

logger = logging.getLogger(__name__)


class OpsGuardian:
    """系统巡检"""

    def __init__(self, db, engine=None):
        self.db = db
        self.engine = engine

    async def run_check(self) -> Dict:
        """执行全系统巡检"""
        now = datetime.now()
        report = {
            'timestamp': now.isoformat(),
            'status': 'healthy',
            'checks': {},
            'warnings': [],
            'errors': [],
        }

        # 1. 数据库检查
        try:
            db_stats = self.db.get_stats()
            report['checks']['database'] = db_stats
            if not db_stats.get('latest_kline_date'):
                report['warnings'].append('No kline data')
        except Exception as e:
            report['checks']['database'] = {'error': str(e)}
            report['errors'].append(f'Database: {e}')

        # 2. 引擎状态
        if self.engine:
            try:
                eng_status = self.engine.get_status()
                report['checks']['engine'] = eng_status
                if eng_status['state'] != 'running':
                    report['warnings'].append(f"Engine state: {eng_status['state']}")
            except Exception as e:
                report['errors'].append(f'Engine: {e}')

        # 3. 持仓检查
        try:
            positions = self.db.get_all_positions()
            report['checks']['positions'] = {
                'count': len(positions),
                'value': sum(p.get('market_value', 0) or 0 for p in positions),
            }
        except Exception as e:
            report['errors'].append(f'Positions: {e}')

        # 4. 熔断检查
        try:
            active_cbs = self.db.get_active_circuit_breakers()
            report['checks']['circuit_breakers'] = {
                'active_count': len(active_cbs),
                'breakers': active_cbs,
            }
            if active_cbs:
                report['warnings'].append(f'{len(active_cbs)} active circuit breakers')
        except Exception as e:
            report['errors'].append(f'Circuit breaker: {e}')

        # 5. 数据新鲜度
        try:
            latest = self.db.get_latest_kline_date()
            report['checks']['data_freshness'] = {
                'latest_kline': latest,
                'stale': False,
            }
            if latest:
                days_behind = (now - datetime.strptime(latest, '%Y-%m-%d')).days
                if days_behind > 1:
                    report['checks']['data_freshness']['stale'] = True
                    report['warnings'].append(f'Kline data {days_behind} days behind')
        except Exception as e:
            report['errors'].append(f'Data freshness: {e}')

        # 确定总体状态
        if report['errors']:
            report['status'] = 'error'
        elif report['warnings']:
            report['status'] = 'warning'

        return report
