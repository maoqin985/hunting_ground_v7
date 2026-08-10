"""
猎场系统 V6.0 — Windows 系统托盘程序
"""

import sys
import threading
import logging
import webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)


class TrayApp:
    """Windows 系统托盘应用"""

    def __init__(self, engine=None, web_port: int = 8501):
        self.engine = engine
        self.web_port = web_port
        self._running = False

    def run(self):
        """启动托盘（Windows）"""
        try:
            import pystray
            from PIL import Image, ImageDraw

            def create_icon():
                """创建简单图标"""
                img = Image.new('RGB', (64, 64), color='#1a1a2e')
                draw = ImageDraw.Draw(img)
                draw.rectangle([16, 16, 48, 48], fill='#4caf50')
                draw.rectangle([24, 24, 40, 40], fill='#1a1a2e')
                return img

            def on_open(icon, item):
                webbrowser.open(f'http://localhost:{self.web_port}')

            def on_pause(icon, item):
                if self.engine:
                    import asyncio
                    asyncio.run(self.engine.pause())
                icon.notify("交易已暂停")

            def on_resume(icon, item):
                if self.engine:
                    import asyncio
                    asyncio.run(self.engine.resume())
                icon.notify("交易已恢复")

            def on_exit(icon, item):
                icon.stop()
                if self.engine:
                    import asyncio
                    asyncio.run(self.engine.stop())

            menu = pystray.Menu(
                pystray.MenuItem("打开面板", on_open, default=True),
                pystray.MenuItem("暂停交易", on_pause),
                pystray.MenuItem("恢复交易", on_resume),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", on_exit),
            )

            icon = pystray.Icon(
                "hunting_ground_v6",
                create_icon(),
                "猎场 V6.0",
                menu,
            )

            self._running = True
            icon.run()

        except ImportError:
            logger.warning("pystray/Pillow not installed, tray mode unavailable")
            logger.info("Use web mode: python main.py --mode web")
        except Exception as e:
            logger.error(f"Tray error: {e}")
