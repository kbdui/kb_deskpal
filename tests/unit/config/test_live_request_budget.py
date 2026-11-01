import json
import urllib.request
import pytest
from scripts.check_companion_live import Budget


def test_expanded_memory_allowance_keeps_aggregate_live_output_budget(monkeypatch):
    sent = []
    class Opener:
        def open(self, request, *, timeout):
            sent.append(json.loads(request.data)['max_output_tokens'])
            return None
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    budget = Budget(48, 8192)
    request = urllib.request.Request('https://service.invalid/responses',
        data=json.dumps({'max_output_tokens': 8192}).encode())
    for _ in range(18):
        budget.transport(request, timeout=40)
    with pytest.raises(RuntimeError, match='budget'):
        budget.transport(request, timeout=40)
    assert sent == [8192] * 18
    assert sum(sent) == 48 * 3072


def test_main_requests_still_use_smaller_default_limit(monkeypatch):
    sent = []
    class Opener:
        def open(self, request, *, timeout):
            sent.append(json.loads(request.data)['max_output_tokens'])
            return None
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    Budget(1, 8192).transport(urllib.request.Request('https://service.invalid/responses', data=b'{}'), timeout=40)
    assert sent == [3072]
