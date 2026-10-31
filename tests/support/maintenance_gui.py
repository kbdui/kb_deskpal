"""Synthetic maintenance lifecycle coverage; no SDK, personal config or databases."""

from threading import Event, get_ident
import json

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QInputDialog, QLineEdit, QLabel, QPushButton, QWidget

from deskpal.gui.launcher import DesktopLauncher
from deskpal.gui.maintenance import MaintenanceBridge, MaintenanceWindow


def status(**changes):
    result = {"values": {"provider": "offline", "timeout_seconds": 30}, "revision": "r1",
              "config_exists": True, "config_valid": True, "credential_present": False,
              "sources": {}, "issues": []}
    result.update(changes)
    return result


class ControlledBridge(QObject):
    reply = Signal(object)
    stopped = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.calls = []
        self.busy = False
        self.closing = False

    def send(self, operation, parameters=None, *, generation=0):
        assert not self.busy and not self.closing
        self.busy = True
        request_id = str(len(self.calls))
        self.calls.append((request_id, operation, dict(parameters or {}), generation))
        return request_id

    def send_sensitive(self, operation, parameters, *, credential_secret=None, generation=0):
        payload = dict(parameters)
        if credential_secret is not None:
            payload["credential_secret"] = credential_secret
        return self.send(operation, payload, generation=generation)

    def complete(self, data):
        request_id, operation, _, generation = self.calls[-1]
        self.busy = False
        self.reply.emit(dict(request_id=request_id, operation=operation, generation=generation,
                             ok=True, data=data))

    def shutdown(self):
        self.closing = True


