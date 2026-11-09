"""新版工作室的真实 Qt 布局、只读边界和素材接线，均使用合成数据。"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QLabel

from deskpal.gui.chat import ChatView
from deskpal.gui.components import PushButton
from deskpal.gui.studio import SettingsSections
from deskpal.gui.views import CommandForm
from tests.support.window import window, deliver, memory_item


def test_settings_sections_and_asset_slots(window, qtbot):
    widget, bridge = window
    settings = widget.settings_page
    assert isinstance(settings.tabs, SettingsSections)
    assert [settings.tabs.tabText(i) for i in range(settings.tabs.count())] == [
        '角色与称呼', '桌宠与外观', '通知与打扰', '数据与隐私', '模型连接', '数据与高级']
    avatars = settings.avatars
    assert [avatars.slot.itemData(i) for i in range(avatars.slot.count())] == ['user_avatar', 'character_avatar']
    assert all(settings.appearance.slot.itemData(i).startswith('pet_') for i in range(settings.appearance.slot.count()))
    widget.set_page(4); widget._fit_screen = lambda: None
    widget.resize(1280, 860); widget.show()
    qtbot.waitUntil(lambda: settings.tabs.navigation.isVisible())
    settings.tabs.setCurrentIndex(2)
    widget.resize(600, 640)
    qtbot.waitUntil(lambda: settings.tabs.selector.isVisible())
    assert settings.tabs.currentIndex() == 2
    assert settings.tabs.selector.currentIndex() == 2
    assert not settings.tabs.navigation.isVisible()


def test_custom_avatar_is_used_and_default_restored(qtbot):
    label = QLabel(); qtbot.addWidget(label)
    custom = QPixmap(32, 32); custom.fill(QColor('#e52030'))
    ChatView._set_avatar(label, 'character_avatar', custom, 24)
    assert label.pixmap().toImage().pixelColor(12, 12).name() == '#e52030'
    ChatView._set_avatar(label, 'character_avatar', QPixmap(), 24)
    assert label.pixmap().toImage().pixelColor(12, 12).name() != '#e52030'


def test_avatar_apply_uses_existing_revision_and_circle_crop(window):
    widget, bridge = window
    editor = widget.settings_page.avatars
    editor.controller.profiles['user_avatar'] = {'revision': 7, 'crop': [0, 0, 1, 1]}
    editor.token = 'synthetic-prepared-image'
    editor._apply()
    request = bridge.requests_for('appearance_apply')[-1]
    assert request['parameters']['slot'] == 'user_avatar'
    assert request['parameters']['display_mode'] == 'circle'
    assert request['parameters']['expected_revision'] == 7
    assert not editor.slot.isEnabled()
    assert widget.has_unsaved_content()
    editor._clear()


def test_arrangements_readonly_and_chat_navigation(window, qtbot):
    widget, bridge = window
    page = widget.arrangements
    assert not page.findChildren(CommandForm)
    assert not hasattr(page.goals, '_write')
    assert not hasattr(page, '_end_attention')
    page.tasks.set_page({'items':[{'id':'t1','request':'整理资料','status':'Waiting'}]})
    page.tasks.list.setCurrentRow(0)
    request = bridge.requests_for('task_review')[-1]
    deliver(qtbot, widget, bridge, request, {'task':{'id':'t1','status':'Waiting'}, 'conversation_id':'daily'})
    page.return_button.click()
    assert widget.stack.currentIndex() == 0
    assert widget.chat_view.conversation_id == 'daily'
    assert not any(r['operation'] in {'confirm','cancel','review_uncertain','attention_end','goal_status','update_goal'} for r in bridge.sent)
    page.proposals.set_page({'items':[{'id':'a1','status':'pending','title':'复查近况'}]})
    page.proposals.list.setCurrentRow(0)
    assert not page.attention_chat.isHidden()
    page.clear_sensitive()
    assert page.attention_chat.isHidden()


def test_memory_search_and_source_navigation(window, qtbot):
    widget, bridge = window
    page = widget.memory_page
    page.query.setText('报告')
    page._filter(0)
    request = bridge.requests_for('memories')[-1]
    assert request['parameters']['query'] == '报告'
    item = memory_item(); item['source']['session_id'] = 'daily'
    deliver(qtbot, widget, bridge, request, {'items':[item], 'has_more':False})
    tree = page.page.memory_tree
    tree.setCurrentItem(tree.topLevelItem(0))
    assert not page.source_button.isHidden()
    page.source_button.click()
    assert widget.chat_view.conversation_id == 'daily'
    assert widget.stack.currentIndex() == 0
    page.clear_sensitive()
    assert not page.query.text() and page.source_button.isHidden()


def test_search_changes_ignore_earlier_reply(window, qtbot):
    widget, bridge = window
    page = widget.memory_page
    page.query.setText('第一次'); page.refresh()
    request = bridge.requests_for('memories')[-1]
    page.query.setText('第二次'); page.refresh()
    deliver(qtbot, widget, bridge, request, {'items':[memory_item()]})
    assert not page.page.items
    newest = bridge.requests_for('memories')[-1]
    assert newest['parameters']['query'] == '第二次'
    deliver(qtbot, widget, bridge, newest, {'items':[]})
    assert not page.page.items


def test_plugin_tools_link_preserves_provider_filter(window):
    widget, bridge = window
    catalog = widget.catalog_page
    catalog._plugins_loaded({'items':[{'id':'schedule','name':'日程与计时器','tool_ids':['timer_list'], 'page_title':'到期记录'}]})
    catalog.plugins.list.setCurrentRow(0)
    catalog.tabs.setCurrentIndex(1)
    assert catalog.filters.isHidden()
    catalog.show_tools.click()
    assert catalog.provider.currentData() == 'schedule'
    assert catalog.tabs.currentIndex() == 0
    assert bridge.requests_for('tools_catalog')[-1]['parameters']['plugin_id'] == 'schedule'
    catalog.clear_sensitive()
    assert catalog.show_tools.isHidden()


def test_chat_composer_and_compact_avatar_share_rendering(window, qtbot):
    widget, bridge = window
    state = widget.chat_coordinator
    compact = ChatView(state, compact=True); qtbot.addWidget(compact)
    compact.set_conversation('daily')
    state._timeline('daily', {'items':[{'id':'u1','sequence':1,'kind':'user_message','body':'测试消息'},
        {'id':'a1','sequence':2,'kind':'assistant_message','body':'收到'}]}, False)
    custom = QPixmap(24,24); custom.fill(QColor('#e52030'))
    state.appearance.images['user_avatar'] = custom
    state.appearance.image_changed.emit('user_avatar', custom)
    for view in (widget.chat_view, compact):
        label = view.findChild(QLabel, 'message_user_avatar')
        assert label.pixmap().toImage().pixelColor(12, 12).name() == '#e52030'
        assert view.composer.isAncestorOf(view.input)
        assert view.send.maximumWidth() == 72
    assert not any(button.text() == '执行记录' for button in compact.findChildren(PushButton))


def test_narrow_notification_numeric_editor_remains_readable(window, qtbot):
    widget, _ = window
    widget._fit_screen = lambda: None
    widget.set_page(4); widget.settings_page.tabs.setCurrentIndex(2)
    widget.resize(600, 640); widget.show()
    for spin in (widget.settings_page.quiet_start, widget.settings_page.quiet_end):
        qtbot.waitUntil(lambda: spin.lineEdit().width() >= 40)
        assert spin.lineEdit().text() in ('22', '8')


def test_wrapping_action_layout_returns_qt_layout_item(qtbot):
    from PySide6.QtWidgets import QWidget, QLayoutItem
    from deskpal.gui.components import ActionFlowLayout
    host = QWidget(); qtbot.addWidget(host)
    layout = ActionFlowLayout(host); button = PushButton('操作'); layout.addWidget(button)
    item = layout.takeAt(0)
    assert isinstance(item, QLayoutItem) and item.widget() is button
    assert layout.count() == 0 and layout.takeAt(0) is None
