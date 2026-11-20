"""生产WebEngine侧栏：合成记录、实际排版/键鼠事件，无后台及模型。"""
import copy
import json
import os

os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')

import pytest
from PySide6.QtCore import QPoint, QSettings, Qt

from deskpal.gui.preferences import GuiPreferences
from deskpal.gui.studio_surface import StudioSurface
from deskpal.gui.tool_results import studio_operation_records
from tests.support.studio import evaluate


def evaluate_object(qtbot, widget, script):
    """显式通过JSON传递对象，兼容Qt绑定对JS对象的返回差异。"""
    return json.loads(evaluate(qtbot, widget, f'JSON.stringify({script})'))


def synthetic_review():
    evidence = dict(type='memory_evidence', content='小鱼想尝试种一株植物。' * 35,
        status='candidate', source_origin='character_reflection', source_time='2026-11-16T07:00:00+00:00',
        uncertainty='这是角色的设想，不代表已经实施。', reason='来自已保存的自主思考')
    results = [dict(tool_name='memory_search', call_id='lookup', success=True, outcome='known',
                    output=dict(items=[dict(id='synthetic-memory', content=json.dumps(evidence, ensure_ascii=False))])),
               dict(tool_name='reflection_record', call_id='thought', success=True, outcome='known',
                    output=dict(topic='关于自己的兴趣', content='先从观察植物开始。\n<script>bad()</script>', reason='尚未选择具体植物'))]
    return dict(status='Failed', title='回头想想自己的兴趣与还没有完成的打算。' * 8,
        task=dict(error='synthetic AttributeError', result=dict(tool_results=results)))


@pytest.fixture
def surface(qtbot, tmp_path):
    prefs = GuiPreferences(QSettings(str(tmp_path / 'prefs.ini'), QSettings.Format.IniFormat))
    widget = StudioSurface()
    qtbot.addWidget(widget)
    attached = []
    review = synthetic_review()
    chat = dict(id='daily', default='daily', available=True, modelReady=True, canSend=True,
        items=[], reviews={'task': dict(review=review, records=studio_operation_records(review))})
    def attach():
        attached.append(True)
        widget.port.publish('init', dict(mode='main', lifecycle='Ready', prefs=dict(execution_width=prefs.execution_width)))
        widget.port.publish('chat', chat)
    def action(name, args):
        if name == 'preference' and args.get('key') == 'execution_width':
            assert prefs.set_execution_width(args['value'])
            widget.port.publish('prefs', dict(execution_width=prefs.execution_width))
    widget.port.attached.connect(attach)
    widget.port.action.connect(action)
    widget.resize(1400, 820)
    widget.show()
    widget.load_studio()
    qtbot.waitUntil(lambda: bool(attached), timeout=15000)
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "!!document.querySelector('[data-action=execution]')"), timeout=10000)
    yield widget, prefs, chat, attached
    widget.release_studio()


def open_records(qtbot, widget):
    evaluate(qtbot, widget, "document.querySelector('[data-action=execution]').click();true")
    widget.port.publish('record', dict(id='task'))
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "document.querySelector('.ds-execution-body')?.textContent.includes('关于自己的兴趣')"))


def test_planning_failure_notice_routes_to_records_only_in_main_window(surface, qtbot):
    widget, _, chat, _ = surface
    received = []
    widget.port.action.connect(lambda name, args: received.append((name, args)))
    notice = dict(kind='system_notice', task_id='task', body='已保留工具执行记录。', has_execution_record=True)
    widget.port.publish('chat', {**chat, 'items': [notice]})
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "!!document.querySelector('.ds-chat-notice [data-record]')"))
    assert evaluate(qtbot, widget, "document.querySelector('.ds-chat-notice').textContent.includes('系统提示')")
    evaluate(qtbot, widget, "document.querySelector('.ds-chat-notice [data-record]').click();true")
    qtbot.waitUntil(lambda: ('records', {'id': 'task'}) in received)
    widget.port.publish('init', dict(mode='pet', lifecycle='Ready', prefs={}))
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "document.querySelector('.ds-chat-notice')?.textContent.includes('主窗口')===true"))
    assert evaluate(qtbot, widget, "!document.querySelector('.ds-chat-notice [data-record]')")


def test_readable_results_and_raw_data_are_separate(surface, qtbot):
    widget, _, _, _ = surface
    open_records(qtbot, widget)
    info = evaluate_object(qtbot, widget, """(()=>{
      const pane=document.querySelector('.ds-execution');
      const raw=pane.querySelector('.ds-execution-raw');
      const first=pane.querySelector('.ds-execution-step');
      first.open=true;
      return {text:pane.textContent,closed:!raw.closest('details').open,
        memories:pane.querySelectorAll('.ds-execution-memory').length,
        injected:pane.querySelectorAll('script').length,
        overflow:pane.querySelector('.ds-execution-body').scrollWidth-pane.querySelector('.ds-execution-body').clientWidth};})()""")
    assert '检索记忆' in info['text'] and '思考已保存 · 收尾未完成' in info['text']
    assert '找到 1 条相关记忆' in info['text'] and info['memories'] == 1
    assert info['closed'] and info['injected'] == 0 and info['overflow'] <= 1


def test_mouse_and_keyboard_resize_persist_and_reload(surface, qtbot):
    widget, prefs, _, attached = surface
    open_records(qtbot, widget)
    pos = evaluate_object(qtbot, widget, "(()=>{const r=document.querySelector('[data-execution-resize]').getBoundingClientRect();return {x:r.x+3,y:r.y+90};})()")
    # WebEngine把原生输入交给渲染子控件；等待异步派发到页面后再继续手势。
    target = widget.view.focusProxy() or widget.view
    qtbot.mousePress(target, Qt.MouseButton.LeftButton, pos=QPoint(round(pos['x']), round(pos['y'])))
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, 'executionDrag!==null'))
    qtbot.mouseMove(target, QPoint(round(pos['x']-60), round(pos['y'])))
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, 'state.executionWidth>=470'))
    qtbot.mouseRelease(target, Qt.MouseButton.LeftButton, pos=QPoint(round(pos['x']-60), round(pos['y'])))
    qtbot.waitUntil(lambda: prefs.execution_width >= 470)
    width = prefs.execution_width
    evaluate(qtbot, widget, "const h=document.querySelector('[data-execution-resize]');h.focus();h.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));true")
    qtbot.waitUntil(lambda: prefs.execution_width == width-20)
    expected = prefs.execution_width
    count = len(attached)
    widget.load_studio()
    qtbot.waitUntil(lambda: len(attached) > count, timeout=15000)
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "!!document.querySelector('[data-action=execution]')"))
    open_records(qtbot, widget)
    actual = evaluate(qtbot, widget, "document.querySelector('.ds-execution').getBoundingClientRect().width")
    assert abs(actual-expected) <= 1


def test_narrow_drawer_and_expand_leave_chat_usable(surface, qtbot):
    widget, _, _, _ = surface
    open_records(qtbot, widget)
    evaluate(qtbot, widget, "document.querySelector('[data-action=execution-expand]').click();true")
    assert evaluate(qtbot, widget, "document.querySelector('.ds-execution').getBoundingClientRect().width") >= 850
    evaluate(qtbot, widget, "document.querySelector('[data-action=execution-expand]').click();true")
    widget.resize(620, 720)
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "document.querySelector('#deskpal-studio').dataset.executionOverlay==='true'"))
    values = evaluate_object(qtbot, widget, """(()=>{const pane=document.querySelector('.ds-execution'),layout=document.querySelector('.ds-chat-layout'),chat=document.querySelector('.ds-chat-center');
      return {pane:pane.clientWidth,available:layout.clientWidth,chat:chat.clientWidth,
        overflow:pane.querySelector('.ds-execution-body').scrollWidth-pane.querySelector('.ds-execution-body').clientWidth,
        handle:getComputedStyle(pane.querySelector('[data-execution-resize]')).display};})()""")
    assert values['pane'] <= values['available'] and abs(values['chat']-values['available']) <= 1
    assert values['handle'] == 'none' and values['overflow'] <= 1
    evaluate(qtbot, widget, "document.querySelector('.ds-execution-scrim').click();true")
    assert evaluate(qtbot, widget, "!document.querySelector('.ds-execution')")


def test_refresh_preserves_open_details_and_scroll_and_pet_hides_sidebar(surface, qtbot):
    widget, _, chat, _ = surface
    open_records(qtbot, widget)
    evaluate(qtbot, widget, "document.querySelector('.ds-execution-step').open=true;true")
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "state.executionDetails.size>0"))
    evaluate(qtbot, widget, "document.querySelector('.ds-execution-memory').open=true;document.querySelector('.ds-execution-body').scrollTop=100;true")
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "state.executionDetails.size>=3"))
    before = evaluate(qtbot, widget, "document.querySelector('.ds-execution-body').scrollTop")
    widget.port.publish('chat', chat)
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "document.querySelector('.ds-execution-memory')?.open"))
    assert abs(evaluate(qtbot, widget, "document.querySelector('.ds-execution-body').scrollTop")-before) <= 1
    widget.port.publish('init', dict(mode='pet', lifecycle='Ready', prefs={}))
    qtbot.waitUntil(lambda: evaluate(qtbot, widget, "!document.querySelector('.ds-execution')"))
    assert evaluate(qtbot, widget, "!document.querySelector('[data-action=execution]')")


def test_preferences_validate_width_and_result_projection_keeps_facts(qapp, tmp_path):
    settings = QSettings(str(tmp_path / 'prefs.ini'), QSettings.Format.IniFormat)
    prefs = GuiPreferences(settings)
    assert prefs.execution_width == 420
    for bad in [True, 0, 339, 721, 500.5, '500']:
        assert not prefs.set_execution_width(bad)
    assert prefs.set_execution_width(600)
    assert GuiPreferences(settings).execution_width == 600
    settings.setValue('gui/execution_width', 'invalid')
    assert prefs.execution_width == 420
    review = synthetic_review()
    original = copy.deepcopy(review)
    records = studio_operation_records(review)
    assert records[0]['display_name'] == '检索记忆'
    assert records[1]['display_name'] == '记录自主思考'
    assert review == original and all(row['result']['success'] is True for row in records)
