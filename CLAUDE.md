# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common Commands

```bash
# Install core dependencies
pip install -r requirements.txt

# Run paper trading simulation
python main.py --mode paper

# Start web dashboard (http://localhost:8501)
python main.py --mode web

# Run backtest
python main.py --mode backtest --start 2023-01-01 --end 2025-12-31

# Run tests
pytest tests/ -v
python -m pytest tests/ -v

# Single test
pytest tests/test_agent.py -v
```

## Architecture

**猎场系统 V6.0 "Genesis"** is an async event-driven quantitative trading system for A-share small-cap stocks (≤50B market cap, 200万 capital).

### Core Data Flow

```
DataGateway (multi-source) → Signal Scanners → AI Ensemble (score/risk/size) → Decision Validator → Broker API
                                      ↓
                              EventBus (pub/sub backbone)
                                      ↓
                   Risk Circuit Breakers ←→ Evolution Lifecycle (daily/weekly/monthly)
```

### Key Modules

- **`core/`** — `Engine` manages subsystem lifecycle (start/pause/resume/stop). `EventBus` is the central pub/sub backbone using `EventType` enums and async handlers with wildcard pattern matching. `Scheduler` runs registered async tasks on intervals, respecting trading hours (9:30–11:30, 13:00–15:00) and priority levels.
- **`data/`** — `DataGateway` is the unified facade over multiple sources: Tushare Pro (primary), AkShare (fallback), Tencent WebSocket (realtime), Eastmoney NLP (news). `Database` is a SQLite DAO (`data/hunting_ground.db`) with schema for kline, signals, trades, positions, circuit breaker log, strategy lifecycle, news sentiment, and portfolio snapshots. All fetchers (`tushare_fetcher.py`, `akshare_fetcher.py`, `websocket_realtime.py`, `news_nlp.py`) follow the same interface pattern.
- **`signals/`** — Scanners produce scores: `technical.py` (RSI/MACD/Bollinger/V-pattern/fractal), `fund_flow.py` (north flow/volume anomaly), `sentiment.py` (NLP/fear-greed), `anomaly.py` (AI anomaly detection). `fusion.py` does weighted fusion using weights from `config/strategy.yaml`.
- **`ai/`** — `feature_engine.py` builds feature matrices. Models: `scorer_xgb.py` (XGBoost signal scoring), `predictor_lstm.py` (LSTM price prediction, 60-day sequences), `position_rl.py` (PPO position sizing via stable-baselines3), `risk_cls_catboost.py` (CatBoost risk classifier), `macro_llm.py` (local Ollama/cloud DeepSeek LLM macro analysis). `ensemble.py` combines model outputs.
- **`decision/`** — `validator.py` enforces the 12-layer buy defense (market cap, data completeness, fake-data detection, limit-up filter, score threshold, sentiment, news NLP, blacklist, volume anomaly, new-low, AI risk, liquidity). `position_sizer.py` calculates position size with Kelly-like formula. `broker_api.py` abstracts broker interfaces to place real orders.
- **`risk/`** — `circuit_breaker.py` implements 5-tier cascading breakers (L1: single stock -5% → close; L2: stock daily -3% → ban 1 day; L3: portfolio daily -4% → no new positions; L4: portfolio weekly -8% → close all + halt week; L5: HS300 -5% or 100+ limit-downs → close all + pause). `stop_loss.py` monitors hard stops and trailing stops. `position_monitor.py` tracks concentration and exposure.
- **`evolution/`** — `daily_review.py` runs post-market (15:30) review of all trades. `weekly_optimizer.py` uses genetic algorithms to tune strategy parameters. `lifecycle.py` manages strategy promotion/demotion/retirement based on rolling performance.
- **`backtest/`** — `engine.py` replays historical data through the decision pipeline. `reporter.py` generates HTML and text reports.
- **`web/`** — `server.py` is a FastAPI app with REST endpoints (`/api/status`, `/api/portfolio`, `/api/signals`, `/api/risk`, `/api/evolution`, `/api/control/*`). `dashboard.html` is a standalone SPA using ECharts for real-time visualization with 10-second auto-refresh. `tray_app.py` provides Windows system tray integration.

### Configuration

All config lives in `config/` as YAML files loaded by `main.py:load_config()`:
- `settings.yaml` — system mode, capital (200万), commissions, instruments (convertible bonds 40% / A-shares 50% / ETFs 10%), data sources, schedule intervals, trading hours, notifications (WeChat/DingTalk/email), LLM config (local Ollama qwen2.5:7b / cloud DeepSeek)
- `strategy.yaml` — signal weights by category, technical indicator parameters, V-pattern/fractal detection settings, AI model hyperparameters, convertible bond thresholds
- `risk.yaml` — 12-layer signal defense rules, 5-tier circuit breaker triggers/actions/cooldowns, stop-loss/take-profit percentages, T+1 protection

### Patterns

- **Async-first**: All I/O uses `async/await`. The Engine runs an event loop with the Scheduler ticking every 1 second.
- **Event-driven**: Modules communicate via `EventBus.publish(Event(type, data))` and `EventBus.subscribe(type, handler)`. Event types are in `EventType` enum — wildcard patterns like `"market.*"` are supported.
- **Lazy imports**: Data source fetchers use lazy imports in `DataGateway.initialize()` so missing optional dependencies (tushare, akshare, torch) don't block startup.
- **SQLite via context manager**: Always use `db._get_conn()` as a context manager for thread-safe DB access.
- **Config injection**: Modules receive their config dict at construction time — no global config singleton.
- **Tests import from expected package paths**: The test imports `from agent.trading_agent import TradingAgent`, meaning source code expects to be importable from specific package roots. The project uses `sys.path.insert(0, str(Path(__file__).parent))` in `main.py` to set up import paths.

### Environment Variables

| Variable | Purpose |
|---|---|
| `TUSHARE_TOKEN` | Tushare Pro API token |
| `DEEPSEEK_API_KEY` | Cloud LLM fallback API key |
| `WECHAT_WEBHOOK` | WeChat notification webhook |
| `DINGTALK_WEBHOOK` | DingTalk notification webhook |
| `EMAIL_SENDER` | Email notification sender |
