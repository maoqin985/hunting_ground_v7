"""
猎场系统 V6.0 — AkShare 免费数据源（备选）
支持 A股/港股/可转债 全品种数据
"""

import logging
from typing import Optional, List, Dict
from datetime import datetime, timedelta
import asyncio

logger = logging.getLogger(__name__)


class AkShareFetcher:
    """AkShare 免费数据获取器（Tushare 不可用时的备选）"""

    def __init__(self, database):
        self.db = database
        self._available = False
        try:
            import akshare as ak
            self.ak = ak
            self._available = True
            logger.info("AkShare client initialized (free mode)")
        except ImportError:
            logger.warning("akshare not installed. Run: pip install akshare")
        except Exception as e:
            logger.error(f"AkShare init failed: {e}")

    @property
    def available(self) -> bool:
        return self._available

    async def sync_stock_basic(self):
        """同步 A 股股票基本信息"""
        if not self._available:
            return

        try:
            # AkShare 获取A股列表
            stock_info = self.ak.stock_info_a_code_name()
            stocks = []
            for _, row in stock_info.iterrows():
                stocks.append({
                    'ts_code': row['code'],
                    'name': row['name'],
                    'industry': '',
                    'market_cap': 0,
                    'list_date': '',
                    'is_st': 1 if 'ST' in str(row['name']) else 0,
                    'updated_at': datetime.now().isoformat(),
                })

            if stocks:
                self.db.upsert_stock_basic(stocks)
                logger.info(f"AkShare: synced {len(stocks)} stock basics")
        except Exception as e:
            logger.error(f"AkShare stock basic sync error: {e}")

    async def sync_daily_kline(self, ts_codes: List[str] = None) -> int:
        """同步日K线"""
        if not self._available:
            return 0

        if ts_codes is None:
            ts_codes = self.db.get_all_codes()

        total = 0
        for ts_code in ts_codes[:50]:  # 限制免费API调用量
            try:
                # AkShare 日K线接口
                if ts_code.startswith('6'):
                    symbol = f"sh{ts_code}"
                else:
                    symbol = f"sz{ts_code}"

                df = self.ak.stock_zh_a_hist(
                    symbol=ts_code,
                    period="daily",
                    start_date="20240101",
                    end_date=datetime.now().strftime('%Y%m%d'),
                    adjust="qfq"
                )

                if df is None or df.empty:
                    continue

                rows = []
                for _, row in df.iterrows():
                    rows.append({
                        'ts_code': ts_code,
                        'trade_date': str(row.get('日期', ''))[:10],
                        'open': float(row.get('开盘', 0)),
                        'high': float(row.get('最高', 0)),
                        'low': float(row.get('最低', 0)),
                        'close': float(row.get('收盘', 0)),
                        'volume': float(row.get('成交量', 0)),
                        'amount': float(row.get('成交额', 0)),
                        'turnover_rate': float(row.get('换手率', 0) or 0),
                        'volume_ratio': 1.0,
                        'rsi_14': 50.0,
                        'change_pct': float(row.get('涨跌幅', 0) or 0),
                    })

                count = self.db.upsert_kline(rows)
                total += count

            except Exception as e:
                logger.debug(f"AkShare kline error for {ts_code}: {e}")
                continue

            await asyncio.sleep(0.3)  # 控制频率

        logger.info(f"AkShare: synced {total} kline rows")
        return total

    async def get_realtime_quotes(self, ts_codes: List[str]) -> Dict[str, Dict]:
        """获取实时行情（批量化）"""
        if not self._available:
            return {}

        result = {}
        try:
            df = self.ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                for ts_code in ts_codes:
                    row = df[df['代码'] == ts_code]
                    if not row.empty:
                        r = row.iloc[0]
                        result[ts_code] = {
                            'price': float(r.get('最新价', 0)),
                            'change_pct': float(r.get('涨跌幅', 0)),
                            'volume': float(r.get('成交量', 0)),
                            'amount': float(r.get('成交额', 0)),
                            'turnover_rate': float(r.get('换手率', 0) or 0),
                            'volume_ratio': float(r.get('量比', 0) or 1.0),
                            'high': float(r.get('最高', 0)),
                            'low': float(r.get('最低', 0)),
                            'open': float(r.get('今开', 0)),
                        }
        except Exception as e:
            logger.error(f"AkShare realtime error: {e}")

        return result

    async def get_convertible_bonds(self) -> List[Dict]:
        """获取可转债列表（含双低指标）"""
        if not self._available:
            return []

        bonds = []
        try:
            df = self.ak.bond_cb_jsl()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    price = float(row.get('price', 0))
                    premium = float(row.get('premium_rt', 100))
                    double_low = price + premium if premium < 100 else price + premium

                    bonds.append({
                        'ts_code': str(row.get('bond_id', '')),
                        'name': str(row.get('bond_nm', '')),
                        'price': price,
                        'premium_rt': premium,
                        'double_low': double_low,
                        'stock_code': str(row.get('stock_id', '')),
                        'remain_size': float(row.get('remain_size', 0)),
                        'rating': str(row.get('rating_cd', '')),
                    })
        except Exception as e:
            logger.error(f"Convertible bond fetch error: {e}")

        return bonds

    async def get_hk_stocks(self) -> List[Dict]:
        """获取港股列表"""
        if not self._available:
            return []

        try:
            df = self.ak.stock_hk_spot_em()
            if df is not None and not df.empty:
                return [
                    {
                        'ts_code': str(row.get('代码', '')),
                        'name': str(row.get('名称', '')),
                        'price': float(row.get('最新价', 0)),
                        'change_pct': float(row.get('涨跌幅', 0)),
                        'volume': float(row.get('成交量', 0)),
                        'amount': float(row.get('成交额', 0)),
                    }
                    for _, row in df.iterrows()
                ]
        except Exception as e:
            logger.error(f"HK stock fetch error: {e}")
        return []
