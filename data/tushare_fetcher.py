"""
猎场系统 V6.0 — Tushare Pro 数据源
需要 Tushare Pro token（环境变量 TUSHARE_TOKEN 或配置文件）
"""

import os
import logging
from typing import Optional, List, Dict
from datetime import datetime, timedelta
import time

logger = logging.getLogger(__name__)


class TushareFetcher:
    """Tushare Pro 数据获取器"""

    def __init__(self, token: str = "", database=None):
        self.db = database
        self._token = token or os.getenv("TUSHARE_TOKEN", "")
        self._pro = None
        self._rate_limit = 200  # 每分钟请求上限
        self._request_times = []

        if self._token:
            self._init_client()

    def _init_client(self):
        try:
            import tushare as ts
            ts.set_token(self._token)
            self._pro = ts.pro_api()
            logger.info("Tushare Pro client initialized")
        except ImportError:
            logger.error("tushare not installed. Run: pip install tushare")
        except Exception as e:
            logger.error(f"Tushare init failed: {e}")

    def _rate_limit_check(self):
        """简单的速率限制"""
        now = time.time()
        self._request_times = [t for t in self._request_times if now - t < 60]
        if len(self._request_times) >= self._rate_limit:
            sleep_time = 60 - (now - self._request_times[0]) + 0.5
            time.sleep(max(0, sleep_time))
        self._request_times.append(time.time())

    async def sync_stock_basic(self):
        """同步全市场股票基本信息"""
        if not self._pro:
            logger.warning("Tushare client not available")
            return

        self._rate_limit_check()
        try:
            # 获取当前上市的A股
            data = self._pro.stock_basic(
                exchange='',
                list_status='L',
                fields='ts_code,name,industry,market,list_date'
            )
            if data is None or data.empty:
                logger.warning("No stock basic data returned")
                return

            # 获取市值数据
            self._rate_limit_check()
            daily_basic = self._pro.daily_basic(
                trade_date=datetime.now().strftime('%Y%m%d'),
                fields='ts_code,total_mv'
            )

            stocks = []
            for _, row in data.iterrows():
                mv_row = daily_basic[daily_basic['ts_code'] == row['ts_code']] if daily_basic is not None else None
                market_cap = float(mv_row.iloc[0]['total_mv']) / 10000 if mv_row is not None and not mv_row.empty else 0

                stocks.append({
                    'ts_code': row['ts_code'],
                    'name': row['name'],
                    'industry': row.get('industry', ''),
                    'market_cap': market_cap,
                    'list_date': row.get('list_date', ''),
                    'is_st': 1 if 'ST' in row['name'] else 0,
                    'updated_at': datetime.now().isoformat(),
                })

            if stocks:
                self.db.upsert_stock_basic(stocks)
                logger.info(f"Synced {len(stocks)} stock basic records")
        except Exception as e:
            logger.error(f"Stock basic sync error: {e}")

    async def sync_daily_kline(self, ts_codes: List[str] = None) -> int:
        """增量同步日K线"""
        if not self._pro:
            logger.warning("Tushare client not available")
            return 0

        total = 0
        latest_date = self.db.get_latest_kline_date()

        # 确定起始日期
        if latest_date:
            start_date = (datetime.strptime(latest_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y%m%d')
        else:
            start_date = '20200101'  # 首次加载3年数据

        end_date = datetime.now().strftime('%Y%m%d')
        if start_date >= end_date:
            logger.debug(f"Kline up to date: {latest_date}")
            return 0

        # 如果没有指定股票列表，获取全市场
        if ts_codes is None:
            ts_codes = self.db.get_all_codes()
            if not ts_codes:
                # 先同步股票列表
                await self.sync_stock_basic()
                stocks = self.db.get_stocks_by_market_cap(0, 10000)
                ts_codes = [s['ts_code'] for s in stocks]

        batch_size = 100
        for i in range(0, len(ts_codes), batch_size):
            batch = ts_codes[i:i + batch_size]
            self._rate_limit_check()

            try:
                codes_str = ','.join(batch)
                data = self._pro.daily(
                    ts_code=codes_str,
                    start_date=start_date,
                    end_date=end_date,
                    fields='ts_code,trade_date,open,high,low,close,vol,amount,turnover_rate,pct_chg'
                )
                if data is None or data.empty:
                    continue

                rows = []
                for _, row in data.iterrows():
                    rows.append({
                        'ts_code': row['ts_code'],
                        'trade_date': datetime.strptime(row['trade_date'], '%Y%m%d').strftime('%Y-%m-%d'),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'volume': float(row['vol']),
                        'amount': float(row['amount']),
                        'turnover_rate': float(row.get('turnover_rate', 0) or 0),
                        'volume_ratio': 1.0,  # 后续计算
                        'rsi_14': 50.0,        # 后续 calc_indicators
                        'change_pct': float(row.get('pct_chg', 0) or 0),
                    })

                count = self.db.upsert_kline(rows)
                total += count
                logger.debug(f"Batch {i // batch_size + 1}: {len(rows)} rows synced")

            except Exception as e:
                logger.error(f"Kline sync error for batch {i // batch_size + 1}: {e}")

        logger.info(f"Kline sync completed: {total} rows total")
        return total

    async def get_daily_basic(self, trade_date: str = None):
        """获取每日指标（市值、换手率、量比等）"""
        if not self._pro:
            return None

        if trade_date is None:
            trade_date = datetime.now().strftime('%Y%m%d')

        self._rate_limit_check()
        try:
            return self._pro.daily_basic(
                trade_date=trade_date,
                fields='ts_code,turnover_rate,volume_ratio,pe,pe_ttm,pb,total_mv,circ_mv'
            )
        except Exception as e:
            logger.error(f"Daily basic fetch error: {e}")
            return None

    async def calculate_indicators_batch(self, ts_codes: List[str]):
        """批量计算技术指标（RSI/MACD/布林/均线/回撤）- 收盘后调用"""
        import numpy as np

        for ts_code in ts_codes:
            kline = self.db.get_kline(ts_code, limit=120)
            if len(kline) < 20:
                continue

            closes = np.array([r['close'] for r in kline])

            # RSI(14)
            rsi = self._calc_rsi(closes, 14)

            # MACD
            dif, dea, hist = self._calc_macd(closes)

            # 布林带
            boll_upper, boll_mid, boll_lower = self._calc_bollinger(closes, 20, 2)

            # 均线
            ma5 = np.convolve(closes, np.ones(5) / 5, mode='valid')
            ma20 = np.convolve(closes, np.ones(20) / 20, mode='valid')
            ma60 = np.convolve(closes, np.ones(60) / 60, mode='valid') if len(closes) >= 60 else np.array([])

            # 60日最大回撤
            max_dd = self._calc_max_drawdown(closes, 60)

            # 更新数据库
            with self.db._get_conn() as conn:
                for i in range(len(kline)):
                    trade_date = kline[i]['trade_date']
                    conn.execute("""UPDATE daily_kline SET
                        rsi_14 = ?, macd_dif = ?, macd_dea = ?, macd_hist = ?,
                        boll_upper = ?, boll_mid = ?, boll_lower = ?,
                        ma_5 = ?, ma_20 = ?, ma_60 = ?, max_drawdown_60 = ?
                        WHERE ts_code = ? AND trade_date = ?""", [
                        rsi[i] if i < len(rsi) else None,
                        dif[i] if i < len(dif) else None,
                        dea[i] if i < len(dea) else None,
                        hist[i] if i < len(hist) else None,
                        boll_upper[i] if i < len(boll_upper) else None,
                        boll_mid[i] if i < len(boll_mid) else None,
                        boll_lower[i] if i < len(boll_lower) else None,
                        ma5[i - 4] if i >= 4 and i - 4 < len(ma5) else None,
                        ma20[i - 19] if i >= 19 and i - 19 < len(ma20) else None,
                        ma60[i - 59] if i >= 59 and i - 59 < len(ma60) else None,
                        max_dd[i] if i < len(max_dd) else None,
                        ts_code, trade_date
                    ])
                conn.commit()

    def _calc_rsi(self, prices, period=14):
        import numpy as np
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        rsi = np.zeros(len(prices))
        rsi[:] = np.nan
        if len(prices) > period:
            avg_gain = np.mean(gains[:period])
            avg_loss = np.mean(losses[:period])
            for i in range(period, len(prices)):
                avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
                avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
                rs = avg_gain / avg_loss if avg_loss > 0 else 100
                rsi[i] = 100 - (100 / (1 + rs))
        return rsi

    def _calc_macd(self, prices, fast=12, slow=26, signal=9):
        import numpy as np
        ema_fast = np.zeros(len(prices))
        ema_slow = np.zeros(len(prices))
        ema_fast[:] = np.nan
        ema_slow[:] = np.nan

        if len(prices) > slow:
            ema_fast[fast - 1] = np.mean(prices[:fast])
            ema_slow[slow - 1] = np.mean(prices[:slow])
            kf = 2 / (fast + 1)
            ks = 2 / (slow + 1)
            for i in range(fast, len(prices)):
                ema_fast[i] = prices[i] * kf + ema_fast[i - 1] * (1 - kf)
            for i in range(slow, len(prices)):
                ema_slow[i] = prices[i] * ks + ema_slow[i - 1] * (1 - ks)

        dif = ema_fast - ema_slow
        dea = np.zeros(len(prices))
        dea[:] = np.nan
        if len(prices) > slow + signal:
            dea[slow + signal - 1] = np.nanmean(dif[slow:slow + signal])
            kd = 2 / (signal + 1)
            for i in range(slow + signal, len(prices)):
                dea[i] = dif[i] * kd + dea[i - 1] * (1 - kd)

        hist = 2 * (dif - dea)
        return dif, dea, hist

    def _calc_bollinger(self, prices, period=20, std_dev=2):
        import numpy as np
        mid = np.zeros(len(prices))
        upper = np.zeros(len(prices))
        lower = np.zeros(len(prices))
        mid[:] = np.nan
        upper[:] = np.nan
        lower[:] = np.nan

        for i in range(period - 1, len(prices)):
            window = prices[i - period + 1:i + 1]
            mid[i] = np.mean(window)
            std = np.std(window)
            upper[i] = mid[i] + std_dev * std
            lower[i] = mid[i] - std_dev * std
        return upper, mid, lower

    def _calc_max_drawdown(self, prices, period=60):
        import numpy as np
        max_dd = np.zeros(len(prices))
        max_dd[:] = np.nan
        for i in range(1, len(prices)):
            window = prices[max(0, i - period + 1):i + 1]
            peak = np.max(window)
            dd = (peak - prices[i]) / peak
            max_dd[i] = dd
        return max_dd
