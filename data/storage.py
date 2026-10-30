"""
猎场系统 V7.0 — SQLite 数据存储层
管理日K线、信号、持仓、新闻、多账户等数据的持久化
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
    account_id  TEXT DEFAULT 'default',
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
    account_id  TEXT DEFAULT 'default',
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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT DEFAULT 'default',
    ts_code     TEXT NOT NULL,
    name        TEXT,
    volume      INTEGER,
    avg_cost    REAL,
    current_price REAL,
    market_value REAL,
    pnl         REAL,
    pnl_pct     REAL,
    hold_days   INTEGER DEFAULT 0,
    buy_date    TEXT,
    updated_at  TEXT,
    UNIQUE(account_id, ts_code)
);

-- 熔断事件表
CREATE TABLE IF NOT EXISTS circuit_breaker_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT DEFAULT 'default',
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
    account_id  TEXT DEFAULT 'default',
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
    account_id  TEXT DEFAULT 'default',
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

-- ============================================================
-- V7.0 新增表
-- ============================================================

-- 虚拟账户表
CREATE TABLE IF NOT EXISTS accounts (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    profile_type    TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    cash            REAL NOT NULL,
    total_value     REAL,
    daily_pnl       REAL DEFAULT 0,
    daily_pnl_pct   REAL DEFAULT 0,
    weekly_pnl      REAL DEFAULT 0,
    weekly_pnl_pct  REAL DEFAULT 0,
    total_pnl       REAL DEFAULT 0,
    total_pnl_pct   REAL DEFAULT 0,
    state           TEXT DEFAULT 'stopped',
    strategy_params TEXT,
    risk_params     TEXT,
    created_at      TEXT,
    updated_at      TEXT
);

-- 因子注册表
CREATE TABLE IF NOT EXISTS factor_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_name     TEXT NOT NULL,
    factor_type     TEXT,
    category        TEXT,
    expression      TEXT,
    ic_value        REAL,
    ir_value        REAL,
    sharpe          REAL,
    effectiveness   REAL DEFAULT 1.0,
    decay_rate      REAL DEFAULT 0.0,
    discovered_at   TEXT,
    last_evaluated  TEXT,
    status          TEXT DEFAULT 'active',
    weight          REAL DEFAULT 1.0,
    metadata_json   TEXT
);

-- 执行日志表
CREATE TABLE IF NOT EXISTS execution_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT NOT NULL,
    order_id        TEXT,
    ts_code         TEXT,
    direction       TEXT,
    strategy        TEXT,
    total_shares    INTEGER,
    filled_shares   INTEGER,
    slices_planned  INTEGER,
    slices_filled   INTEGER,
    avg_fill_price  REAL,
    vwap_price      REAL,
    slippage_bps    REAL,
    execution_cost  REAL,
    started_at      TEXT,
    completed_at    TEXT,
    status          TEXT,
    details_json    TEXT
);

CREATE TABLE IF NOT EXISTS scan_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_time       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    time_str        TEXT,
    pool_size       INTEGER DEFAULT 0,
    signals_found   INTEGER DEFAULT 0,
    signals_json    TEXT,
    decisions_json  TEXT,
    account_id      TEXT DEFAULT 'all'
);

-- 资产配置快照表
CREATE TABLE IF NOT EXISTS asset_allocation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT NOT NULL,
    snapshot_time   TEXT,
    equity_pct      REAL,
    etf_pct         REAL,
    convertible_bond_pct REAL,
    cash_pct        REAL,
    sector_exposure TEXT,
    concentration_hhi REAL,
    updated_at      TEXT
);

-- 复盘报告表
CREATE TABLE IF NOT EXISTS review_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT,
    date            TEXT,
    daily_pnl       REAL,
    daily_pnl_pct   REAL,
    signal_accuracy REAL,
    benchmark_return REAL,
    factor_performance TEXT,
    trades_summary  TEXT,
    llm_summary     TEXT,
    created_at      TEXT
);

-- 选股池缓存表
CREATE TABLE IF NOT EXISTS stock_pool (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_type   TEXT,
    trade_date      TEXT,
    ts_code         TEXT,
    score           REAL,
    rank            INTEGER,
    tier            TEXT,
    factors_json    TEXT,
    created_at      TEXT
);

-- account_id 列迁移 (添加到已有表)
CREATE TABLE IF NOT EXISTS _migration_log (
    version TEXT PRIMARY KEY,
    applied_at TEXT
);

-- 面板设置表（键值对持久化）
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT DEFAULT (datetime('now','localtime'))
);

-- 行情分析报告表（每日午盘/收盘各一份）
CREATE TABLE IF NOT EXISTS market_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type     TEXT,              -- noon / close
    report_date     TEXT,              -- YYYY-MM-DD
    period_label    TEXT,              -- 午盘 / 收盘
    market_summary  TEXT,              -- 行情摘要
    index_data      TEXT,              -- JSON: 指数涨跌
    breadth_data    TEXT,              -- JSON: 涨跌家数/涨停/跌停
    prediction      TEXT,              -- 行情预测
    buy_recommendations  TEXT,         -- JSON: 买入推荐列表
    sell_recommendations TEXT,         -- JSON: 卖出推荐列表
    llm_source      TEXT,              -- ollama/deepseek/rule-based
    market_regime   TEXT,              -- JSON: 牛熊市判断 {value,label,risk_level,recommendation}
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- 参数优化运行结果表（自进化闭环：遗传算法/贝叶斯调参持久化）
CREATE TABLE IF NOT EXISTS optimization_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    optimized_at    TEXT,              -- ISO 时间
    best_params     TEXT,              -- JSON: 最优参数
    best_fitness    REAL,              -- 最优适应度
    history         TEXT,              -- JSON: [generation, best_fitness, avg_fitness]
    created_at      TEXT DEFAULT (datetime('now','localtime'))
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
            # 迁移：为旧库补充 market_regime 列（幂等）
            cols = [r[1] for r in conn.execute("PRAGMA table_info(market_reports)").fetchall()]
            if "market_regime" not in cols:
                conn.execute("ALTER TABLE market_reports ADD COLUMN market_regime TEXT")
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

    def get_trading_dates(self, start_date: str = None, end_date: str = None) -> List[str]:
        """获取全市场去重的交易日列表（升序）"""
        sql = "SELECT DISTINCT trade_date FROM daily_kline"
        params: List = []
        conds: List[str] = []
        if start_date:
            conds.append("trade_date >= ?")
            params.append(start_date)
        if end_date:
            conds.append("trade_date <= ?")
            params.append(end_date)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY trade_date ASC"
        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [r["trade_date"] for r in rows]

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

    # ============================================================
    # V7.0 — 多账户管理
    # ============================================================

    def save_account(self, account: Dict):
        """保存/更新虚拟账户"""
        strat = account.get('strategy_params', {})
        risk = account.get('risk_params', {})
        sql = """INSERT OR REPLACE INTO accounts
            (id, name, profile_type, initial_capital, cash, total_value,
             daily_pnl, daily_pnl_pct, weekly_pnl, weekly_pnl_pct,
             total_pnl, total_pnl_pct, state, strategy_params, risk_params,
             created_at, updated_at)
            VALUES (:id, :name, :profile_type, :initial_capital, :cash, :total_value,
                    :daily_pnl, :daily_pnl_pct, :weekly_pnl, :weekly_pnl_pct,
                    :total_pnl, :total_pnl_pct, :state, :strategy_params, :risk_params,
                    :created_at, :updated_at)"""
        with self._get_conn() as conn:
            conn.execute(sql, {
                'id': account.get('id', ''),
                'name': account.get('name', ''),
                'profile_type': account.get('profile_type', ''),
                'initial_capital': account.get('initial_capital', 0),
                'cash': account.get('cash', 0),
                'total_value': account.get('total_value', 0),
                'daily_pnl': account.get('daily_pnl', 0),
                'daily_pnl_pct': account.get('daily_pnl_pct', 0),
                'weekly_pnl': account.get('weekly_pnl', 0),
                'weekly_pnl_pct': account.get('weekly_pnl_pct', 0),
                'total_pnl': account.get('total_pnl', 0),
                'total_pnl_pct': account.get('total_pnl_pct', 0),
                'state': account.get('state', 'stopped'),
                'strategy_params': json.dumps(strat, ensure_ascii=False) if isinstance(strat, dict) else str(strat),
                'risk_params': json.dumps(risk, ensure_ascii=False) if isinstance(risk, dict) else str(risk),
                'created_at': account.get('created_at', ''),
                'updated_at': account.get('updated_at', ''),
            })
            conn.commit()

    def get_account(self, account_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", [account_id]).fetchone()
            return dict(row) if row else None

    def list_accounts(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]

    def delete_account(self, account_id: str):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM accounts WHERE id = ?", [account_id])
            conn.commit()

    # ---- 持仓 (per-account) ----

    def get_all_positions(self, account_id: str = "default") -> List[Dict]:
        sql = ("SELECT p.*, COALESCE(NULLIF(p.name, p.ts_code), b.name, p.ts_code) AS stock_name "
               "FROM positions p LEFT JOIN stock_basic b ON b.ts_code = p.ts_code "
               "WHERE p.account_id = ?")
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, [account_id]).fetchall()]

    def get_position_count(self, account_id: str = "default") -> int:
        with self._get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM positions WHERE account_id = ?",
                              [account_id]).fetchone()
            return row["c"] if row else 0

    def update_position(self, ts_code: str, name: str, volume: int, avg_cost: float,
                        current_price: float, buy_date: str, account_id: str = "default"):
        market_value = volume * current_price
        pnl = (current_price - avg_cost) * volume
        pnl_pct = (current_price / avg_cost - 1) * 100 if avg_cost > 0 else 0
        sql = """INSERT OR REPLACE INTO positions
            (ts_code, account_id, name, volume, avg_cost, current_price, market_value, pnl, pnl_pct, buy_date, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))"""
        with self._get_conn() as conn:
            conn.execute(sql, [ts_code, account_id, name, volume, avg_cost,
                              current_price, market_value, pnl, pnl_pct, buy_date])
            conn.commit()

    def remove_position(self, ts_code: str, account_id: str = "default"):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM positions WHERE ts_code = ? AND account_id = ?",
                        [ts_code, account_id])
            conn.commit()

    # ---- 交易 (per-account) ----

    def save_trade(self, trade: Dict, account_id: str = "default") -> int:
        trade['account_id'] = account_id
        sql = """INSERT INTO trades (account_id, ts_code, name, direction, price, volume, amount, fee,
            trade_time, signal_id, decision_model, pnl, pnl_pct, notes)
            VALUES (:account_id, :ts_code, :name, :direction, :price, :volume, :amount, :fee,
                    :trade_time, :signal_id, :decision_model, :pnl, :pnl_pct, :notes)"""
        with self._get_conn() as conn:
            cur = conn.execute(sql, trade)
            conn.commit()
            return cur.lastrowid

    def get_trades(self, account_id: str = "default", start_date: str = None,
                   end_date: str = None) -> List[Dict]:
        sql = "SELECT * FROM trades WHERE account_id = ?"
        params = [account_id]
        if start_date:
            sql += " AND trade_time >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND trade_time <= ?"
            params.append(end_date)
        sql += " ORDER BY trade_time DESC"
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ---- 信号 (per-account) ----

    def save_signal(self, ts_code: str, trade_date: str, signal_type: str, score: float,
                    details: Dict = None, account_id: str = "default"):
        sql = """INSERT INTO signals (account_id, ts_code, trade_date, signal_type, score, details)
            VALUES (?, ?, ?, ?, ?, ?)"""
        with self._get_conn() as conn:
            conn.execute(sql, [account_id, ts_code, trade_date, signal_type, score,
                              json.dumps(details or {}, ensure_ascii=False)])
            conn.commit()

    def get_signals_by_date(self, trade_date: str, min_score: float = 0,
                            account_id: str = "default") -> List[Dict]:
        sql = "SELECT * FROM signals WHERE trade_date = ? AND account_id = ? AND score >= ? ORDER BY score DESC"
        with self._get_conn() as conn:
            rows = conn.execute(sql, [trade_date, account_id, min_score]).fetchall()
            return [dict(r) for r in rows]

    # ---- 熔断 (per-account) ----

    def log_circuit_breaker(self, level: int, name: str, trigger_reason: str, action: str,
                            account_id: str = "default"):
        sql = """INSERT INTO circuit_breaker_log (account_id, level, name, trigger_reason, action)
            VALUES (?, ?, ?, ?, ?)"""
        with self._get_conn() as conn:
            conn.execute(sql, [account_id, level, name, trigger_reason, action])
            conn.commit()

    def get_active_circuit_breakers(self, account_id: str = "default") -> List[Dict]:
        sql = "SELECT * FROM circuit_breaker_log WHERE account_id = ? AND resolved_at IS NULL ORDER BY level"
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, [account_id]).fetchall()]

    # ---- 组合快照 (per-account) ----

    def save_portfolio_snapshot(self, snapshot: Dict, account_id: str = "default"):
        snapshot['account_id'] = account_id
        sql = """INSERT INTO portfolio_snapshot
            (account_id, snapshot_time, total_value, cash, position_value, daily_pnl, daily_pnl_pct,
             weekly_pnl, weekly_pnl_pct, total_pnl, total_pnl_pct, position_count)
            VALUES (:account_id, datetime('now','localtime'), :total_value, :cash, :position_value,
                    :daily_pnl, :daily_pnl_pct, :weekly_pnl, :weekly_pnl_pct,
                    :total_pnl, :total_pnl_pct, :position_count)"""
        with self._get_conn() as conn:
            conn.execute(sql, snapshot)
            conn.commit()

    def get_portfolio_snapshots(self, account_id: str = "default",
                                 days: int = 30) -> List[Dict]:
        """获取历史组合快照，用于盈亏曲线图"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM portfolio_snapshot WHERE account_id=? "
                "AND snapshot_time >= datetime('now', 'localtime', ?) "
                "ORDER BY snapshot_time ASC",
                (account_id, f'-{days} days')
            ).fetchall()
        return [dict(r) for r in rows] if rows else []

    # ---- 盈亏曲线（三账户 + 总收益，可切换时间范围） ----

    def get_pnl_series(self, range: str = "month") -> Dict[str, Any]:
        """获取三账户 + 总收益的累计收益率曲线。

        range: day(当日,横轴为交易时间) / week / month / year
        返回: {range, labels:[...], series:{aggressive,conservative,growth_hunter,total}}
        值为累计收益率 %（相对各账户初始资金），总收益按合并市值/合并初始资金计算。
        """
        days_map = {"day": 1, "week": 7, "month": 30, "year": 365}
        days = days_map.get(range, 30)
        accounts = ["aggressive", "conservative", "growth_hunter"]

        caps: Dict[str, float] = {}
        with self._get_conn() as conn:
            for r in conn.execute("SELECT id, initial_capital FROM accounts").fetchall():
                caps[r["id"]] = float(r["initial_capital"] or 0)

        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT account_id, snapshot_time, total_value FROM portfolio_snapshot "
                "WHERE account_id IN ('aggressive','conservative','growth_hunter') "
                "AND snapshot_time >= datetime('now','localtime', ?) "
                "ORDER BY snapshot_time ASC",
                (f'-{days} days',)
            ).fetchall()

        labels: List[str] = []
        series: Dict[str, List] = {a: [] for a in accounts}
        series["total"] = []

        if not rows:
            return {"range": range, "labels": labels, "series": series}

        # 按时间桶聚合：当日用完整时间戳（横轴=交易时间），其余按日期聚合取当日最后值
        buckets: Dict[str, Dict[str, float]] = {}
        order: List[str] = []
        for r in rows:
            st = r["snapshot_time"]
            key = st if range == "day" else (st[:10] if st else "")
            if not key:
                continue
            if key not in buckets:
                buckets[key] = {}
                order.append(key)
            buckets[key][r["account_id"]] = float(r["total_value"] or 0)

        for key in order:
            labels.append(key)
            total_val = 0.0
            avail_cap = 0.0
            for a in accounts:
                v = buckets[key].get(a)
                cap = caps.get(a, 0.0)
                if v is not None and cap > 0:
                    series[a].append(round((v - cap) / cap * 100, 2))
                else:
                    series[a].append(None)
                if v is not None:
                    total_val += v
                    avail_cap += cap
            if avail_cap > 0:
                series["total"].append(round((total_val - avail_cap) / avail_cap * 100, 2))
            else:
                series["total"].append(None)

        return {"range": range, "labels": labels, "series": series}

    # ---- 行情分析报告 ----

    def save_market_report(self, report: Dict) -> int:
        """保存一份行情分析报告，返回自增 id。"""
        sql = """INSERT INTO market_reports
            (report_type, report_date, period_label, market_summary, index_data,
             breadth_data, prediction, buy_recommendations, sell_recommendations, llm_source, market_regime)
            VALUES (:report_type, :report_date, :period_label, :market_summary, :index_data,
                    :breadth_data, :prediction, :buy_recommendations, :sell_recommendations, :llm_source, :market_regime)"""
        with self._get_conn() as conn:
            cur = conn.execute(sql, {
                'report_type': report.get('report_type', 'close'),
                'report_date': report.get('report_date', ''),
                'period_label': report.get('period_label', ''),
                'market_summary': report.get('market_summary', ''),
                'index_data': json.dumps(report.get('index_data', {}), ensure_ascii=False),
                'breadth_data': json.dumps(report.get('breadth_data', {}), ensure_ascii=False),
                'prediction': report.get('prediction', ''),
                'buy_recommendations': json.dumps(report.get('buy_recommendations', []), ensure_ascii=False),
                'sell_recommendations': json.dumps(report.get('sell_recommendations', []), ensure_ascii=False),
                'llm_source': report.get('llm_source', 'rule-based'),
                'market_regime': json.dumps(report.get('market_regime', {}), ensure_ascii=False),
            })
            conn.commit()
            return cur.lastrowid

    def get_market_reports(self, limit: int = 10) -> List[Dict]:
        """获取最近的行情分析报告（最新在前，最多 limit 条）。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM market_reports ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("index_data", "breadth_data", "buy_recommendations", "sell_recommendations", "market_regime"):
                try:
                    d[k] = json.loads(d.get(k) or "{}")
                except Exception:
                    d[k] = {}
            out.append(d)
        return out

    def has_market_report(self, report_date: str, report_type: str) -> bool:
        """判断某日某时段（noon/close）是否已生成过行情分析报告（跨重启去重）。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM market_reports WHERE report_date=? AND report_type=? LIMIT 1",
                (report_date, report_type)).fetchone()
        return row is not None

    # ---- 参数优化运行（自进化闭环） ----

    def save_optimization_run(self, result: Dict) -> int:
        """持久化一次参数优化结果，返回自增 id。"""
        sql = """INSERT INTO optimization_runs
            (optimized_at, best_params, best_fitness, history)
            VALUES (:optimized_at, :best_params, :best_fitness, :history)"""
        with self._get_conn() as conn:
            cur = conn.execute(sql, {
                'optimized_at': result.get('optimized_at', ''),
                'best_params': json.dumps(result.get('best_params', {}), ensure_ascii=False),
                'best_fitness': result.get('best_fitness', 0.0),
                'history': json.dumps(result.get('history', []), ensure_ascii=False),
            })
            conn.commit()
            return cur.lastrowid

    def get_optimization_runs(self, limit: int = 10) -> List[Dict]:
        """获取最近的参数优化运行结果（最新在前）。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM optimization_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("best_params", "history"):
                try:
                    d[k] = json.loads(d.get(k) or ("[]" if k == "history" else "{}"))
                except Exception:
                    d[k] = [] if k == "history" else {}
            out.append(d)
        return out

    # ---- 最新信号 / 交易 (跨账户聚合, 用于总览) ----

    @staticmethod
    def _resolve_account_filter(account_id: str) -> str:
        """将 'default' / 空串 / None 视为『全部账户』，其余视为具体账户。"""
        return (account_id or "").strip()

    def get_recent_signals(self, limit: int = 10, account_id: str = None) -> List[Dict]:
        """获取最新信号（按插入顺序倒序，即最新在前），并附带股票名称。
        account_id 为空/默认时跨所有账户聚合；否则仅返回该账户。"""
        aid = self._resolve_account_filter(account_id)
        base = ("SELECT s.*, COALESCE(b.name, s.ts_code) AS stock_name "
                "FROM signals s LEFT JOIN stock_basic b ON b.ts_code = s.ts_code")
        if aid and aid != "default":
            sql = base + " WHERE s.account_id = ? ORDER BY s.id DESC LIMIT ?"
            params = [aid, limit]
        else:
            sql = base + " ORDER BY s.id DESC LIMIT ?"
            params = [limit]
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_recent_trades(self, limit: int = 10, account_id: str = None) -> List[Dict]:
        """获取最近交易（最新在前），并附带股票名称。
        account_id 为空/默认时跨所有账户聚合；否则仅返回该账户。"""
        aid = self._resolve_account_filter(account_id)
        base = ("SELECT t.*, COALESCE(NULLIF(t.name, t.ts_code), b.name, t.ts_code) AS stock_name "
                "FROM trades t LEFT JOIN stock_basic b ON b.ts_code = t.ts_code")
        if aid and aid != "default":
            sql = base + " WHERE t.account_id = ? ORDER BY t.id DESC LIMIT ?"
            params = [aid, limit]
        else:
            sql = base + " ORDER BY t.id DESC LIMIT ?"
            params = [limit]
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ---- 因子注册 ----

    def save_factor(self, factor: Dict) -> int:
        sql = """INSERT OR REPLACE INTO factor_registry
            (factor_name, factor_type, category, expression, ic_value, ir_value, sharpe,
             effectiveness, decay_rate, discovered_at, last_evaluated, status, weight, metadata_json)
            VALUES (:factor_name, :factor_type, :category, :expression, :ic_value, :ir_value, :sharpe,
                    :effectiveness, :decay_rate, :discovered_at, :last_evaluated, :status, :weight, :metadata_json)"""
        with self._get_conn() as conn:
            cur = conn.execute(sql, {
                'factor_name': factor.get('factor_name', ''),
                'factor_type': factor.get('factor_type', ''),
                'category': factor.get('category', ''),
                'expression': factor.get('expression', ''),
                'ic_value': factor.get('ic_value', 0),
                'ir_value': factor.get('ir_value', 0),
                'sharpe': factor.get('sharpe', 0),
                'effectiveness': factor.get('effectiveness', 1.0),
                'decay_rate': factor.get('decay_rate', 0),
                'discovered_at': factor.get('discovered_at', ''),
                'last_evaluated': factor.get('last_evaluated', ''),
                'status': factor.get('status', 'active'),
                'weight': factor.get('weight', 1.0),
                'metadata_json': json.dumps(factor.get('metadata_json', {}), ensure_ascii=False),
            })
            conn.commit()
            return cur.lastrowid

    def get_active_factors(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM factor_registry WHERE status = 'active' ORDER BY effectiveness DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_factor_by_name(self, name: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM factor_registry WHERE factor_name = ?",
                              [name]).fetchone()
            return dict(row) if row else None

    def update_factor_performance(self, name: str, ic: float, ir: float, effectiveness: float):
        with self._get_conn() as conn:
            conn.execute("""UPDATE factor_registry
                SET ic_value = ?, ir_value = ?, effectiveness = ?, last_evaluated = datetime('now','localtime')
                WHERE factor_name = ?""", [ic, ir, effectiveness, name])
            conn.commit()

    # ---- 复盘报告 ----

    def save_review_report(self, report: Dict):
        sql = """INSERT INTO review_reports
            (account_id, date, daily_pnl, daily_pnl_pct, signal_accuracy, benchmark_return,
             factor_performance, trades_summary, llm_summary, created_at)
            VALUES (:account_id, :date, :daily_pnl, :daily_pnl_pct, :signal_accuracy, :benchmark_return,
                    :factor_performance, :trades_summary, :llm_summary, datetime('now','localtime'))"""
        with self._get_conn() as conn:
            conn.execute(sql, {
                'account_id': report.get('account_id', ''),
                'date': report.get('date', ''),
                'daily_pnl': report.get('daily_pnl', 0),
                'daily_pnl_pct': report.get('daily_pnl_pct', 0),
                'signal_accuracy': report.get('signal_accuracy', 0),
                'benchmark_return': report.get('benchmark_return', 0),
                'factor_performance': json.dumps(report.get('factor_performance', {}), ensure_ascii=False),
                'trades_summary': json.dumps(report.get('trades_summary', []), ensure_ascii=False),
                'llm_summary': report.get('llm_summary', ''),
            })
            conn.commit()

    def get_review_reports(self, account_id: str = None, limit: int = 20) -> List[Dict]:
        if account_id:
            sql = "SELECT * FROM review_reports WHERE account_id = ? ORDER BY date DESC LIMIT ?"
            params = [account_id, limit]
        else:
            sql = "SELECT * FROM review_reports ORDER BY date DESC LIMIT ?"
            params = [limit]
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ---- 选股池 ----

    def save_stock_pool(self, pool: List[Dict], strategy_type: str, trade_date: str):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM stock_pool WHERE strategy_type = ? AND trade_date = ?",
                        [strategy_type, trade_date])
            for item in pool:
                conn.execute("""INSERT INTO stock_pool
                    (strategy_type, trade_date, ts_code, score, rank, tier, factors_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))""",
                    [strategy_type, trade_date, item.get('ts_code', ''),
                     item.get('score', 0), item.get('rank', 0), item.get('tier', ''),
                     json.dumps(item.get('factors', {}), ensure_ascii=False)])
            conn.commit()

    def get_stock_pool(self, strategy_type: str, trade_date: str = None) -> List[Dict]:
        base = ("SELECT sp.*, COALESCE(b.name, sp.ts_code) AS stock_name "
                "FROM stock_pool sp LEFT JOIN stock_basic b ON b.ts_code = sp.ts_code ")
        if trade_date:
            sql = base + "WHERE sp.strategy_type = ? AND sp.trade_date = ? ORDER BY sp.rank"
            params = [strategy_type, trade_date]
        else:
            sql = base + "WHERE sp.strategy_type = ? ORDER BY sp.trade_date DESC, sp.rank"
            params = [strategy_type]
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ---- 执行日志 ----

    def save_execution_log(self, entry: Dict):
        sql = """INSERT INTO execution_log
            (account_id, order_id, ts_code, direction, strategy, total_shares, filled_shares,
             slices_planned, slices_filled, avg_fill_price, vwap_price, slippage_bps,
             execution_cost, started_at, completed_at, status, details_json)
            VALUES (:account_id, :order_id, :ts_code, :direction, :strategy, :total_shares, :filled_shares,
                    :slices_planned, :slices_filled, :avg_fill_price, :vwap_price, :slippage_bps,
                    :execution_cost, :started_at, :completed_at, :status, :details_json)"""
        with self._get_conn() as conn:
            conn.execute(sql, entry)
            conn.commit()

    def get_execution_logs(self, account_id: str = None, limit: int = 50) -> List[Dict]:
        base = ("SELECT el.*, COALESCE(b.name, el.ts_code) AS stock_name "
                "FROM execution_log el LEFT JOIN stock_basic b ON b.ts_code = el.ts_code ")
        if account_id:
            sql = base + "WHERE el.account_id = ? ORDER BY el.started_at DESC LIMIT ?"
            params = [account_id, limit]
        else:
            sql = base + "ORDER BY el.started_at DESC LIMIT ?"
            params = [limit]
        with self._get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ---- 扫描日志 ----

    def save_scan_log(self, entry: Dict):
        """持久化每轮扫描结果"""
        import json as _json
        signals = entry.get('signals', [])
        decisions = entry.get('account_decisions', [])
        sql = """INSERT INTO scan_log
            (scan_time, time_str, pool_size, signals_found, signals_json, decisions_json, account_id)
            VALUES (datetime('now','localtime'), :time_str, :pool_size, :signals_found,
                    :signals_json, :decisions_json, :account_id)"""
        with self._get_conn() as conn:
            conn.execute(sql, {
                'time_str': entry.get('time_str', ''),
                'pool_size': entry.get('pool_size', 0),
                'signals_found': entry.get('signals_found', 0),
                'signals_json': _json.dumps(signals, ensure_ascii=False),
                'decisions_json': _json.dumps(decisions, ensure_ascii=False),
                'account_id': 'all',
            })
            conn.commit()

    def get_scan_logs(self, limit: int = 20) -> List[Dict]:
        """读取最近的扫描日志"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scan_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        import json as _json
        for r in rows:
            d = dict(r)
            d['signals'] = _json.loads(d.get('signals_json', '[]'))
            d['account_decisions'] = _json.loads(d.get('decisions_json', '[]'))
            d['time'] = d.get('scan_time', '')
            result.append(d)
        return result

    # ---- 面板设置 ----

    def get_settings(self) -> Dict[str, Any]:
        """读取所有面板设置（键值对）"""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_setting(self, key: str, value: Any):
        """写入/更新单个面板设置"""
        import json as _json
        if not isinstance(value, str):
            value = _json.dumps(value, ensure_ascii=False)
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO app_settings (key, value, updated_at)
                   VALUES (?, ?, datetime('now','localtime'))
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                       updated_at=datetime('now','localtime')""",
                (key, value),
            )
            conn.commit()

    def set_settings(self, settings: Dict[str, Any]):
        """批量写入面板设置"""
        for k, v in (settings or {}).items():
            self.set_setting(k, v)

    # ---- 资产配置 ----

    def save_asset_allocation(self, entry: Dict):
        sql = """INSERT INTO asset_allocation
            (account_id, snapshot_time, equity_pct, etf_pct, convertible_bond_pct,
             cash_pct, sector_exposure, concentration_hhi, updated_at)
            VALUES (:account_id, datetime('now','localtime'), :equity_pct, :etf_pct,
                    :convertible_bond_pct, :cash_pct, :sector_exposure, :concentration_hhi,
                    datetime('now','localtime'))"""
        with self._get_conn() as conn:
            conn.execute(sql, entry)
            conn.commit()

    def get_latest_allocation(self, account_id: str) -> Optional[Dict]:
        sql = "SELECT * FROM asset_allocation WHERE account_id = ? ORDER BY snapshot_time DESC LIMIT 1"
        with self._get_conn() as conn:
            row = conn.execute(sql, [account_id]).fetchone()
            return dict(row) if row else None

    # ---- 数据库迁移 ----

    def apply_migration(self, version: str):
        """执行数据库迁移"""
        # 检查是否已执行
        with self._get_conn() as conn:
            row = conn.execute("SELECT version FROM _migration_log WHERE version = ?",
                              [version]).fetchone()
            if row:
                return  # 已执行

        if version == 'v7.0_add_account_id':
            self._migrate_v7_account_id()
        elif version == 'v7.0_add_new_tables':
            self._migrate_v7_new_tables()

        with self._get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO _migration_log (version, applied_at) VALUES (?, datetime('now','localtime'))",
                        [version])
            conn.commit()
            logger.info(f"Migration {version} applied")

    def _migrate_v7_account_id(self):
        """添加 account_id 列到已有表"""
        alter_tables = ['positions', 'trades', 'signals', 'portfolio_snapshot',
                       'circuit_breaker_log', 'strategy_lifecycle']
        with self._get_conn() as conn:
            for table in alter_tables:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id TEXT DEFAULT 'default'")
                except sqlite3.OperationalError:
                    pass  # 列已存在
            conn.commit()

    def _migrate_v7_new_tables(self):
        """创建 V7.0 新表 (已通过 _init_schema 处理)"""
        pass  # 新表在 _init_schema 中创建
