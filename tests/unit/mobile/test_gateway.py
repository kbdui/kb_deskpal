"""远程入口复用本地业务，但不开放维护、角色修改或任意工具授权。"""
import pytest

from deskpal.application.client import ClientGateway


class Port:
    def __init__(self):
        self.calls = []

    def execute(self, operation, parameters):
        self.calls.append((operation, parameters))
        return {"accepted": True}


@pytest.mark.parametrize("operation", ["settings", "memory", "companion_command", "appearance_apply", "plugin_set_option", "resume"])
def test_unrelated_operations_are_not_exposed(operation):
    port = Port()
    with pytest.raises(ValueError):
        ClientGateway(port).execute(operation, {})
    assert not port.calls


def test_submit_requires_explicit_conversation_and_stable_command_identity():
    gateway = ClientGateway(Port())
    with pytest.raises(ValueError):
        gateway.execute("submit", {"request": "hello", "command_id": "one"})
    with pytest.raises(ValueError):
        gateway.execute("conversation_create", {"title": "new"})
    params = {"request": "hello", "command_id": "one", "conversation_id": "daily", "expected_conversation_epoch": 0}
    assert gateway.execute("submit", params)["accepted"]
    with pytest.raises(ValueError):
        gateway.execute("submit", {**params, "high_risk_authorized_tools": ["document_delete"]})
