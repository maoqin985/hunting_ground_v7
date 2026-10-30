# 猎场系统 V7.0 "Genesis"

**AI 原生量化交易系统** — 专为 A股小盘股（≤50亿市值）设计

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---


## V7 部署与操作

完整安装、凭证配置、Windows/Linux 启动、连接状态检查、备份恢复及故障处理见 **[部署与操作手册](docs/DEPLOYMENT.md)**。

默认通过 `python main.py --mode v7` 启动完整多账户模拟交易系统及 Web 面板（http://localhost:8501/）。仅 `--mode web` 启动面板，不启动完整引擎。

当前 V7 使用原规则决策，加可选 DeepSeek 受限复核；Tushare MCP 为主数据源，腾讯/新浪为备用。下列 AI 模块为仓库提供的能力，是否启用取决于启动路径及训练模型配置。源码不包含个人数据库、运行日志或真实 API 密钥；`.env.example` 不会被程序自动加载。

## 特性

- **AI 自主决策**: XGBoost 评分 + LSTM 预测 + PPO 仓位优化 + CatBoost 风控 + LLM 宏观分析
- **十二层买入防线**: 继承 V5.2 十层 + 新增 AI 风险分类 + 流动性检查
- **五层递进熔断**: 个股止损 → 个股日损 → 组合日损 → 组合周损 → 大盘熔断
- **自进化闭环**: 每日复盘 → 周度遗传算法优化 → 月度策略淘汰/晋升
- **实时事件驱动**: WebSocket 行情推送 + 异步事件总线
- **多品种支持**: A股 + 可转债(T+0) + 港股(预留) + ETF
- **200万资金优化**: 50亿以下小盘股聚焦，6只持仓分散风险
- **Windows EXE 打包**: PyInstaller 单文件 + 系统托盘 + Windows Service

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 Tushare MCP Token（主数据源；实时接口不可用时自动降级到腾讯 HTTP/WebSocket）
# PowerShell:
$env:TUSHARE_TOKEN = "your_tushare_token"
# 可选：启用 DeepSeek 受限决策复核（原规则仍是买入门槛）
$env:DEEPSEEK_API_KEY = "your_deepseek_key"

# 3. 启动 V7 多账户模拟交易
python main.py --mode v7

# 4. 仅需要独立 Web 面板时（不要与 V7 同时占用 8501）
python main.py --mode web
# 浏览器打开 http://localhost:8501

# 5. 运行回测
python main.py --mode backtest --start 2023-01-01 --end 2025-12-31
```

## 项目结构

```
hunting_ground_v6/
├── config/          # YAML 配置文件
├── core/            # 事件总线、调度器、主引擎
├── data/            # 数据接入（Tushare/AkShare/WebSocket/NLP）
├── signals/         # 技术面/资金面/情绪面/AI异常检测扫描器
├── ai/              # AI 模型（XGBoost/LSTM/PPO/CatBoost/LLM）
├── decision/        # 交易决策、信号验证、仓位计算、券商API
├── risk/            # 五层熔断、止损监控、持仓监控
├── backtest/        # 回测引擎、报告生成
├── evolution/       # 策略生命周期、每日复盘、遗传算法优化
├── web/             # FastAPI 后端、Dashboard、系统托盘
├── scripts/         # PyInstaller 打包脚本
├── tests/           # 单元测试
└── main.py          # 主入口
```

## 配置说明

编辑 `config/settings.yaml`：

```yaml
capital:
  total: 2000000.0       # 总资金 200万
  max_positions: 6        # 最大持仓 6 只
  max_single_position_pct: 0.25  # 单票 ≤25%
  max_market_cap: 50.0    # 市值 ≤50亿
```

## AI 模型训练

首次使用需积累 ≥3个月 K线数据后，逐步训练：

```python
# 信号评分模型
from ai.scorer_xgb import XGBoostScorer
scorer = XGBoostScorer(config)
scorer.train(X_train, y_train)

# LSTM 价格预测
from ai.predictor_lstm import LSTMPredictor
predictor = LSTMPredictor(config)
predictor.train(X_train, y_train, epochs=50)

# 仓位优化 RL
from ai.position_rl import RLTrainer
trainer = RLTrainer(config)
trainer.train(market_data, timesteps=100000)
```

## 打包为 EXE

```bash
pip install pyinstaller
python scripts/build_exe.py
# 输出: dist/HuntingGroundV6.exe
```

## 风险提示

⚠️ **本系统仅供学习和研究使用。股市有风险，投资需谨慎。**
AI 模型决策不代表未来收益保证。实盘前请充分模拟验证。

## License

MIT License — 详见 [LICENSE](LICENSE)

---

基于以下开源项目构建：
- [Qbot](https://github.com/UFund-Me/Qbot) — AI 量化交易机器人
- [Hikyuu](https://github.com/fasiondog/hikyuu) — C++/Python 量化框架
- [QUANTAXIS](https://github.com/yutiansut/QUANTAXIS) — 多市场量化平台
