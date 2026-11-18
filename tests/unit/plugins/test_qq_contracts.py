"""QQ 插件关键边界：迁移、消息归属、发送幂等、事件签名和渠道授权。"""

from contextlib import closing
import hashlib
import hmac
import sqlite3

import pytest

from deskpal.plugins.execution import ChannelDenied, ChannelHost, TaskExecutionContext, ToolPolicy
from deskpal.plugins.qq.runtime import ids
from deskpal.plugins.qq.social import SocialMemory
from deskpal.plugins.qq.store import QqStore, SCHEMA
from deskpal.plugins.qq.transport import endpoint, verify_event_signature
from deskpal.tools.models import ToolCall


STAMP = "2026-11-14T10:00:00+00:00"


def incoming(store: QqStore, platform_id: str, body: str, *, thread: str = "private:12345",
             speaker: str = "12345", eligible: bool = True) -> bool:
    return store.receive("99999", thread, platform_id, speaker, "联系人", body, STAMP,
                         outgoing=False, eligible=eligible)


def test_v1_database_migrates_without_losing_messages_or_send_receipts(tmp_path):
    path = tmp_path / "plugin_qq.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO qq_message(account,thread,platform_id,speaker_id,speaker_name,direction,content,created_at,eligible) VALUES(?,?,?,?,?,?,?,?,?)",
                   ("99999", "private:12345", "m1", "12345", "联系人", "in", "保留原文", STAMP, 1))
        db.execute("INSERT INTO qq_send_batch VALUES(?,?,?,?,?,?,?,?,?)",
                   ("batch", "task", "call", "99999", "private:12345", "hash", 0, "blocked", STAMP))
        db.execute("INSERT INTO qq_send_command VALUES(?,?,?, ?,?,?)",
                   ("batch", 0, "已发消息", "confirmed", "9001", ""))
        db.commit()

    store = QqStore(path)
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert store.rows("SELECT content,reply_to,mentions FROM qq_message") == [
            {"content": "保留原文", "reply_to": None, "mentions": "[]"}
        ]
        assert store.rows("SELECT content,state,platform_id FROM qq_send_command") == [
            {"content": "已发消息", "state": "confirmed", "platform_id": "9001"}
        ]
        assert store.db.execute("SELECT COUNT(*) FROM qq_social_memory").fetchone()[0] == 0
    finally:
        store.close()


def test_interrupted_send_is_marked_unknown_and_never_assumed_failed(tmp_path):
    path = tmp_path / "qq.sqlite3"
    store = QqStore(path)
    try:
        incoming(store, "m1", "你好")
        batch = store.batch("task", "call", "99999", "private:12345", "回复", ["第一条"])
        with store.transaction() as db:
            db.execute("UPDATE qq_send_command SET state='sending' WHERE batch_id=?", (batch["id"],))
    finally:
        store.close()
    reopened = QqStore(path)
    try:
        command = reopened.rows("SELECT state FROM qq_send_command WHERE batch_id=?", (batch["id"],))[0]
        status = reopened.rows("SELECT state FROM qq_send_batch WHERE id=?", (batch["id"],))[0]
        assert command["state"] == "unknown"
        assert status["state"] == "blocked"
    finally:
        reopened.close()


def test_messages_are_deduplicated_and_late_unseen_messages_are_not_claimed_by_learning(tmp_path):
    store = QqStore(tmp_path / "qq.sqlite3")
    try:
        assert incoming(store, "m1", "本轮已看到") is True
        assert incoming(store, "m1", "重复上报") is False
        work = store.reserve_work("99999")
        assert work is not None
        planned = store.admit(work["id"])
        planned_ids = tuple(row["sequence"] for row in planned)
        store.mark_planning_input(work["id"], planned_ids)

        incoming(store, "m2", "规划之后才到")
        assert [row["content"] for row in store.learning_input(work["id"])] == ["本轮已看到"]
        assert store.rows("SELECT work_id FROM qq_message WHERE platform_id='m2'")[0]["work_id"] is None
    finally:
        store.close()


def test_send_batch_retry_keeps_message_group_and_rejects_changed_body_or_delivery(tmp_path):
    store = QqStore(tmp_path / "qq.sqlite3")
    try:
        first = store.batch("task", "call", "99999", "group:54321", "草稿", ["第一条", "第二条"],
                            delivery={"mention_ids": ["12345"]})
        retry = store.batch("task", "call", "99999", "group:54321", "草稿", None,
                            delivery={"mention_ids": ["12345"]})
        assert retry["id"] == first["id"]
        assert [row["content"] for row in store.rows("SELECT content FROM qq_send_command WHERE batch_id=? ORDER BY position", (first["id"],))] == ["第一条", "第二条"]
        with pytest.raises(ValueError, match="发送编号已绑定其他内容"):
            store.batch("task", "call", "99999", "group:54321", "改过的草稿", None,
                        delivery={"mention_ids": ["12345"]})
        with pytest.raises(ValueError, match="发送编号已绑定其他内容"):
            store.batch("task", "call", "99999", "group:54321", "草稿", None,
                        delivery={"mention_ids": ["67890"]})
    finally:
        store.close()


def test_history_is_scoped_to_current_account_thread_and_query(tmp_path):
    store = QqStore(tmp_path / "qq.sqlite3")
    try:
        incoming(store, "m1", "项目进度", thread="private:12345")
        incoming(store, "m2", "项目私密细节", thread="private:67890", speaker="67890")
        incoming(store, "m3", "其他话题", thread="group:54321", speaker="23456")
        rows = store.history("99999", "private:12345", query="项目")
        assert [row["content"] for row in rows] == ["项目进度"]
    finally:
        store.close()


def test_same_qq_identity_maps_to_one_person_without_merging_same_display_names(tmp_path):
    store = QqStore(tmp_path / "qq.sqlite3")
    try:
        rows = []
        for platform_id, thread, person_id in (("m1", "private:12345", "12345"),
                                                ("m2", "group:54321", "12345"),
                                                ("m3", "group:54321", "67890")):
            incoming(store, platform_id, "你好", thread=thread, speaker=person_id)
            rows.append(store.rows("SELECT * FROM qq_message WHERE platform_id=?", (platform_id,))[0])
        social = SocialMemory(type("Runtime", (), {"store": store})())
        people = social.people("99999", rows)
        by_account = {person["account_id"]: person["person_id"] for person in people}
        assert len(by_account) == 2
        assert by_account["12345"] != by_account["67890"]
        identity_ids = store.rows("SELECT person_id FROM qq_identity WHERE namespace='99999' AND external_id='12345'")
        assert len(identity_ids) == 1
        assert identity_ids[0]["person_id"] == by_account["12345"]
        assert store.db.execute("SELECT COUNT(*) FROM qq_person").fetchone()[0] == 2
    finally:
        store.close()


def test_qq_tool_capability_is_denied_to_local_tasks_and_allowed_only_by_runtime_policy():
    class Runtime:
        tool_names = ("qq_send",)

        def __init__(self, allowed: bool):
            self.is_allowed = allowed

        def allowed(self, context, call):
            return self.is_allowed

    class Host(ChannelHost):
        def __init__(self, context, runtime):
            self._context, self._runtime = context, runtime
            self.runtimes = {"qq": runtime}

        def context(self, task_id):
            return self._context

        def runtime(self, context):
            return self._runtime

    context = TaskExecutionContext("qq", "qq", "99999", "12345", "private:12345", "private")
    call = ToolCall("qq_send", {"draft": "不要越界"})
    with pytest.raises(ChannelDenied):
        ToolPolicy(Host(None, Runtime(True))).check("desktop-task", call)
    assert ToolPolicy(Host(context, Runtime(True))).check("qq-task", call) is True
    with pytest.raises(ChannelDenied):
        ToolPolicy(Host(context, Runtime(False))).check("qq-task", call)


def test_endpoint_only_accepts_loopback_http_and_scope_wildcard_is_explicit():
    assert endpoint("http://127.0.0.1:3000/") == "http://127.0.0.1:3000"
    for invalid in ("https://127.0.0.1:3000", "http://192.168.1.2:3000", "http://user@127.0.0.1:3000",
                    "http://127.0.0.1:3000/path", "http://127.0.0.1:3000?token=x"):
        with pytest.raises(ValueError):
            endpoint(invalid)
    assert ids("12345, 67890") == ["12345", "67890"]
    assert ids("*") == ["*"]
    for invalid in ("* 12345", "1234", ["12345", 67890]):
        with pytest.raises(ValueError):
            ids(invalid)


def test_event_signature_accepts_only_exact_raw_body_hmac():
    secret = "test-signature-secret-123"
    body = b'{"post_type":"message","message":"hello"}'
    signature = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    assert verify_event_signature(secret, body, signature)
    assert not verify_event_signature(secret, body + b" ", signature)
    assert not verify_event_signature(secret, body, "sha1=" + "0" * 40)
