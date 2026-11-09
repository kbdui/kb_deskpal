"""P6：用户入口、上下文与生命周期。全部使用合成数据，无真实模型。"""
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QDialog, QDockWidget, QLabel
from qfluentwidgets import FluentWindow, ComboBox, PushButton, Theme, isDarkTheme

from deskpal.gui.components import ChoiceBox
from deskpal.gui.components import ScrollArea
from deskpal.gui.pet_chat import PetChatWindow
from deskpal.gui.theme import select
from tests.support.window import window, task_review, deliver, set_form


def test_five_routes_and_settings_only_profiles(window, qtbot):
    widget, bridge = window
    assert isinstance(widget, FluentWindow)
    assert len(widget.nav_buttons) == 5
    assert widget.settings_page.isAncestorOf(widget.profiles_button)
    widget.show()
    for index, name in enumerate(('chat', 'arrangements', 'memory', 'tools', 'settings')):
        widget.set_page(index)
        assert widget.stack.currentWidget().objectName() == name
        assert widget.directory_dialog.isHidden()
    assert not widget.findChildren(QDockWidget)


def test_session_directory_is_on_demand_and_draft_survives_navigation(window, qtbot):
    widget, _ = window; widget.show()
    widget.chat_view.input.setPlainText('未发送的专项要求')
    widget.chat_view.title.click()
    qtbot.waitUntil(widget.directory_dialog.isVisible)
    widget.set_page(2)
    assert widget.directory_dialog.isHidden()
    widget.set_page(0)
    assert widget.chat_view.input.toPlainText() == '未发送的专项要求'
    assert widget.chat_view.conversation_row.isHidden()


def test_sidebar_narrow_view_returns_to_chat_and_releases_subscription(window, qtbot):
    widget, _ = window; widget.show()
    widget.chat_coordinator.set_review('task-1', task_review())
    widget._open_tool_results('task-1')
    assert widget.tool_results_dock.isVisible()
    widget.resize(720, 700); widget._responsive()
    assert widget.stack.isHidden()
    widget._clear_tool_results()
    assert not widget.stack.isHidden()
    assert not widget._tool_results_task
    assert widget.tool_results_dock.isHidden()


def test_latest_execution_waits_for_real_review(window, qtbot):
    widget, bridge = window
    widget.chat_coordinator.timelines['daily'] = [{'task_id': 'latest'}]
    widget.chat_view.task_id = 'previously-located-task'
    widget._latest_tool_results()
    widget._update_tool_results('latest')  # 请求尚未返回，不能丢掉订阅。
    assert widget._tool_results_task == 'latest'
    widget.chat_coordinator.set_review('latest', task_review('latest'))
    assert not widget.tool_results_dock.isHidden()


def test_compact_chat_has_no_execution_or_settings_actions(window, qtbot):
    widget, _ = window
    compact = PetChatWindow(widget.chat_coordinator); qtbot.addWidget(compact)
    texts = [button.text() for button in compact.findChildren(PushButton)]
    assert '展开' in texts
    assert not any('执行记录' in text or '人格' in text or '角色档案' in text for text in texts)


def test_memory_display_groups_request_backend_query_and_preserve_latest_choice(window, qtbot):
    widget, bridge = window; page = widget.memory_page
    page.refresh()
    first = bridge.requests_for('memories')[-1]
    assert first['parameters']['group'] == 'facts'
    page.filter.setCurrentIndex(page.filter.findData('buffer'))
    deliver(qtbot, widget, bridge, first, {'items': [{'id': 'old', 'draft': {'content': '旧分组'}}]})
    latest = bridge.requests_for('memories')[-1]
    assert latest['parameters']['group'] == 'buffer' and latest['parameters']['offset'] == 0
    deliver(qtbot, widget, bridge, latest, {'items': []})
    assert not page.page.items


def test_memory_selection_is_readonly_and_empty_selection_clears_details(window):
    widget, bridge = window; page = widget.memory_page
    page._selected({'id':'m1', 'draft':{'content':'可读记忆'}})
    assert page.page.summary_body.toPlainText() == '可读记忆'
    page._selected({})
    assert page.page.summary_title.text() == '选择一条记录查看详情。'
    assert not page.page.summary_body.toPlainText()
    assert not hasattr(page, 'create_memory') and not hasattr(page, 'forms')
    assert not bridge.requests_for('memory')


def test_learning_records_readable_and_erased_on_privacy(window, qtbot):
    widget, bridge = window; page = widget.memory_page
    page.learning_button.click()
    request = bridge.requests_for('learning_records')[-1]
    deliver(qtbot, widget, bridge, request, {'items': [{'action': 'retain', 'understanding': '倾诉时先听',
        'reason': '用户明确表达', 'uncertainty': '仅针对这次倾诉', 'created_at': '2026-10-31T08:00:00Z'}]})
    dialog = page.findChild(QDialog)
    assert dialog is not None
    texts = '\n'.join(label.text() for label in dialog.findChildren(QLabel))
    assert '用户明确表达' in texts and '仅针对这次倾诉' in texts
    page.clear_sensitive()
    assert dialog.isHidden()
    assert all(not label.text() for label in dialog.findChildren(QLabel))


def test_arrangements_tabs_and_paused_filter(window):
    widget, bridge = window; page = widget.arrangements
    assert [page.tabs.tabText(i) for i in range(3)] == ['日程与提醒', '任务进度', '后续关注']
    assert page.task_tabs.widget(1) is page.goals
    page.task_filter.setCurrentIndex(page.task_filter.findData('Paused'))
    assert bridge.requests_for('tasks')[-1]['parameters']['statuses'] == ['Paused']


def test_notification_control_is_in_settings_and_uses_real_receipt(window, qtbot):
    widget, bridge = window; page = widget.settings_page
    policy = {'chat': {'paused': 0}, 'reminder': {'paused': 1}}
    page._policy_loaded(policy); page.chat_pause.click()
    request = bridge.requests_for('notification_control')[-1]
    assert request['parameters']['kind'] == 'chat' and request['parameters']['paused'] is True
    assert request['parameters']['command_id']
    deliver(qtbot, widget, bridge, request, {'chat': {'paused': 1}, 'reminder': {'paused': 1}})
    assert page.chat_pause.text() == '恢复主动聊天'


def test_choice_value_and_wheel_do_not_change_selection(window):
    widget, _ = window
    choice = ChoiceBox(widget); choice.addItem('甲', 'a'); choice.addItem('乙', {'id': 'b'})
    choice.setCurrentIndex(1)
    event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, 120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(choice, event)
    assert isinstance(choice, ComboBox) and choice.currentData() == {'id': 'b'}


def test_theme_changes_business_surface_with_controls(window, qtbot):
    widget, _ = window
    try:
        select('dark')
        assert isDarkTheme()
        assert '#191b24' in widget.styleSheet()
        select('light')
        assert not isDarkTheme()
        assert '#f5f6fa' in widget.styleSheet()
    finally:
        select('auto')


def test_schedule_selection_has_readable_time_and_prepares_draft_without_execution(window, qtbot):
    widget, bridge = window; page = widget.arrangements
    page.schedules.set_page({'items': [dict(id='schedule', source='calendar', title='阅读计划',
        status='scheduled', start_at='2026-11-01T08:00:00+08:00')]})
    assert page.schedules.list.count() == 2  # 日期标题不是一个可操作的日程。
    page.schedules.list.setCurrentRow(1)
    assert page.schedules.selected_item()['id'] == 'schedule'
    assert '2026年11月01日' in page.schedules.summary_meta.text()
    page.schedule_edit.click()
    assert '阅读计划' in widget.chat_view.input.toPlainText()
    assert widget.stack.currentIndex() == 0
    assert not bridge.requests_for('submit')


def test_repeated_page_scroll_does_not_change_filters_or_crash(window, qtbot):
    widget, _ = window; widget.show()
    page = widget.memory_page
    page.page.set_page({'items': [dict(id=f'm{i}', status='accepted', revision=1,
        draft=dict(content=f'可读事实 {i}'), source={}) for i in range(100)]})
    views = [(2, page.page.memory_tree), (3, widget.catalog_page.tools.list), (1, widget.arrangements.schedules.list)]
    for index, view in views:
        widget.set_page(index)
        for delta in [-120] * 25 + [120] * 25:
            event = QWheelEvent(QPointF(8, 8), QPointF(8, 8), QPoint(), QPoint(0, delta),
                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(view.viewport(), event)
            QApplication.processEvents()
    assert page.filter.currentData() == 'facts'
    assert page.page.memory_tree.topLevelItemCount() == 100


def test_goal_browser_drops_old_children_when_switching_goal(window, qtbot):
    widget, bridge = window; goals = widget.arrangements.goals
    goals._goals({'items':[{'id':'g1','title':'第一目标'}, {'id':'g2','title':'第二目标'}]})
    first = bridge.requests_for('milestones')[-1]
    goals.parent_goal.setCurrentIndex(1)
    deliver(qtbot, widget, bridge, first, {'items':[{'id':'old','title':'旧目标步骤'}]})
    assert not goals.pages['milestone'].items
    current = bridge.requests_for('milestones')[-1]
    assert current['parameters']['goal_id'] == 'g2'
    deliver(qtbot, widget, bridge, current, {'items':[{'id':'new','title':'新目标步骤'}]})
    assert goals.pages['milestone'].items[0]['id'] == 'new'
    assert not bridge.requests_for('create_goal')


def test_closed_learning_dialog_drops_late_reply(window, qtbot):
    widget, bridge = window; page = widget.memory_page
    page.learning_button.click()
    request = bridge.requests_for('learning_records')[-1]
    dialog = page.findChild(QDialog); dialog.close()
    qtbot.wait(1)
    deliver(qtbot, widget, bridge, request, {'items': [{'understanding': '不应写进已经销毁的控件'}]})
    assert not page.findChildren(QDialog)


def test_fluent_choice_wheel_scrolls_parent_without_changing_option(window, qtbot):
    from PySide6.QtWidgets import QWidget, QVBoxLayout
    widget, _ = window
    area = ScrollArea(); qtbot.addWidget(area)
    host = QWidget(); layout = QVBoxLayout(host); choice = ChoiceBox()
    choice.addItem('一', 1); choice.addItem('二', 2); layout.addWidget(choice)
    host.setMinimumHeight(1200); area.setWidget(host); area.setWidgetResizable(True)
    area.resize(300, 240); area.show()
    event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(choice, event)
    qtbot.waitUntil(lambda: area.verticalScrollBar().value() > 0)
    assert choice.currentData() == 1


def test_memory_latest_revision_updates_readonly_details(window):
    widget, bridge = window; page = widget.memory_page
    original = dict(id='m', revision=1, status='accepted', draft={'content':'原先理解'})
    page.page.set_page({'items':[original]}); page.page._select_memory_id('m')
    page.page.set_page({'items':[{**original, 'revision':2, 'draft':{'content':'后台修订'}}]})
    assert page.page.summary_body.toPlainText() == '后台修订'
    assert not bridge.requests_for('memory')


def test_goal_latest_revision_updates_readonly_details(window):
    widget, bridge = window; page = widget.arrangements.goals.pages['goal']
    original = dict(id='g', revision=1, status='Active', title='阅读目标')
    page.set_page({'items':[original]}); page.list.setCurrentRow(0)
    page.set_page({'items':[{**original, 'revision':2, 'title':'后台修订'}]})
    assert page.selected_item()['revision'] == 2
    assert page.summary_title.text() == '后台修订'
    assert not bridge.requests_for('update_goal')


def test_narrow_record_detail_can_return_to_list_and_reopen_same_record(window, qtbot):
    widget, _ = window; widget.resize(720, 700); widget.show(); widget.set_page(2)
    page = widget.memory_page.page
    page.set_page({'items': [dict(id='m1', status='accepted', revision=1, draft={'content':'可读正文'})]})
    page._select_memory_id('m1')
    qtbot.waitUntil(lambda: page.memory_tree.isHidden())
    assert page.back_to_list.isVisible() and page.summary_body.isVisible()
    page.back_to_list.click()
    qtbot.waitUntil(page.memory_tree.isVisible)
    row = page.memory_tree.topLevelItem(0)
    page._reopen_detail(row, 0)
    qtbot.waitUntil(lambda: page.memory_tree.isHidden())
    assert page.selected_item()['id'] == 'm1'


def test_readonly_goal_has_no_draft_but_avatar_preview_blocks_handoff(window):
    widget, _ = window; page = widget.arrangements.goals
    page.pages['goal'].set_page({'items':[{'id':'g1','title':'阅读目标'}]})
    assert not page.has_unsaved_content()
    widget.settings_page.avatars.token = 'prepared-avatar'
    assert widget.has_unsaved_content()
    widget.settings_page.avatars._clear()
    assert not page.has_unsaved_content()
