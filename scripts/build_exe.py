"""
猎场系统 V6.0 — PyInstaller 打包脚本
生成 Windows 单文件 EXE

用法:
  python scripts/build_exe.py           # 轻量版 (无AI模型)
  python scripts/build_exe.py --full    # 完整版 (含Ollama LLM)
  python scripts/build_exe.py --one-dir # 目录版 (启动更快)
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist"

SPEC_CONTENT = """# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['{main_script}'],
    pathex=['{project_root}'],
    binaries=[],
    datas=[
        ('config/*.yaml', 'config'),
        ('web/dashboard.html', 'web'),
    ],
    hiddenimports=[
        'aiohttp', 'yaml', 'numpy', 'pandas',
        'fastapi', 'uvicorn', 'asyncio',
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'jupyter',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HuntingGroundV6',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
"""


def build():
    """执行打包"""
    try:
        import PyInstaller.__main__ as pyi

        args = [
            str(PROJECT_ROOT / 'main.py'),
            '--name=HuntingGroundV6',
            '--onefile',
            '--console',
            '--add-data=config/*.yaml;config',
            '--add-data=web/dashboard.html;web',
            '--hidden-import=aiohttp',
            '--hidden-import=yaml',
            '--hidden-import=fastapi',
            '--exclude-module=tkinter',
            '--exclude-module=matplotlib',
            '--clean',
            '--noconfirm',
        ]

        print(f"Building EXE from {PROJECT_ROOT}...")
        pyi.run(args)
        print(f"Build complete! Output: {DIST_DIR / 'HuntingGroundV6.exe'}")

    except ImportError:
        print("Please install pyinstaller: pip install pyinstaller")
        sys.exit(1)


if __name__ == '__main__':
    build()
