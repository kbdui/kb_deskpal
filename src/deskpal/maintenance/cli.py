"""Explicit local maintenance CLI, without constructing ApplicationService."""

import argparse
from collections.abc import Sequence
import getpass
import json
from pathlib import Path
import sys
from typing import Any

from .errors import MaintenanceError
from .service import MaintenanceService, OPERATIONS, default_config_path, safe_error


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _constant(value: str) -> Any:
    raise ValueError("nonfinite")


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = argparse.ArgumentParser(description="Deskpal 本地配置、模型缓存与跨库备份恢复")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--maintenance", required=True, choices=sorted(OPERATIONS))
    parser.add_argument("--maintenance-parameters", default="{}", metavar="JSON")
    parser.add_argument("--store-api-key", action="store_true", help="保存配置时用隐藏输入写入 Windows 系统凭据库")
    parser.add_argument("--json", action="store_true", help="兼容原 CLI；本维护入口始终返回 JSON 回执")
    args = parser.parse_args(argv)
    if args.store_api_key and args.maintenance not in {"setup_save", "setup_repair"}:
        parser.error("--store-api-key 只能用于保存或修复配置")
    try:
        if len(args.maintenance_parameters.encode("utf-8")) > 131072:
            raise MaintenanceError("invalid_config")
        parameters = json.loads(args.maintenance_parameters, object_pairs_hook=_pairs, parse_constant=_constant)
        if not isinstance(parameters, dict) or "credential_secret" in parameters:
            raise MaintenanceError("invalid_config")
        if args.store_api_key:
            if not sys.stdin.isatty():
                raise MaintenanceError("confirmation_required")
            secret = getpass.getpass("新的 API 密钥（不会写入 TOML）：")
            if not secret:
                raise MaintenanceError("credential_missing")
            parameters["credential_secret"] = secret
        try:
            result = MaintenanceService(args.config if args.config is not None else default_config_path()).execute(
                args.maintenance, parameters)
        finally:
            parameters.pop("credential_secret", None)
        print(json.dumps({"ok": True, "operation": args.maintenance, "data": result},
                         ensure_ascii=False, allow_nan=False))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({"ok": False, "operation": args.maintenance, "error": safe_error(error)},
                         ensure_ascii=False, allow_nan=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
