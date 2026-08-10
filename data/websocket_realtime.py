"""
猎场系统 V6.0 — 腾讯/新浪 WebSocket 实时行情客户端
"""

import asyncio
import json
import logging
import re
from typing import Dict, List, Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


class WebSocketClient:
    """实时行情 WebSocket 客户端"""

    def __init__(self, database):
        self.db = database
        self._subscribed_codes: List[str] = []
        self._quotes_cache: Dict[str, Dict] = {}
        self._running = False
        self._callbacks: List[Callable] = []
        self._session = None

    async def subscribe(self, ts_codes: List[str]):
        """订阅实时行情"""
        self._subscribed_codes = list(set(self._subscribed_codes + ts_codes))
        if not self._running:
            self._running = True
            asyncio.create_task(self._poll_loop())
        logger.info(f"Subscribed to {len(self._subscribed_codes)} stocks realtime")

    async def on_quote(self, callback: Callable):
        """注册行情回调"""
        self._callbacks.append(callback)

    async def get_quotes(self, ts_codes: List[str] = None) -> Dict[str, Dict]:
        """获取缓存的实时行情"""
        if ts_codes is None:
            return dict(self._quotes_cache)
        return {c: self._quotes_cache.get(c, {}) for c in ts_codes}

    async def _poll_loop(self):
        """HTTP轮询替代WebSocket（更稳定）"""
        import aiohttp

        while self._running:
            try:
                if not self._subscribed_codes:
                    await asyncio.sleep(5)
                    continue

                # 腾讯行情API（HTTP长轮询）
                await self._fetch_tencent_quotes()
                await asyncio.sleep(3)  # 3秒刷新

            except Exception as e:
                logger.error(f"Realtime poll error: {e}")
                await asyncio.sleep(10)

    async def _fetch_tencent_quotes(self):
        """从腾讯接口获取实时行情"""
        import aiohttp

        # 转换代码格式：600000.SH -> sh600000
        codes = []
        for c in self._subscribed_codes:
            if c.startswith('6'):
                codes.append(f"sh{c}")
            elif c.startswith('0') or c.startswith('3'):
                codes.append(f"sz{c}")
            else:
                codes.append(f"sz{c}")

        if not codes:
            return

        url = f"http://qt.gtimg.cn/q={','.join(codes[:50])}"

        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return
                    text = await resp.text(encoding='gbk')

            # 解析返回数据
            for line in text.strip().split('\n'):
                if '="' not in line:
                    continue
                try:
                    raw_code = line.split('="')[0].split('_')[-1]
                    data_str = line.split('="')[1].strip('";\n')
                    fields = data_str.split('~')

                    if len(fields) < 50:
                        continue

                    # 还原代码格式
                    if raw_code.startswith('sh'):
                        ts_code = raw_code[2:]
                    else:
                        ts_code = raw_code[2:]

                    quote = {
                        'ts_code': ts_code,
                        'name': fields[1],
                        'price': float(fields[3]) if fields[3] else 0,
                        'open': float(fields[5]) if fields[5] else 0,
                        'high': float(fields[33]) if fields[33] else 0,
                        'low': float(fields[34]) if fields[34] else 0,
                        'volume': float(fields[6]) if fields[6] else 0,
                        'amount': float(fields[37]) if fields[37] else 0,
                        'turnover_rate': float(fields[38]) if fields[38] else 0,
                        'volume_ratio': float(fields[49]) if len(fields) > 49 and fields[49] else 1.0,
                        'change_pct': float(fields[32]) if fields[32] else 0,
                        'change': float(fields[31]) if fields[31] else 0,
                        'bid1': float(fields[9]) if fields[9] else 0,
                        'ask1': float(fields[19]) if fields[19] else 0,
                        'time': datetime.now().isoformat(),
                    }

                    self._quotes_cache[ts_code] = quote

                    # 触发回调
                    for cb in self._callbacks:
                        try:
                            await cb(quote)
                        except Exception:
                            pass

                except Exception as e:
                    logger.debug(f"Parse error for line: {line[:50]}... {e}")
                    continue

        except Exception as e:
            logger.debug(f"Tencent quote fetch error: {e}")

    async def stop(self):
        self._running = False
        logger.info("WebSocket client stopped")
