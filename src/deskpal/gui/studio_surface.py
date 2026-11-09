"""Local renderer for the approved Clear Studio design."""
from functools import lru_cache
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QFile, QIODevice, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget
from shiboken6 import delete, isValid

ASSETS = Path(__file__).parent / "assets" / "studio"


@lru_cache(maxsize=1)
def studio_document() -> str:
    channel = QFile(":/qtwebchannel/qwebchannel.js")
    if not channel.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("Qt WebChannel script is unavailable")
    library = bytes(channel.readAll().data()).decode("utf-8")
    style = (ASSETS / "studio.css").read_text(encoding="utf-8")
    shell = (ASSETS / "shell.html").read_text(encoding="utf-8")
    script = (ASSETS / "studio.js").read_text(encoding="utf-8")
    nonce = uuid4().hex
    policy = (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; img-src data:; "
        f"script-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'"
    )
    return (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{policy}">'
            f'<title>Deskpal</title><style>{style}</style><body>{shell}'
            f'<script nonce="{nonce}">{library}</script>'
            f'<script nonce="{nonce}">{script}</script></body></html>')


class StudioPage(QWebEnginePage):
    allow_document = False

    def acceptNavigationRequest(self, url: QUrl | str, kind: Any, main_frame: bool) -> bool:
        if main_frame and self.allow_document and QUrl(url).scheme() == "data":
            self.allow_document = False
            return True
        return False


class StudioPort(QObject):
    message = Signal(str)
    action = Signal(str, dict)
    attached = Signal()

    @Slot()
    def attach(self) -> None:
        self.attached.emit()

    @Slot(str, str)
    def send(self, name: str, raw: str) -> None:
        if len(raw) > 120_000 or len(name) > 40:
            return
        try:
            value = json.loads(raw)
        except ValueError:
            return
        if isinstance(value, dict):
            self.action.emit(name, value)

    def publish(self, kind: str, payload: Any) -> None:
        self.message.emit(json.dumps({"kind": kind, "data": payload}, ensure_ascii=False))


class StudioSurface(QWidget):
    """An isolated, in-memory browser profile shared by all new visual surfaces."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.port = StudioPort(self)
        # The view owns the page and precedes the profile in QObject teardown.
        self.view = QWebEngineView(self)
        self.profile = QWebEngineProfile(self)
        self.page = StudioPage(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        settings = self.page.settings()
        for attribute in (
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows,
            QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard,
            QWebEngineSettings.WebAttribute.NavigateOnDropEnabled,
        ):
            settings.setAttribute(attribute, False)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("studio", self.port)
        self.page.setWebChannel(self.channel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

    def load_studio(self) -> None:
        self.page.allow_document = True
        self.view.setHtml(studio_document())

    def release_studio(self) -> None:
        """Release the page before its profile and before QApplication exits."""
        if isValid(self.page):
            self.channel.deregisterObject(self.port)
            delete(self.page)
        if isValid(self.profile):
            delete(self.profile)
