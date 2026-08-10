#!/usr/bin/env python
"""
猎场系统 V6.0 "Genesis" — 主入口
用法: python main.py [--mode paper|live] [--web] [--backtest] [--install]
"""

import sys
import asyncio
import argparse
import logging
import yaml
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))


def load_config(config_dir: str = "config") -> dict:
    """加载所有配置文件"""
    config = {}
    config_path = Path(config_dir)

    for cfg_file in ["settings.yaml", "strategy.yaml", "risk.yaml"]:
        fp = config_path / cfg_file
        if fp.exists():
            with open(fp, 'r', encoding='utf-8') as f:
                # 简单YAML加载（无依赖）
                content = f.read()
                config.update(yaml.safe_load(content) if hasattr(yaml, 'safe_load') else {})
    return config


def setup_logging(config: dict):
    """配置日志"""
    log_cfg = config.get('system', {})
    log_level = getattr(logging, log_cfg.get('log_level', 'INFO'))
    log_dir = Path(log_cfg.get('log_dir', 'logs'))
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_dir / 'hunting_ground.log', encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ]
    )


async def run_paper_mode(config: dict):
    """模拟交易模式"""
    from core.engine import Engine
    from data.gateway import DataGateway
    from data.storage import Database

    logger = logging.getLogger(__name__)

    # 初始化
    db = Database(config.get('system', {}).get('data_dir', 'data') + '/hunting_ground.db')
    gateway = DataGateway(config, db)
    engine = Engine(config)

    # 注册子系统
    engine.register_subsystem('database', db)
    engine.register_subsystem('gateway', gateway)

    # 初始化数据层
    await gateway.initialize()

    # 启动引擎
    await engine.run_forever()


async def run_web_mode(config: dict):
    """启动Web面板"""
    from web.server import create_app
    import uvicorn

    app = create_app(config)
    uvicorn_config = uvicorn.Config(app, host="127.0.0.1", port=8501, log_level="info")
    server = uvicorn.Server(uvicorn_config)
    await server.serve()


async def run_backtest(config: dict, start: str = "2022-01-01", end: str = "2025-12-31"):
    """运行回测"""
    from data.storage import Database
    from backtest.engine import BacktestEngine, BacktestConfig
    from backtest.reporter import BacktestReporter

    db = Database(config.get('system', {}).get('data_dir', 'data') + '/hunting_ground.db')

    bt_config = BacktestConfig(
        initial_capital=config.get('capital', {}).get('total', 2000000.0),
        start_date=start,
        end_date=end,
        max_positions=config.get('capital', {}).get('max_positions', 6),
        max_single_pct=config.get('capital', {}).get('max_single_position_pct', 0.25),
    )

    engine = BacktestEngine(bt_config, db)
    # 策略函数需要根据实际信号逻辑定义
    result = engine.run()

    reporter = BacktestReporter()
    print(reporter.generate_report(result))
    reporter.export_html(result, "backtest_report.html")


def main():
    parser = argparse.ArgumentParser(description='猎场系统 V6.0 "Genesis"')
    parser.add_argument('--mode', choices=['paper', 'live', 'web', 'backtest'],
                        default='paper', help='运行模式')
    parser.add_argument('--start', default='2022-01-01', help='回测起始日期')
    parser.add_argument('--end', default='2025-12-31', help='回测结束日期')
    parser.add_argument('--config', default='config', help='配置文件目录')

    args = parser.parse_args()
    config = load_config(args.config)
    setup_logging(config)

    logger = logging.getLogger(__name__)
    logger.info("=" * 50)
    logger.info(f"猎场系统 V6.0 | 模式: {args.mode} | 资金: {config.get('capital', {}).get('total', 0):,.0f}元")
    logger.info("=" * 50)

    if args.mode == 'backtest':
        asyncio.run(run_backtest(config, args.start, args.end))
    elif args.mode == 'web':
        asyncio.run(run_web_mode(config))
    else:
        asyncio.run(run_paper_mode(config))


if __name__ == '__main__':
    main()
