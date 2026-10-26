"""Bounded local DTOs; preview bytes must not enter generic JSON channels."""

from dataclasses import dataclass, field


class AssetError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        messages = {
            "asset_decoder_unavailable": "图片解码组件不可用，请在安装桌面组件后导入图片。",
            "asset_revision_conflict": "外观已被另一窗口更新，请刷新后重新保存。",
            "asset_generation_stale": "这次图片选择已过期，请使用最新预览。",
            "asset_import_expired": "图片预览已过期或已取消，请重新选择。",
            "asset_format_unsupported": "仅支持内容与扩展名一致的 PNG 或 JPEG 静态图片。",
            "asset_animation_unsupported": "不支持多帧或动画图片，请选择静态 PNG 或 JPEG。",
            "asset_pet_transparent": "桌宠形象不能全透明，请选择具有可见内容的图片。",
            "asset_byte_limit": "图片必须大于零且不超过 5 MiB。",
            "asset_dimension_limit": "图片单边不得超过 2048 像素。",
            "asset_group_limit": "桌宠表情组超过 8 百万像素或 20 MiB，请缩小图片。",
            "asset_integrity_failed": "托管图片校验失败，请重新导入或恢复默认。",
            "asset_publish_failed": "图片保存失败，请检查可用空间与文件权限。",
            "asset_path_unsafe": "图片目录或文件类型不安全，请保留文件并检查目录。",
        }
        self.message = messages.get(code, "图片操作失败，请重新选择或恢复默认。")
        super().__init__(self.message)


@dataclass(frozen=True)
class NormalizedImage:
    png: bytes = field(repr=False)
    width: int
    height: int
    has_visible_pixels: bool


@dataclass(frozen=True)
class AssetMetadata:
    asset_id: str
    media_type: str
    bytes: int
    width: int
    height: int
    sha256: str
    revision: int = 1
    provenance: str = "user_import"


@dataclass(frozen=True)
class AppearanceProfile:
    slot: str
    revision: int
    asset_id: str | None
    effective_asset_id: str | None
    builtin: bool
    display_mode: str
    crop: tuple[float, float, float, float]
    asset: AssetMetadata | None
    schema_version: int = 1


@dataclass(frozen=True)
class PreparedImport:
    token: str
    slot: str
    generation: int
    width: int
    height: int
    bytes: int
    sha256: str


@dataclass(frozen=True)
class AssetPreview:
    png: bytes = field(repr=False)
    width: int
    height: int
    asset_id: str | None = None
    token: str | None = None


@dataclass(frozen=True)
class CollectionResult:
    removed_assets: int
    removed_files: int
    discarded_imports: int
    more: bool
