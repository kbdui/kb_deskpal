"""Public maintenance failures contain fixed, credential-free messages only."""

MESSAGES = {
    "invalid_config": "配置无效，请检查非秘密设置；损坏文件需明确确认修复。",
    "config_missing": "尚未创建配置，请先完成首次配置。",
    "revision_conflict": "内容已变化，请刷新后重新确认。",
    "confirmation_required": "此操作需要明确确认。",
    "busy": "工作库或模型缓存正在使用，请关闭相关实例后重试。",
    "unsafe_path": "目录关系或文件类型不安全，请选择独立的普通目录。",
    "io_error": "本地文件操作失败，请检查权限和可用空间。",
    "credential_missing": "未发现可用凭据，请配置所选环境变量或 Windows 系统凭据。",
    "environment_override": "环境设置会覆盖文件中的部分设置。",
    "cache_missing": "尚未准备语义检索模型，请明确允许下载后准备。",
    "cache_unmanaged": "此目录不是本程序管理的缓存；可离线校验，请换用空目录准备。",
    "cache_invalid": "模型缓存未通过校验，请选择新目录重新准备。",
    "optional_dependency_missing": "请手动执行 python -m pip install -r requirements-embedding.txt 安装固定版本依赖。",
    "download_not_allowed": "下载需要明确授权。",
    "cancelled": "操作已协作取消；当前模型调用结束后才释放资源。",
    "credential_store_unavailable": "系统凭据库不可用，请使用外部环境变量配置凭据。",
    "archive_sensitive_ack_required": "备份含私人内容，请明确确认敏感内容后重试。",
    "archive_rollback_ack_required": "恢复旧备份可能重新引入已遗忘内容，请明确确认。",
    "archive_invalid": "备份无效，请选择完整可信的备份文件。",
    "archive_schema_unsupported": "备份数据库版本不受支持，请使用匹配版本。",
    "archive_limit_exceeded": "备份超过当前处理限额，请保留数据并使用支持更大归档的版本。",
    "archive_destination_exists": "目标已经存在，请选择新的文件名或目录。",
    "archive_path_unsafe": "备份路径不安全，请选择独立的普通目录。",
    "archive_instance_busy": "工作库正在使用，请关闭相关实例后重试。",
    "archive_source_uninitialized": "尚无已初始化工作库，请先正常启动一次。",
    "archive_io_error": "备份文件操作失败，请检查权限和空间。",
    "restore_pending": "恢复尚未完成，请按恢复引导继续。",
    "restore_resume_required": "检测到未完成恢复，请先继续或处理该恢复。",
    "restore_conflict": "恢复目标已变化，请保留现场并重新检查。",
}


class MaintenanceError(RuntimeError):
    """Stable public error code; never include an original exception."""

    def __init__(self, code: str) -> None:
        self.code = code if code in MESSAGES else "io_error"
        self.message = MESSAGES[self.code]
        super().__init__(self.message)


def message_for(code: str) -> str:
    return MESSAGES.get(code, MESSAGES["io_error"])
