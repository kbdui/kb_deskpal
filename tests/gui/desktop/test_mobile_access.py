"""手机 HTTP→共享 bridge→实际临时后台，以及关闭/撤销屏障。"""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
from types import SimpleNamespace

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from shiboken6 import delete

from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.gui.bridge import ApplicationBridge
from deskpal.gui.preferences import GuiPreferences
from deskpal.gui.studio_window import StudioWindow
from deskpal.planner.models import Plan
from deskpal.tasks.models import TaskStatus
from deskpal.tools.models import ToolCall
from tests.integration.mobile.test_http import unused_port, request, pair, command
from tests.support.studio import evaluate as studio_evaluate


def evaluate(qtbot, surface, script):
    page = surface.page() if callable(surface.page) else surface.page
    return studio_evaluate(qtbot, SimpleNamespace(page=page), script)


def config(tmp_path):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline", "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
                               "DESKPAL_DATA_DIR": str(tmp_path / "profile")})


def start(qtbot, tmp_path, *, planner=None):
    bridge = ApplicationBridge(lambda: create_application(config(tmp_path), planner=planner))
    bridge.configure_mobile(tmp_path / "application-mobile.sqlite3", {"user": "我", "assistant": "小猫"})
    replies = []
    bridge.reply.connect(replies.append)
    bridge.start()
    qtbot.waitUntil(lambda: bridge.state == "Ready", timeout=10000)
    request_id = bridge.send("mobile_enable", {"bind": "127.0.0.1", "port": unused_port(), "public_origin": ""})
    qtbot.waitUntil(lambda: any(item["request_id"] == request_id for item in replies))
    assert next(item for item in replies if item["request_id"] == request_id)["ok"]
    return bridge, replies


def stop(qtbot, bridge):
    bridge.shutdown()
    qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=10000)


def http_rpc(qtbot, executor, runtime, cookie, session, operation, parameters):
    future = executor.submit(request, runtime, "/api/command", command(runtime, session, operation, parameters),
                             cookie=cookie, headers={"X-Deskpal-CSRF": session["csrf"]})
    qtbot.waitUntil(future.done, timeout=10000)
    status, body, _ = future.result()
    assert status == 200, body
    return body["data"]


def test_http_uses_same_application_and_submission_is_durable_and_idempotent(qtbot, tmp_path):
    bridge, _ = start(qtbot, tmp_path)
    try:
        runtime = bridge._mobile
        cookie, session, _ = pair(runtime)
        with ThreadPoolExecutor(max_workers=2) as executor:
            rpc = lambda op, params={}: http_rpc(qtbot, executor, runtime, cookie, session, op, params)
            directory = rpc("conversation_list")
            identity = directory["default_id"]
            timeline = rpc("conversation_timeline", {"id": identity})
            parameters = {"request": "合成手机请求", "conversation_id": identity,
                          "expected_conversation_epoch": timeline["epoch"], "command_id": "mobile-command-one"}
            first = rpc("submit", parameters)
            second = rpc("submit", parameters)
            assert first["task_id"] == second["task_id"]
            qtbot.waitUntil(lambda: bridge._app.tasks.get(first["task_id"]).status == TaskStatus.COMPLETED, timeout=10000)
            assert len(bridge._app.tasks.list_tasks()) == 1
            assert rpc("command_status", {"command_id": "mobile-command-one"})["accepted"]
            actual = bridge._app.conversation_timeline(identity)
            assert sum(item["kind"] == "user_message" for item in actual["items"]) == 1
            assert any(item["kind"] == "assistant_message" and item["body"] for item in actual["items"])
            created = rpc("conversation_create", {"title": "专项测试", "command_id": "mobile-create-one"})
            assert created["id"] != identity
    finally:
        stop(qtbot, bridge)
    assert not runtime.accepting
    assert bridge._app.closed


class FilePlanner:
    def __init__(self, output=None):
        self.calls = 0
        self.output = output

    def plan_context(self, request, **kwargs):
        self.calls += 1
        if (self.output is not None and self.output.exists()) or (self.output is None and self.calls > 1):
            return Plan("写入完成。")
        return self.plan(request)

    def plan(self, request, *, memories=(), tools=()):
        return Plan("write", (ToolCall("write_text", {"path": "mobile-test.txt", "text": "合成内容"}),))


def test_mobile_confirmation_uses_actual_plan_revision_and_permission_checks(qtbot, tmp_path):
    bridge, _ = start(qtbot, tmp_path, planner=FilePlanner(tmp_path / "workspace" / "mobile-test.txt"))
    try:
        runtime = bridge._mobile
        cookie, session, _ = pair(runtime)
        with ThreadPoolExecutor(max_workers=2) as executor:
            rpc = lambda op, params={}: http_rpc(qtbot, executor, runtime, cookie, session, op, params)
            daily = rpc("conversation_list")["items"][0]
            task = rpc("submit", {"request": "写入合成文件", "conversation_id": daily["id"], "expected_conversation_epoch": daily["epoch"], "command_id": "write-once"})
            task_id = task["task_id"]
            qtbot.waitUntil(lambda: bridge._app.tasks.get(task_id).status == TaskStatus.WAITING, timeout=10000)
            assert not (tmp_path / "workspace" / "mobile-test.txt").exists()
            paused = rpc("task_pause", {"task_id": task_id, "command_id": "pause-once"})
            assert paused["status"] == "Paused"
            first_amendment = rpc("task_append_requirement", {"task_id": task_id, "command_id": "append-once", "content": "保留原来的文件要求。"})
            second_amendment = rpc("task_append_requirement", {"task_id": task_id, "command_id": "append-once", "content": "保留原来的文件要求。"})
            assert first_amendment["accepted"] and first_amendment["sequence"] == second_amendment["sequence"]
            rpc("task_continue", {"task_id": task_id, "command_id": "continue-once"})
            qtbot.waitUntil(lambda: bridge._app.tasks.get(task_id).status == TaskStatus.WAITING, timeout=10000)
            review = rpc("task_review", {"task_id": task_id})
            assert "confirm" in review["available_actions"]
            rpc("confirm", {"task_id": task_id, "command_id": "grant-once", "expected_revision": review["task_revision"],
                            "plan_hash": review["plan_hash"], **review["missing_permissions"]})
            qtbot.waitUntil(lambda: bridge._app.tasks.get(task_id).status == TaskStatus.COMPLETED, timeout=10000)
            assert (tmp_path / "workspace" / "mobile-test.txt").read_text(encoding="utf-8") == "合成内容"
            rejected = rpc("task_append_requirement", {"task_id": task_id, "command_id": "too-late", "content": "任务结束后不应声称已采纳。"})
            assert rejected["accepted"] is False
    finally:
        stop(qtbot, bridge)


def test_revoked_queued_request_never_reaches_facade_and_shutdown_unblocks_waiter(qtbot, tmp_path):
    from tests.gui.desktop.test_bridge import FakeApplication, FakeFacade
    app = FakeApplication()
    facade = FakeFacade(app)
    bridge = ApplicationBridge(lambda: app, facade_factory=lambda _: facade)
    bridge.start()
    qtbot.waitUntil(lambda: bridge.state == "Ready")
    facade.block = "block"
    bridge.send("block")
    qtbot.waitUntil(facade.entered.is_set)
    allowed = Event()
    allowed.set()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(bridge.call_remote, "conversation_create", {"title": "不能创建", "command_id": "revoked"}, allowed.is_set)
        qtbot.waitUntil(lambda: len(bridge._remote_waiters) == 1)
        allowed.clear()
        facade.release.set()
        qtbot.waitUntil(future.done)
        assert future.result()["ok"] is False
        assert not any(operation == "conversation_create" for operation, _ in facade.calls)
    # 再阻塞一条命令，退出必须立即解除 HTTP 等待，并阻止排队请求执行。
    facade.entered.clear()
    facade.release.clear()
    bridge.send("block")
    qtbot.waitUntil(facade.entered.is_set)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(bridge.call_remote, "conversation_create", {"title": "退出后不能创建", "command_id": "closing"}, lambda: True)
        qtbot.waitUntil(lambda: len(bridge._remote_waiters) == 1)
        bridge.shutdown()
        qtbot.waitUntil(future.done)
        assert future.result()["ok"] is False
        facade.release.set()
        qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=10000)
    assert not any(operation == "conversation_create" for operation, _ in facade.calls)


def test_mobile_browser_pair_chat_reload_scroll_and_desktop_settings(qtbot, tmp_path):
    bridge, _ = start(qtbot, tmp_path)
    view = QWebEngineView()
    profile = QWebEngineProfile(view)
    page = QWebEnginePage(profile, view)
    view.setPage(page)
    window = None
    try:
        runtime = bridge._mobile
        view.resize(390, 740)
        view.show()
        view.load(QUrl(f"http://127.0.0.1:{runtime.config['port']}"))
        qtbot.waitUntil(lambda: evaluate(qtbot, view, "typeof S!=='undefined' && !document.querySelector('#login').hidden"), timeout=10000)
        code = runtime.local("mobile_pair", {})["pairing"]["code"]
        evaluate(qtbot, view, f"document.querySelector('[name=code]').value='{code}';document.querySelector('#pair-form').requestSubmit();true")
        qtbot.waitUntil(lambda: evaluate(qtbot, view, "typeof S!=='undefined' && S.loaded && S.epoch!==null"), timeout=10000)
        evaluate(qtbot, view, "document.querySelector('#input').value='浏览器合成聊天';document.querySelector('#compose').requestSubmit();true")
        qtbot.waitUntil(lambda: len(bridge._app.tasks.list_tasks()) == 1, timeout=10000)
        qtbot.waitUntil(lambda: bridge._app.tasks.list_tasks()[0].status == TaskStatus.COMPLETED, timeout=10000)
        evaluate(qtbot, view, "S.lastSession=0;poll();true")
        qtbot.waitUntil(lambda: evaluate(qtbot, view, "document.querySelector('#messages').textContent.includes('浏览器合成聊天')"), timeout=10000)
        assert evaluate(qtbot, view, "getComputedStyle(document.querySelector('.message.user .author')).justifyContent") == "flex-end"
        assert evaluate(qtbot, view, "document.documentElement.scrollWidth<=innerWidth")
        # 用足够长的合成记录检查滚动锚点；输入不会重建聊天容器。
        evaluate(qtbot, view, "S.items.push(...Array.from({length:30},(_,i)=>({id:'synthetic-'+i,sequence:100+i,kind:'assistant_message',body:'滚动验证。'.repeat(20)})));render(true);document.querySelector('#thread').scrollTop=200;render();true")
        assert evaluate(qtbot, view, "document.querySelector('#thread').scrollTop") == 200
        assert evaluate(qtbot, view, "document.querySelector('#bottom').hidden") is False
        # 有任务时默认追加，用户可明确切回新消息；跟进无需额外创建任务。
        evaluate(qtbot, view, "S.reviews={fake:{task_id:'fake',status:'Running',title:'合成工作'}};S.targetChosen=false;render();true")
        assert evaluate(qtbot, view, "S.target") == "fake"
        assert evaluate(qtbot, view, "document.querySelector('#input').placeholder.includes('补充')")
        evaluate(qtbot, view, "window.savedRpc=rpc;rpc=async(op,p)=>op==='command_status'?{accepted:false}:op==='task_append_requirement'?{accepted:false,reason:'任务已收尾，输入已保留。'}:window.savedRpc(op,p);document.querySelector('#input').value='不能丢失的跟进内容';perform('task_append_requirement',{task_id:'fake',content:'不能丢失的跟进内容'});true")
        qtbot.waitUntil(lambda: evaluate(qtbot, view, "!S.busy && !S.pending"))
        assert evaluate(qtbot, view, "document.querySelector('#input').value") == "不能丢失的跟进内容"
        evaluate(qtbot, view, "rpc=window.savedRpc;true")
        view.reload()
        qtbot.waitUntil(lambda: evaluate(qtbot, view, "typeof S!=='undefined' && S.loaded && S.epoch!==null"), timeout=10000)
        assert len(bridge._app.tasks.list_tasks()) == 1
        preferences = GuiPreferences(QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat))
        window = StudioWindow(bridge, preferences)
        window.show()
        qtbot.waitUntil(lambda: window.attached, timeout=10000)
        window.show_settings(5)
        qtbot.waitUntil(lambda: "mobile" in window._data, timeout=10000)
        assert evaluate(qtbot, window, "!!document.querySelector('form[data-form=mobile-config]')")
        assert evaluate(qtbot, window, "document.querySelector('.ds-setting-content').textContent.includes('手机访问')")
        original_port = runtime.config["port"]
        evaluate(qtbot, window, "const portInput=document.querySelector('[name=port]');portInput.value='8766';portInput.dispatchEvent(new Event('input',{bubbles:true}));render();true")
        assert evaluate(qtbot, window, "document.querySelector('[name=port]').value") == "8766"
        assert runtime.config["port"] == original_port
        evaluate(qtbot, window, "state.mobileDraft=null;render();true")
        evaluate(qtbot, window, "window.mobilePainted=false;requestAnimationFrame(()=>requestAnimationFrame(()=>window.mobilePainted=true));true")
        qtbot.waitUntil(lambda: evaluate(qtbot, window, "window.mobilePainted"))
        qtbot.wait(100)
        view.grab().save(str(tmp_path / "mobile-390.png"))
        window.grab().save(str(tmp_path / "desktop-mobile-settings.png"))
        evaluate(qtbot, view, "document.querySelector('#logout').click();true")
        qtbot.waitUntil(lambda: evaluate(qtbot, view, "!document.querySelector('#login').hidden"))
        code = runtime.local("mobile_pair", {})["pairing"]["code"]
        evaluate(qtbot, view, f"document.querySelector('[name=code]').value='{code}';document.querySelector('#pair-form').requestSubmit();true")
        qtbot.waitUntil(lambda: evaluate(qtbot, view, "!!S.session && S.loaded"), timeout=10000)
        assert evaluate(qtbot, view, "document.querySelectorAll('#directory [data-session]').length>0")
    finally:
        if window is not None:
            window.release_studio()
            window._allow_close = True
            window.close()
            delete(window)
        delete(page)
        delete(profile)
        view.close()
        delete(view)
        stop(qtbot, bridge)


def test_desktop_mobile_buttons_enable_pair_revoke_and_disable(qtbot, tmp_path):
    """走实际 WebChannel 和 Channel，不绕过设置页直接调用 bridge。"""
    bridge = ApplicationBridge(lambda: create_application(config(tmp_path)))
    bridge.configure_mobile(tmp_path / "access.sqlite3", {})
    replies = []
    bridge.reply.connect(replies.append)
    bridge.start()
    qtbot.waitUntil(lambda: bridge.state == "Ready", timeout=10000)
    preferences = GuiPreferences(QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat))
    window = StudioWindow(bridge, preferences)

    def click(script, operation):
        previous = len(replies)
        assert evaluate(qtbot, window, script + ";true")
        qtbot.waitUntil(lambda: any(item["operation"] == operation for item in replies[previous:]), timeout=10000)
        receipt = next(item for item in replies[previous:] if item["operation"] == operation)
        assert receipt["ok"], receipt
        qtbot.waitUntil(lambda: not window.operations.pending, timeout=10000)
        return receipt["data"]

    try:
        window.show()
        qtbot.waitUntil(lambda: window.attached, timeout=10000)
        window.show_settings(5)
        qtbot.waitUntil(lambda: "mobile" in window._data, timeout=10000)
        port = unused_port()
        click(f"document.querySelector('[name=port]').value='{port}';document.querySelector('[data-form=mobile-config]').requestSubmit()", "mobile_enable")
        runtime = bridge._mobile
        assert runtime.accepting and runtime.config["port"] == port
        # 只监听回环时明确提示仅本机；多网卡默认折叠，不能冒充可用手机入口。
        assert evaluate(qtbot, window, "document.querySelector('.ds-setting-content').textContent.includes('手机无法通过此地址连接电脑')")
        evaluate(qtbot, window, "window.savedMobileAccess=state.data.mobile;state.data.mobile={enabled:true,configuration:{bind:'0.0.0.0'},addresses:['http://127.0.0.1:8765','http://10.0.0.2:8765','http://192.168.66.247:8765','http://198.18.0.1:8765']};render();true")
        assert evaluate(qtbot, window, "document.querySelector('.ds-access-addresses').open") is False
        assert evaluate(qtbot, window, "document.querySelector('.ds-access-addresses .ds-access-url').checkVisibility()") is False
        assert evaluate(qtbot, window, "document.querySelector('.ds-setting-content').textContent.includes('3 个候选地址')")
        evaluate(qtbot, window, "document.querySelector('.ds-access-addresses summary').click();true")
        assert evaluate(qtbot, window, "document.querySelector('.ds-access-addresses').open")
        assert evaluate(qtbot, window, "document.querySelector('.ds-access-addresses .ds-setting-row').textContent.includes('手机请勿使用')")
        # 局域网模式不能把保留的 FRP 地址显示成当前入口，折叠区也不应混入。
        evaluate(qtbot, window, "state.data.mobile.configuration.public_origin='https://mobile.example.com';state.data.mobile.addresses.push('https://mobile.example.com');render();true")
        assert evaluate(qtbot, window, "[...document.querySelectorAll('.ds-access-url')].some(el=>el.textContent.includes('mobile.example.com'))") is False
        assert evaluate(qtbot, window, "document.querySelector('[name=public_origin]').type") == "hidden"
        assert evaluate(qtbot, window, "document.querySelector('[name=public_origin]').value") == "https://mobile.example.com"
        # 切回 FRP 时恢复展示上次地址，不丢配置。
        evaluate(qtbot, window, "document.querySelector('[name=bind]').value='127.0.0.1';document.querySelector('[name=bind]').dispatchEvent(new Event('change',{bubbles:true}));true")
        assert evaluate(qtbot, window, "document.querySelector('[name=public_origin]').type") == "url"
        assert evaluate(qtbot, window, "document.querySelector('[name=public_origin]').value") == "https://mobile.example.com"
        evaluate(qtbot, window, "state.mobileDraft=null;state.data.mobile.configuration.bind='127.0.0.1';render();true")
        assert evaluate(qtbot, window, "document.querySelector('.ds-access-url').textContent") == "https://mobile.example.com"
        assert evaluate(qtbot, window, "document.querySelector('.ds-access-addresses').open") is False
        # 仅有一个网络候选时直接显示，同时保留仅本机地址的正确说明。
        evaluate(qtbot, window, "state.data.mobile={enabled:true,configuration:{bind:'0.0.0.0'},addresses:['http://127.0.0.1:8765','http://192.168.1.2:8765']};render();true")
        assert evaluate(qtbot, window, "document.querySelector('.ds-access-url').textContent") == "http://192.168.1.2:8765"
        evaluate(qtbot, window, "state.data.mobile=window.savedMobileAccess;render();true")
        # 应用已有配置也必须成功，不只是首次开启。
        click("document.querySelector('[data-form=mobile-config]').requestSubmit()", "mobile_enable")
        for _ in range(2):
            data = click("document.querySelector('[data-action=mobile-pair]').click()", "mobile_pair")
            status, _, headers = request(runtime, "/api/pair", {"code": data["pairing"]["code"], "name": "按钮测试手机"})
            assert status == 200
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            assert request(runtime, "/api/session", cookie=cookie)[0] == 200
            # 重新生成配对码会同时读取最新设备列表。
            click("document.querySelector('[data-action=mobile-pair]').click()", "mobile_pair")
            qtbot.waitUntil(lambda: evaluate(qtbot, window, "!!document.querySelector('[data-revoke-device]')"))
            click("document.querySelector('[data-revoke-device]').click()", "mobile_revoke")
            assert request(runtime, "/api/session", cookie=cookie)[0] == 401
        assert not runtime.store.devices()
        click("document.querySelector('[data-action=mobile-disable]').click()", "mobile_disable")
        assert not runtime.accepting and runtime.server is None
        assert runtime.store.configuration()["enabled"] is False
        # 能力目录默认全部收起；用户收起最后一组后，刷新也不能自动展开第一组。
        evaluate(qtbot, window, "state.page='plugins';state.pluginTab='catalog';state.openGroups.clear();state.data.plugins={items:[{id:'agent',name:'助手管理'},{id:'schedule',name:'日程管理'}]};state.data.tools={items:[{id:'memory_list',plugin_id:'agent',display_name:'查看记忆'},{id:'calendar_list',plugin_id:'schedule',display_name:'查看日程'}]};render();true")
        assert evaluate(qtbot, window, "document.querySelectorAll('.ds-tool-list details[open]').length") == 0
        evaluate(qtbot, window, "document.querySelector('[data-group=agent] summary').click();true")
        qtbot.waitUntil(lambda: evaluate(qtbot, window, "state.openGroups.has('agent')"))
        evaluate(qtbot, window, "render();true")
        assert evaluate(qtbot, window, "document.querySelector('[data-group=agent]').open")
        evaluate(qtbot, window, "document.querySelector('[data-group=agent] summary').click();true")
        qtbot.waitUntil(lambda: evaluate(qtbot, window, "!state.openGroups.size"))
        evaluate(qtbot, window, "render();true")
        assert evaluate(qtbot, window, "document.querySelectorAll('.ds-tool-list details[open]').length") == 0
    finally:
        window.release_studio()
        window._allow_close = True
        window.close()
        delete(window)
        stop(qtbot, bridge)


def test_late_mobile_enable_is_stopped_before_backend_close(qtbot, tmp_path, monkeypatch):
    from deskpal.mobile.server import MobileRuntime
    from tests.gui.desktop.test_bridge import FakeApplication, FakeFacade
    entered, release = Event(), Event()
    original = MobileRuntime.start

    def delayed(runtime, configuration):
        entered.set()
        assert release.wait(8)
        original(runtime, configuration)

    monkeypatch.setattr(MobileRuntime, "start", delayed)
    app = FakeApplication()
    bridge = ApplicationBridge(lambda: app, facade_factory=FakeFacade)
    bridge.configure_mobile(tmp_path / "access.sqlite3", {})
    bridge.start()
    qtbot.waitUntil(lambda: bridge.state == "Ready")
    bridge.send("mobile_enable", {"bind": "127.0.0.1", "port": unused_port(), "public_origin": ""})
    try:
        qtbot.waitUntil(entered.is_set)
        bridge.shutdown()
        assert not app.closed
    finally:
        release.set()
        qtbot.waitUntil(lambda: bridge.state == "Closed", timeout=10000)
    assert app.closed and not bridge._mobile.accepting and bridge._mobile.server is None


def test_mobile_clarification_and_cancel_use_the_existing_task(qtbot, tmp_path):
    class ClarifyPlanner:
        first = True

        def plan_context(self, request, **kwargs):
            if self.first:
                self.first = False
                return Plan("请补充你的选择。", (), "clarify")
            return Plan("已经收到你的选择。")

    planner = ClarifyPlanner()
    bridge, _ = start(qtbot, tmp_path, planner=planner)
    try:
        runtime = bridge._mobile
        cookie, session, _ = pair(runtime)
        with ThreadPoolExecutor(max_workers=2) as executor:
            rpc = lambda op, params={}: http_rpc(qtbot, executor, runtime, cookie, session, op, params)
            daily = rpc("conversation_list")["items"][0]
            parameters = {"request": "需要补充信息", "conversation_id": daily["id"], "expected_conversation_epoch": daily["epoch"], "command_id": "clarify-one"}
            task_id = rpc("submit", parameters)["task_id"]
            qtbot.waitUntil(lambda: bridge._app.tasks.get(task_id).status == TaskStatus.AWAITING_INPUT, timeout=10000)
            rpc("task_answer", {"task_id": task_id, "content": "选第一项。", "command_id": "answer-one"})
            qtbot.waitUntil(lambda: bridge._app.tasks.get(task_id).status == TaskStatus.COMPLETED, timeout=10000)
            assert len(bridge._app.tasks.list_tasks()) == 1
            planner.first = True
            task_id = rpc("submit", {**parameters, "command_id": "clarify-two"})["task_id"]
            qtbot.waitUntil(lambda: bridge._app.tasks.get(task_id).status == TaskStatus.AWAITING_INPUT, timeout=10000)
            assert rpc("cancel", {"task_id": task_id, "command_id": "cancel-one"})["status"] == "Cancelled"
    finally:
        stop(qtbot, bridge)
