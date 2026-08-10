@echo off
chcp 65001 >nul
echo ============================================================
echo  猎场系统 V6.0 "Genesis" — Windows 一键安装
echo ============================================================
echo.

:: 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到Python，请先安装 Python 3.11+
    echo 下载: https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python 已安装

:: 创建虚拟环境（可选）
if not exist "venv" (
    echo 创建虚拟环境...
    python -m venv venv
)
echo [OK] 虚拟环境就绪

:: 激活虚拟环境
call venv\Scripts\activate.bat

:: 安装依赖
echo 安装核心依赖...
pip install pyyaml numpy pandas aiohttp fastapi uvicorn aiosqlite -q

echo.
echo 是否安装 AI 模型依赖? (y/n)
set /p INSTALL_AI=
if /i "%INSTALL_AI%"=="y" (
    echo 安装 AI 依赖（可能需要较长时间）...
    pip install xgboost catboost scikit-learn -q
    echo [OK] AI 依赖安装完成
)

echo.
echo 是否安装 LLM 支持（Ollama + Qwen2.5）? (y/n)
set /p INSTALL_LLM=
if /i "%INSTALL_LLM%"=="y" (
    echo 请手动安装 Ollama: https://ollama.com/download/windows
    echo 然后运行: ollama pull qwen2.5:7b
)

:: 创建必要目录
mkdir data 2>nul
mkdir data\models 2>nul
mkdir data\daily_signals 2>nul
mkdir logs 2>nul
mkdir backup 2>nul

echo.
echo ============================================================
echo  安装完成！
echo.
echo  配置步骤:
echo  1. 编辑 config\settings.yaml (可选)
echo  2. 设置 Tushare Token: set TUSHARE_TOKEN=your_token
echo.
echo  启动命令:
echo  - 模拟交易:   python main.py --mode paper
echo  - Web面板:    python main.py --mode web
echo  - 回测:       python main.py --mode backtest
echo ============================================================
pause
