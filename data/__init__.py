"""
猎场系统 V6.0 — 数据层
"""

from .storage import Database
from .gateway import DataGateway

__all__ = ["Database", "DataGateway"]
