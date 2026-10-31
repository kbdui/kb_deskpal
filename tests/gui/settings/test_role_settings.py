from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox
from deskpal.gui.settings import InteractionSettings


class Bridge(QObject):
    reply = Signal(object)
    state = "Ready"
    privacy_epoch = 0

    def send(self, *args, **kwargs):
        return "request"


def test_role_card_has_no_presets_or_candidate_controls_and_preserves_edits(qtbot):
    widget = InteractionSettings(SimpleNamespace(bridge=Bridge(), available=True))
    qtbot.addWidget(widget)
    widget._snapshot({"schema_version": 2, "revision": 1,
                      "character_card": {"text": "沉稳好奇", "use_policy": "cloud_allowed"}})
    assert widget.card.toPlainText() == "沉稳好奇"
    widget.card.setPlainText("未保存的开放背景")
    widget._snapshot({"schema_version": 2, "revision": 2,
                      "character_card": {"text": "后台更新", "use_policy": "cloud_allowed"}})
    assert widget.card.toPlainText() == "未保存的开放背景"
    assert not widget.findChildren(QComboBox)
    assert not widget.findChildren(QDoubleSpinBox)
    widget.clear_sensitive()
    assert widget.card.toPlainText() == ""
