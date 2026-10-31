"""Readable settings retain exact safe data and original command authority."""

import json
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton, QScrollArea

from deskpal.gui.maintenance import MaintenanceWindow
from tests.support.maintenance_gui import ControlledBridge, status
from tests.support.window import window


def _leaves(tree):
    def walk(item):
        yield item
        for index in range(item.childCount()):
            yield from walk(item.child(index))
    for index in range(tree.topLevelItemCount()):
        yield from walk(tree.topLevelItem(index))


def test_settings_default_summaries_hide_internal_values_and_copy_exact_paths(window, qtbot):
    widget, _ = window
    settings = widget.settings_page
    workspace = r"C:\Users\synthetic-person\Documents\very-long-workspace\project"
    data_dir = r"C:\Users\synthetic-person\AppData\Local\Deskpal\private-data"
    settings._settings({"provider": "openai", "execution_location": "cloud", "model": "synthetic-model",
                        "llm_ready": True, "credential_source": "system_vault", "active_revision": "a" * 64,
                        "memory_mode": "candidate_review", "embedding_provider": "disabled",
                        "workspace_dir": workspace, "data_dir": data_dir,
                        "credential_secret": "synthetic-never-display"})
    widget.set_page(4)
    settings.tabs.setCurrentIndex(3)
    widget.show()
    qtbot.waitUntil(settings.memory_summary.isVisible)
    assert "自动学习与模型复核" in settings.memory_summary.text()
    assert "关键词检索" in settings.memory_summary.text()
    assert workspace not in settings.memory_summary.text() and data_dir not in settings.memory_summary.text()
    assert "Windows 系统凭据库" in settings.model_summary.text()
    assert "active_revision" not in settings.model_summary.text() and "a" * 64 not in settings.model_summary.text()
    assert not settings.memory_info.isVisible() and not settings.model_info.isVisible()
    node = next(item for item in _leaves(settings.memory_info)
                if item.data(1, Qt.ItemDataRole.UserRole) == workspace)
    settings.memory_info.setCurrentItem(node)
    copy = settings.memory_info.parentWidget().findChild(QPushButton)
    copy.click()
    assert QGuiApplication.clipboard().text().endswith(workspace)
    assert "synthetic-never-display" not in "\n".join(label.text() for label in settings.findChildren(QLabel))
    settings._privacy()
    assert not settings.memory_summary.text() and settings.memory_info.topLevelItemCount() == 0


def test_maintenance_safe_receipt_hides_secrets_and_preserves_original_save_values(qtbot):
    bridge = ControlledBridge()
    bridge.config_path = Path(r"C:\Users\synthetic-person\configuration\deskpal.toml")
    widget = MaintenanceWindow(bridge)
    qtbot.addWidget(widget)
    widget.open()
    workspace = r"C:\Users\synthetic-person\projects\actual-workspace"
    values = {"provider": "offline", "timeout_seconds": 30, "workspace_dir": workspace,
              "data_dir": r"C:\Users\synthetic-person\data", "credential_secret": "synthetic-dto-secret"}
    bridge.complete(status(values=values, effective=values, credential_secret="synthetic-dto-secret"))
    assert "配置可供启动" in widget.output.toPlainText()
    assert "provider:" not in widget.output.toPlainText() and workspace not in widget.output.toPlainText()
    assert not widget.details_panel.isVisible() and not widget.config_location.isVisible()
    assert str(bridge.config_path) not in widget.config_label.text()
    assert widget.config_location.text() == str(bridge.config_path)
    widget.config_copy.click()
    assert QGuiApplication.clipboard().text() == str(bridge.config_path)
    receipt = json.loads(widget.technical_output.toPlainText())
    assert receipt["values"]["workspace_dir"] == workspace and receipt["revision"] == "r1"
    assert "synthetic-dto-secret" not in widget.technical_output.toPlainText()
    widget._save(False)
    submitted = bridge.calls[-1][2]
    assert submitted["values"]["workspace_dir"] == workspace and submitted["expected_revision"] == "r1"
    assert "credential_secret" not in submitted["values"]
    widget.close()
    bridge.stopped.emit()


def test_backup_receipt_copies_full_archive_hash_and_database_hashes(qtbot):
    bridge = ControlledBridge()
    widget = MaintenanceWindow(bridge)
    qtbot.addWidget(widget)
    widget.open()
    bridge.complete(status())
    archive = r"C:\Users\synthetic-person\Documents\backups\long-name.zip"
    digest = "9f" * 32
    db_digest = "a7" * 32
    manifest = {"format_version": 2, "databases": [{"name": "workflow.sqlite3", "present": True,
                  "bytes": 312, "sha256": db_digest, "schema_version": 3}],
                "assets": [{"name": "assets/synthetic.png", "bytes": 32, "sha256": "b" * 64,
                            "width": 1, "height": 1, "media_type": "image/png"}]}
    widget.request("backup_inspect", {"archive": archive})
    bridge.complete({"archive_path": archive, "archive_sha256": digest, "manifest": manifest,
                     "credential_secret": "synthetic-never-display"})
    assert "完整性检查已完成" in widget.output.toPlainText()
    assert archive not in widget.output.toPlainText() and digest not in widget.output.toPlainText()
    assert not widget.details_panel.isVisible()
    widget.details_toggle.click()
    widget.copy_receipt.click()
    copied = json.loads(QGuiApplication.clipboard().text())
    assert copied["archive_path"] == archive and copied["archive_sha256"] == digest
    assert copied["manifest"] == manifest
    assert "synthetic-never-display" not in widget.technical_output.toPlainText()
    widget.close()
    bridge.stopped.emit()


def test_cache_chinese_evidence_preserves_cas_revision(qtbot):
    bridge = ControlledBridge()
    widget = MaintenanceWindow(bridge)
    qtbot.addWidget(widget)
    widget.open()
    bridge.complete(status())
    revision = "c" * 64
    widget.request("cache_status")
    bridge.complete({"status": "ready", "verification": "previously_verified", "revision": revision,
                     "managed": True, "dependency_available": True, "files": 4, "bytes": 1048576})
    assert "只记录曾成功验证；本次尚未验证" in widget.output.toPlainText()
    assert revision not in widget.output.toPlainText()
    assert json.loads(widget.technical_output.toPlainText())["revision"] == revision
    widget.clear_confirm.setChecked(True)
    widget._clear_cache()
    assert bridge.calls[-1][2] == {"expected_revision": revision, "confirm": True}
    widget.close()
    bridge.stopped.emit()


@pytest.mark.parametrize("width", [720, 1280, 1920])
def test_full_window_settings_remain_narrow_and_scrollable(window, qtbot, monkeypatch, width):
    widget, _ = window
    # The offscreen plugin starts with an 800px screen. Give the production
    # screen-fit policy a synthetic large desktop to exercise the requested size.
    monkeypatch.setattr(widget.screen(), "availableGeometry", lambda: QRect(0, 0, 2200, 1000))
    widget.resize(width, 760)
    widget.show()
    widget.set_page(4)
    settings = widget.settings_page
    for index in range(settings.tabs.count()):
        settings.tabs.setCurrentIndex(index)
        qtbot.wait(15)
        area = settings.tabs.currentWidget()
        assert isinstance(area, QScrollArea)
        assert widget.width() == width
        assert area.viewport().width() >= 300
        assert area.horizontalScrollBar().maximum() == 0
        assert area.viewport().height() >= 350
        assert area.widget().width() <= 960
        for field_type in (QComboBox, QLineEdit):
            for field in area.widget().findChildren(field_type):
                assert field.width() <= 960
    settings.tabs.setCurrentIndex(4)
    qtbot.wait(15)
    open_model = next(button for button in settings.findChildren(QPushButton)
                      if button.text() == "编辑连接与密钥")
    assert 100 <= open_model.width() <= 420
    assert open_model.width() >= open_model.sizeHint().width()
    assert open_model.isVisible() and open_model.isEnabled()
    requested = []
    widget.maintenance_requested.connect(lambda: requested.append(True))
    open_model.click()
    assert requested == [True]


def test_maintenance_form_has_main_height_when_receipts_are_collapsed(qtbot):
    bridge = ControlledBridge()
    widget = MaintenanceWindow(bridge)
    qtbot.addWidget(widget)
    widget.open()
    bridge.complete(status())
    qtbot.wait(25)
    area = widget.screen().availableGeometry()
    assert widget.width() <= area.width() and widget.height() <= area.height()
    assert widget.tabs.height() > widget.output.height() * 2
    assert widget.tabs.height() >= 350
    assert not widget.details_panel.isVisible()
    save = next(button for button in widget.findChildren(QPushButton) if button.text() == "保存配置")
    assert save.width() == save.sizeHint().width() and save.width() <= 420
    widget.close()
    bridge.stopped.emit()
