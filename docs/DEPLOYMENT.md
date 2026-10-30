# 猎场系统 V7.0 部署与操作手册

本文对应 `main.py --mode v7`：同时启动多账户引擎、行情同步、定时扫描和 Web 面板。默认 `system.mode: paper`，使用模拟交易。只执行 `--mode web` 或 `run_server.py` 不会启动完整交易引擎。

## 1. 环境与文件

- 推荐 Python 3.12；使用 Git 获取仓库。
- Windows PowerShell 或 Linux。Linux 进程时区应设为 `Asia/Shanghai`，因为交易时段使用本地时间。
- 可连接 Tushare MCP、DeepSeek API；备用行情使用腾讯/新浪。
- 一个数据库只运行一个 V7 实例，默认监听 TCP 8501。

```text
config/settings.yaml  系统、资金、行情、DeepSeek 配置
config/strategy.yaml  策略配置
config/risk.yaml      风控配置
data/hunting_ground.db  默认数据库（首次启动自动创建，不随源码发布）
logs/hunting_ground.log 默认日志（启动后创建，不随源码发布）
.env.example          环境变量示例，无真实凭证
```

上传版本的 `system.data_dir` 为 `./data`，`system.log_dir` 为 `./logs`。必须从项目根目录启动；修改路径时优先使用绝对路径，并确保服务运行账户有写权限。

**已有安装迁移**：源码仓库不包含旧电脑的持仓、交易和面板设置。沿用旧数据库时先按第 7 节备份，再把备份恢复到新实例的数据目录。首次使用新空目录会创建默认模拟账户。

## 2. Windows 安装与启动

```powershell
git clone https://github.com/maoqin985/hunting_ground_v6.git
Set-Location hunting_ground_v6
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 将占位文字替换为自己的有效凭证。只在当前 PowerShell 会话生效。
$env:TUSHARE_TOKEN = "你的Tushare令牌"
$env:DEEPSEEK_API_KEY = "你的DeepSeek密钥"

# 前台运行，保留此终端；Ctrl+C 停止。
.\.venv\Scripts\python.exe main.py --mode v7
```

打开 <http://localhost:8501/>。日志出现“猎场系统 V7.0 运行中”表示初始同步和任务注册完成；网页可能先于初始同步就绪。

程序**不会自动加载 `.env` 文件**。复制 `.env.example` 仅作为填写模板，实际运行必须由终端或进程管理器注入环境变量。不要把真实凭证写入 YAML、启动脚本或 Git 提交。

`TUSHARE_MCP_URL` 可选，默认 `https://api.tushare.pro/mcp/`。不需要安装 Tushare SDK 即可通过 MCP 访问；接口权限、额度及实时数据可用性由 Tushare 账户决定。

如需 Windows 后台运行，可在已经设置上述变量的同一 PowerShell 中执行：

```powershell
$projectRoot = (Get-Location).Path
New-Item -ItemType Directory -Force -Path .\logs | Out-Null
$serviceProcess = Start-Process -FilePath "$projectRoot\.venv\Scripts\python.exe" -ArgumentList @('main.py','--mode','v7') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput "$projectRoot\logs\console.out.log" -RedirectStandardError "$projectRoot\logs\console.err.log" -PassThru
$serviceProcess.Id
```

后台子进程继承启动时的环境变量。修改密钥后需重启进程。日常维护优先使用前台 Ctrl+C 正常退出；后台维护前先核实端口对应的进程，不要按所有 `python.exe` 批量结束。

## 3. Linux 安装与启动

```bash
git clone https://github.com/maoqin985/hunting_ground_v6.git
cd hunting_ground_v6
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
export TZ=Asia/Shanghai
export TUSHARE_TOKEN='你的Tushare令牌'
export DEEPSEEK_API_KEY='你的DeepSeek密钥'
.venv/bin/python main.py --mode v7
```

推荐使用 systemd 长期运行。以下示例假定仓库在 `/opt/hunting_ground_v6`，运行账户为已创建的 `hunting`，且对项目数据和日志目录有写权限。按实际安装位置替换路径。

将以下内容保存为 `/etc/hunting-ground.env`，仅允许管理员和必要服务账户读取（例如 root 所有、权限 600，由 systemd 读取）：

```ini
TZ=Asia/Shanghai
TUSHARE_TOKEN=你的Tushare令牌
DEEPSEEK_API_KEY=你的DeepSeek密钥
```

将以下内容保存为 `/etc/systemd/system/hunting-ground.service`：

```ini
[Unit]
Description=Hunting Ground V7 paper trading dashboard
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=hunting
WorkingDirectory=/opt/hunting_ground_v6
EnvironmentFile=/etc/hunting-ground.env
ExecStart=/opt/hunting_ground_v6/.venv/bin/python main.py --mode v7
Restart=on-failure
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hunting-ground
sudo systemctl status hunting-ground
sudo journalctl -u hunting-ground -n 100 --no-pager
sudo systemctl restart hunting-ground
# 停止：sudo systemctl stop hunting-ground
```

面板目前监听 `0.0.0.0:8501`，未内置用户登录鉴权。远程服务器应限制 8501 入站访问，通过 SSH 隧道连接；不要直接对公网开放。客户端执行 `ssh -L 8501:127.0.0.1:8501 用户@服务器` 后访问 <http://localhost:8501/>。

## 4. 验证启动及数据连接

Windows：

```powershell
Invoke-RestMethod http://127.0.0.1:8501/api/v2/system/health
$settings = Invoke-RestMethod http://127.0.0.1:8501/api/settings
$settings.llm_status
$settings.tushare_status
Get-Content .\logs\hunting_ground.log -Tail 80
```

Linux：

```bash
curl -fsS http://127.0.0.1:8501/api/v2/system/health
curl -fsS http://127.0.0.1:8501/api/settings
tail -n 80 logs/hunting_ground.log
```

面板右上角“设置”显示 DeepSeek 和 Tushare 状态，可点击刷新。DeepSeek 调用次数、成功次数和最近错误属于当前进程，重启会清零；调优参数保存在数据库。

| 状态 | 含义与处理 |
| --- | --- |
| DeepSeek `not_configured` | 未读取到 API Key，继续使用原规则 |
| DeepSeek `ready` | 配置已加载，等待符合原规则的信号触发调用 |
| DeepSeek `checking` | 正在调用 |
| DeepSeek `healthy` | 已有成功调用；结合最近成功时间判断 |
| DeepSeek `fallback` | 调用失败，已回退原规则，检查最近错误、权限及网络 |
| Tushare `not_configured` | 未读取到 Token，使用可用备用数据源 |
| Tushare 显示可用 | 当前实现根据凭证和客户端配置判断，不等于已完成每个接口的网络验证；实际权限和返回以同步日志为准 |

Tushare 负责股票列表、日 K、每日指标、实时行情和指数等请求。MCP 接口不支持、无权限或失败时，网关尝试可用的其他数据源；备用行情也可能限流或不可用。

## 5. 日常操作与决策边界

1. 启动后确认账户、资金、持仓与预期一致，再检查设置中的连接状态。
2. 面板可切换进攻型、稳定型和成长猎手账户，查看信号、持仓、成交、风控和报告。
3. 默认候选池市值为 10～50 亿元，市场扫描、异动扫描和止损检查默认间隔为 60 秒，仅在交易时段执行相关任务。
4. 面板保存的运行设置会持久化到数据库，重启时恢复，因此可能覆盖 YAML 中对应的默认值。
5. 模型未配置、调用失败或信号未达原规则门槛时，未出现 DeepSeek 成功调用不代表进程故障。

当前 V7 路径使用原规则评分器，加 DeepSeek 受限复核；仓库中存在 XGBoost、LSTM、PPO、CatBoost 等模块，不代表本次启动已经加载训练完成的模型。

DeepSeek 可读取由网关取得的行情、每日指标及最近 20 根 K 线，在原规则允许买入后确认、否决或有限调整评分和仓位。原规则 `hold` 不能被升级为 `buy`。默认评分调整范围 ±10，仓位乘数 0.5～1.0，最低置信度 0.60；有限自调不替代原风控、硬止损和熔断。

## 6. 更新、重启与回滚

更新前停止服务并备份数据库；检查 `git status`，保留本地配置修改。确认工作区无待保留改动后：

```bash
git pull --ff-only origin master
# Windows 将下面的解释器路径改为 .\.venv\Scripts\python.exe
.venv/bin/python -m pip install -r requirements.txt
```

重新启动后复查健康接口、账户和持仓。需要回滚时，保留当前版本提交号，停止服务后切换到已验证的旧提交；若版本涉及数据库迁移，同时恢复与旧代码对应的备份。不要只回退代码而忽略数据库兼容性。

## 7. 数据库备份与恢复

备份必须包含 SQLite 中的持仓、交易、账户、设置和自调状态。推荐使用 SQLite 在线备份接口，避免只复制正在写入的 `.db` 而遗漏 WAL 数据。以下命令从项目根目录执行，Windows 使用 `.\.venv\Scripts\python.exe` 替换 `python`：

```bash
python -c "import sqlite3,pathlib,datetime; source=pathlib.Path('data/hunting_ground.db').resolve(); assert source.is_file(), 'Database missing'; target=pathlib.Path('backup')/('hunting_ground_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.db'); target.parent.mkdir(exist_ok=True); src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True); dst=sqlite3.connect(target); src.backup(dst); dst.close(); src.close(); print(target)"
```

如果配置了其他 `data_dir`，需同步替换命令中的源路径。备份文件和真实凭证应保存在本地或私有备份位置，不上传源码仓库。

恢复步骤：停止唯一的 V7 实例；备份当前数据目录；将选定备份复制到**新的空数据目录**并命名为 `hunting_ground.db`；修改 `system.data_dir` 指向该目录；重启检查。不要把备份直接覆盖到仍在运行的 SQLite 数据库上，也不要混用旧的 `-wal`/`-shm` 文件。

若使用 `HUNTING_GROUND_DATA_DIR`，请保持它与 YAML 中 `system.data_dir` 一致：当前环境变量主要供独立 Web 初始化使用，V7 引擎数据库实际使用 YAML 配置。

## 8. 常见问题

- **8501 被占用**：Windows 执行 `Get-NetTCPConnection -LocalPort 8501 -State Listen` 查看进程，再确认是否已有实例；不要重复启动共享同一数据库的进程。
- **日志或数据库拒绝访问**：给运行账户授予对应目录写权限，或修改 YAML 为可写目录。不要因此创建另一个空数据库而误以为旧账户丢失。
- **设置过变量但仍未接入**：变量只在设置它的终端及之后创建的子进程生效。通过其他终端、任务计划或服务启动时，需为那个进程配置变量。
- **Tushare 权限/额度不足**：确认账户有对应 MCP 工具权限。指数、实时接口与日线接口的权限可能不同，查看日志中的实际数据源和错误。
- **DeepSeek 401/超时/额度错误**：检查 `DEEPSEEK_API_KEY`、账户余额、API 地址及网络；修复后重启或等待下一次符合规则的信号，并刷新设置查看结果。
- **无交易信号**：检查交易日、时区、行情更新、候选池、原规则门槛和风控状态。

## 9. 验证范围

基础检查：

```bash
python -m compileall -q accounts ai backtest core data decision evolution execution ops risk signals stock_pool web main.py
python -m pytest tests/test_v7_multiccount.py -q -k "not EventBus"
```

仓库遗留的 `tests/test_agent.py` 引用缺失的 `agent` 包，完整测试集会在收集阶段失败；上述账户集成测试排除了 3 个依赖异步测试支持的 EventBus 测试。这些限制不应被表述为全量测试通过。

Windows 上已验证 V7 启动和本机 HTTP 接口；Linux/systemd 步骤是部署配置示例，尚未在本次上传中实际部署验证。DeepSeek 真实调用需要有效密钥；没有密钥时只能验证规则回退路径。
