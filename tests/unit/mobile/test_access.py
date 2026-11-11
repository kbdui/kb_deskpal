"""仅使用临时授权库，不接触用户档案、网络或模型。"""
import sqlite3

import pytest

from deskpal.mobile.access import AccessStore, Pairing


def test_pairing_is_expiring_single_use_and_only_hash_is_saved(tmp_path, monkeypatch):
    store = AccessStore(tmp_path / "access.sqlite3")
    pairing = Pairing(store)
    code = pairing.generate()["code"]
    token = pairing.exchange(code, "手机")
    assert store.valid(token)
    assert AccessStore(store.path).valid(token)
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT * FROM device").fetchone()
    assert token not in row and code not in row
    with pytest.raises(ValueError):
        pairing.exchange(code, "重复")
    pairing.generate()
    monkeypatch.setattr("deskpal.mobile.access.time", lambda: pairing.expires + 1)
    with pytest.raises(ValueError):
        pairing.exchange(pairing.code, "已过期")


def test_five_bad_attempts_require_a_new_local_pairing_code(tmp_path):
    store = AccessStore(tmp_path / "access.sqlite3")
    pairing = Pairing(store)
    code = pairing.generate()["code"]
    wrong = "00000000" if code != "00000000" else "11111111"
    for _ in range(5):
        with pytest.raises(ValueError):
            pairing.exchange(wrong, "手机")
    with pytest.raises(ValueError):
        pairing.exchange(code, "手机")
    token = pairing.exchange(pairing.generate()["code"], "手机")
    store.revoke(store.devices()[0]["id"])
    assert not store.valid(token)


def test_configuration_is_independent_and_device_count_bounded(tmp_path):
    store = AccessStore(tmp_path / "access.sqlite3")
    assert store.configuration()["enabled"] is False
    store.configure(enabled=True, bind="127.0.0.1", port=8765, public_origin="https://phone.example")
    assert AccessStore(store.path).configuration()["public_origin"] == "https://phone.example"
    for index in range(16):
        store.issue(str(index))
    with pytest.raises(ValueError):
        store.issue("too many")
    store.revoke()
    assert store.devices() == []
