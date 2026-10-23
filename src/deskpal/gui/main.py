"""Optional desktop entry point. Configuration is loaded by the background factory."""

import argparse
from pathlib import Path
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deskpal Windows 桌面应用")
    parser.add_argument("--config", type=Path, help="TOML 配置路径；密钥使用环境/.env或系统凭据引用")
    parser.add_argument("--offline", action="store_true", help="本次运行使用离线模型，不调用云 API")
    parser.add_argument("--setup", "--maintenance", dest="maintenance", action="store_true",
                        help="启动独立配置、模型缓存和备份恢复维护界面")
    args = parser.parse_args(argv)
    try:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("GUI 依赖未安装：请在项目环境运行 python -m pip install -r requirements-gui.txt", file=sys.stderr)
        return 2
    from deskpal.application.factory import create_application
    from deskpal.config import load_config
    from deskpal.maintenance.service import default_config_path
    from .launcher import DesktopLauncher

    app = QApplication([sys.argv[0]])
    QCoreApplication.setOrganizationName("Deskpal")
    QCoreApplication.setApplicationName("Deskpal")
    app.setQuitOnLastWindowClosed(False)
    config_path = args.config if args.config is not None else default_config_path()
    launcher = DesktopLauncher(config_path, lambda: create_application(load_config(config_path,
        provider_override="offline" if args.offline else None)), maintenance=args.maintenance,
        maintenance_provider_override="offline" if args.offline else None)
    launcher.start()
    return app.exec()
