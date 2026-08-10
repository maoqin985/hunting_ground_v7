"""
猎场系统 V6.0 — 统一日志管理
"""

import logging
import logging.handlers
from pathlib import Path


def setup_logger(name: str = "hunting_ground",
                 log_dir: str = "logs",
                 level: str = "INFO",
                 max_bytes: int = 10 * 1024 * 1024,
                 backup_count: int = 10) -> logging.Logger:
    """配置统一日志"""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)-5s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # 文件处理器（轮转）
    fh = logging.handlers.RotatingFileHandler(
        log_path / f'{name}.log',
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8',
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 交易日志
    trade_handler = logging.handlers.RotatingFileHandler(
        log_path / 'trades.log',
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8',
    )
    trade_handler.setFormatter(fmt)
    trade_logger = logging.getLogger(f'{name}.trades')
    trade_logger.addHandler(trade_handler)

    # 风控日志
    risk_handler = logging.handlers.RotatingFileHandler(
        log_path / 'risk.log',
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8',
    )
    risk_handler.setFormatter(fmt)
    risk_logger = logging.getLogger(f'{name}.risk')
    risk_logger.addHandler(risk_handler)

    return logger
