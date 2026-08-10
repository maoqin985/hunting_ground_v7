"""
猎场系统 V6.0 — SQLite 数据存储层
管理日K线、信号、持仓、新闻等数据的持久化
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from contextlib import contextmanager

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
-- 日K线表
CREATE TABLE IF NOT EXISTS daily_kline (
    ts_code     TEXT NOT NULL,      -- 股票代码
    trade_date  TEXT NOT NULL,      -- 交易日期 YYYY-MM-DD
    open        REAL,               -- 开盘价
    high        REAL,               -- 最高价
    low         REAL,               -- 最低价
    close       REAL,               -- 收盘价
    volume      REAL,               -- 成交量（手）
    amount      REAL,               -- 成交额（千元）
    turnover_rate REAL,             -- 换手率 %
    volume_ratio  REAL,             -- 量比
    rsi_14      REAL,               -- RSI(14)
    macd_dif    REAL,               -- MACD DIF
    macd_dea    REAL,               -- MACD DEA
    macd_hist   REAL,               -- MACD 柱
    boll_upper  REAL,               -- 布林上轨
    boll_mid    REAL,               -- 布林中轨
    boll_lower  REAL,               -- 布林下轨
    ma_5        REAL,               -- 5日均线
    ma_20       REAL,               -- 20日均线
    ma_60       REAL,               -- 60日均线
    max_drawdown_60 REAL,           -- 60日最大回撤
    change_pct  REAL,               -- 涨跌幅 %
    PRIMARY KEY (ts_code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_kline_code ON daily_kline(ts_code);
CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(trade_date);

-- 股票基本信息表
CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code     TEXT PRIMARY KEY,
    name        TEXT,
    industry    TEXT,
    market_cap  REAL,               -- 总市值（亿）
    list_date   TEXT,
    is_st       INTEGER DEFAULT 0,  -- 是否ST
    updated_at  TEXT
);

-- 信号记录表
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code     TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    signal_type TEXT,               -- technical / fund_flow / sentiment / ai
    score       REAL,               -- 信号评分
    details     TEXT,               -- JSON详情
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(trade_date);

-- 交易记录表
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code     TEXT NOT NULL,
    name        TEXT,
    direction   TEXT,               -- buy / sell
    price       REAL,
    volume      INTEGER,            -- 股数
    amount      REAL,               -- 金额
    fee         REAL,               -- 手续费
    trade_time  TEXT,
    signal_id   INTEGER,
    decision_model TEXT,            -- 决策来源模型
    pnl         REAL,               -- 平仓盈亏
    pnl_pct     REAL,               -- 盈亏百分比
    notes       TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

-- 持仓状态表
CREATE TABLE IF NOT EXISTS positions (
    ts_code     TEXT PRIMARY KEY,
    name        TEXT,
    volume      INTEGER,
    avg_cost    REAL,
    current_price REAL,
    market_value REAL,
    pnl         REAL,
    pnl_pct     REAL,
    hold_days   INTEGER DEFAULT 0,
    buy_date    TEXT,
    updated_at  TEXT
);

-- 熔断事件表
CREATE TABLE IF NOT EXISTS circuit_breaker_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       INTEGER NOT NULL,
    name        TEXT,
    trigger_reason TEXT,
    action      TEXT,
    triggered_at TEXT DEFAULT (datetime('now','localtime')),
    resolved_at TEXT
);

-- 策略进化表
CREATE TABLE IF NOT EXISTS strategy_lifecycle (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    period      TEXT,               -- daily / weekly / monthly
    period_start TEXT,
    period_end  TEXT,
    sharpe_ratio REAL,
    win_rate    REAL,
    max_drawdown REAL,
    total_return REAL,
    annual_return REAL,
    trade_count INTEGER DEFAULT 0,
    avg_hold_days REAL,
    parameters  TEXT,               -- JSON
    status      TEXT,               -- active / observing / degraded / retired
    created_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 新闻情绪表
CREATE TABLE IF NOT EXISTS news_sentiment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code     TEXT,
    title       TEXT,
    content     TEXT,
    url         TEXT,
    source      TEXT,
    sentiment   REAL,               -- -1.0 到 1.0
    is_negative INTEGER DEFAULT 0,
    keywords    TEXT,
    pub_time    TEXT,
    fetched_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 持仓状态快照
CREATE TABLE IF NOT EXISTS portfolio_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_time TEXT,
    total_value REAL,
    cash        REAL,
    position_value REAL,
    daily_pnl   REAL,
    daily_pnl_pct REAL,
    weekly_pnl  REAL,
    weekly_pnl_pct REAL,
    total_pnl   REAL,
    total_pnl_pct REAL,
    position_count INTEGER
);
"""


class Database:
    """SQLite 数据库 DAO"""

    def __init__(self, db_path: str = "data/hunting_ground.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.info(f"Database initialized: {self.db_path}")

    def _init_schema(self):
        with self._get_conn() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ---- 日K线操作 ----

    def upsert_kline(self, rows: List[Dict]) -> int:
        """批量插入/更新日K线"""
        sql = """INSERT OR REPLACE INTO daily_kline
            (ts_code, trade_date, open, high, low, close, volume, amount,
             turnover_rate, volume_ratio, rsi_14, change_pct)
            VALUES (:ts_code, :trade_date, :open, :high, :low, :close, :volume, :amount,
                    :turnover_rate, :volume_ratio, :rsi_14, :change_pct)"""
        with self._get_conn() as conn:
            conn.executemany(sql, rows)
            conn.commit()
            return len(rows)

    def get_kline(self, ts_code: str, start_date: str = None, end_date: str = None, limit: int = 500) -> List[Dict]:
        """查询日K线"""
        sql = "SELECT * FROM daily_kline WHERE ts_code = ?"
        params = [ts_code]
        if start_date:
            sql += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND trade_date <= ?"
            params.append(end_date)
        sql += " ORDER BY trade_date DESC LIMIT ?"
        params.append(limit)
        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_latest_kline_date(self) -> Optional[str]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT MAX(trade_date) as d FROM daily_kline").fetchone()
            return row["d"] if row else None

    def get_all_codes(self) -> List[str]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT DISTINCT ts_code FROM daily_kline").fetchall()
            return [r["ts_code"] for r in rows]

    # ---- 股票基本信息 ----

    def upsert_stock_basic(self, stocks: List[Dict]):
        sql = """INSERT OR REPLACE INTO stock_basic
            (ts_code, name, industry, market_cap, list_date, is_st, updated_at)
            VALUES (:ts_code, :name, :industry, :market_cap, :list_date, :is_st, :updated_at)"""
        with self._get_conn() as conn:
            conn.executemany(sql, stocks)
            conn.commit()

    def get_stock_info(self, ts_code: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM stock_basic WHERE ts_code = ?", [ts_code]).fetchone()
            return dict(row) if row else None

    def get_stocks_by_market_cap(self, min_cap: float, max_cap: float) -> List[Dict]:
        sql = "SELECT * FROM stock_basic WHERE market_cap BETWEEN ? AND ? AND is_st = 0"
        with self._get_conn() as conn:
            rows = conn.execute(sql, [min_cap, max_cap]).fetchall()
            return [dict(r) for r in rows]

    # ---- 信号记录 ----

    def save_signal(self, ts_code: str, trade_date: str, signal_type: str, score: float, details: Dict = None):
        sql = """INSERT INTO signals (ts_code, trade_date, signal_type, score, details)
            VALUES (?, ?, ?, ?, ?)"""
        with self._get_conn() as conn:
            conn.execute(sql, [ts_code, trade_date, signal_type, score, json.dumps(details or {})])
            conn.commit()

    def get_signals_by_date(self, trade_date: str, min_score: float = 0) -> List[Dict]:
        sql = "SELECT * FROM signals WHERE trade_date = ? AND score >= ? ORDER BY score DESC"
        with self._get_conn() as conn:
            rows = conn.execute(sql, [trade_date, min_score]).fetchall()
            return [dict(r) for r in rows]

    # ---- 交易记录 ----

    def save_trade(self, trade: Dict) -> int:
        sql = """INSERT INTO trades (ts_code, name, direction, price, volume, amount, fee,
            trade_time, signal_id, decision_model, pnl, pnl_pct, notes)
            VALUES (:ts_code, :name, :direction, :price, :volume, :amount, :fee,
                    :trade_time, :signal_id, :decision_model, :pnl, :pnl_pct, :notes)"""
        with self._get_conn() as conn:
            cur = conn.execute(sql, trade)
            conn.commit()
            return cur.lastrowid

    def get_trades(self, start_date: str = None, end_date: str = None) -> List[Dict]:
        sql = "SELECT * FROM trades WHERE 1=1"
        params = []
        if start_date:
            sql += " AND trade_time >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND trade_time <= ?"
            params.append(end_date)
        sql += " ORDER BY trade_time DESC"
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ---- 持仓操作 ----

    def update_position(self, ts_code: str, name: str, volume: int, avg_cost: float,
                        current_price: float, buy_date: str):
        market_value = volume * current_price
        pnl = (current_price - avg_cost) * volume
        pnl_pct = (current_price / avg_cost - 1) * 100 if avg_cost > 0 else 0
        sql = """INSERT OR REPLACE INTO positions
            (ts_code, name, volume, avg_cost, current_price, market_value, pnl, pnl_pct, buy_date, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))"""
        with self._get_conn() as conn:
            conn.execute(sql, [ts_code, name, volume, avg_cost, current_price, market_value, pnl, pnl_pct, buy_date])
            conn.commit()

    def remove_position(self, ts_code: str):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM positions WHERE ts_code = ?", [ts_code])
            conn.commit()

    def get_all_positions(self) -> List[Dict]:
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()]

    def get_position_count(self) -> int:
        with self._get_conn() as conn:
            return conn.execute("SELECT COUNT(*) as c FROM positions").fetchone()["c"]

    # ---- 熔断日志 ----

    def log_circuit_breaker(self, level: int, name: str, trigger_reason: str, action: str):
        sql = """INSERT INTO circuit_breaker_log (level, name, trigger_reason, action)
            VALUES (?, ?, ?, ?)"""
        with self._get_conn() as conn:
            conn.execute(sql, [level, name, trigger_reason, action])
            conn.commit()

    def get_active_circuit_breakers(self) -> List[Dict]:
        sql = "SELECT * FROM circuit_breaker_log WHERE resolved_at IS NULL ORDER BY level"
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    def resolve_circuit_breaker(self, breaker_id: int):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE circuit_breaker_log SET resolved_at = datetime('now','localtime') WHERE id = ?",
                [breaker_id]
            )
            conn.commit()

    # ---- 策略进化 ----

    def save_lifecycle_record(self, record: Dict):
        record["parameters"] = json.dumps(record.get("parameters", {}))
        sql = """INSERT INTO strategy_lifecycle
            (strategy_name, period, period_start, period_end, sharpe_ratio, win_rate,
             max_drawdown, total_return, annual_return, trade_count, avg_hold_days, parameters, status)
            VALUES (:strategy_name, :period, :period_start, :period_end, :sharpe_ratio, :win_rate,
                    :max_drawdown, :total_return, :annual_return, :trade_count, :avg_hold_days, :parameters, :status)"""
        with self._get_conn() as conn:
            conn.execute(sql, record)
            conn.commit()

    # ---- 新闻情绪 ----

    def save_news_sentiment(self, news_items: List[Dict]):
        sql = """INSERT OR REPLACE INTO news_sentiment
            (ts_code, title, content, url, source, sentiment, is_negative, keywords, pub_time)
            VALUES (:ts_code, :title, :content, :url, :source, :sentiment, :is_negative, :keywords, :pub_time)"""
        with self._get_conn() as conn:
            conn.executemany(sql, news_items)
            conn.commit()

    def get_negative_stocks(self) -> List[str]:
        sql = """SELECT DISTINCT ts_code FROM news_sentiment
            WHERE is_negative = 1 AND fetched_at > datetime('now', '-7 days')"""
        with self._get_conn() as conn:
            return [r["ts_code"] for r in conn.execute(sql).fetchall()]

    # ---- 组合快照 ----

    def save_portfolio_snapshot(self, snapshot: Dict):
        sql = """INSERT INTO portfolio_snapshot
            (snapshot_time, total_value, cash, position_value, daily_pnl, daily_pnl_pct,
             weekly_pnl, weekly_pnl_pct, total_pnl, total_pnl_pct, position_count)
            VALUES (datetime('now','localtime'), :total_value, :cash, :position_value,
                    :daily_pnl, :daily_pnl_pct, :weekly_pnl, :weekly_pnl_pct,
                    :total_pnl, :total_pnl_pct, :position_count)"""
        with self._get_conn() as conn:
            conn.execute(sql, snapshot)
            conn.commit()

    def get_stats(self) -> Dict:
        with self._get_conn() as conn:
            kline_count = conn.execute("SELECT COUNT(*) as c FROM daily_kline").fetchone()["c"]
            stock_count = conn.execute("SELECT COUNT(DISTINCT ts_code) as c FROM daily_kline").fetchone()["c"]
            trade_count = conn.execute("SELECT COUNT(*) as c FROM trades").fetchone()["c"]
            return {
                "kline_rows": kline_count,
                "stocks_tracked": stock_count,
                "total_trades": trade_count,
                "latest_kline_date": self.get_latest_kline_date(),
            }
