import json

import pytest

from veritas import invoice_gateway as gateway


def test_gateway_fails_closed_and_rejects_unsafe_endpoints(monkeypatch):
    monkeypatch.delenv('VERITAS_INVOICE_GATEWAY_URL', raising=False)
    monkeypatch.delenv('VERITAS_INVOICE_GATEWAY_TOKEN', raising=False)
    with pytest.raises(RuntimeError, match='not configured'):
        gateway.configured_adapter().execute('contract', {}, 'key')
    for url in ('http://example.com', 'https://user:pass@example.com',
                'https://example.com?token=secret', 'https://example.com/#fragment'):
        with pytest.raises(ValueError):
            gateway.GatewayAdapter(url, 'secret')


def test_gateway_passes_stable_key_without_logging_token(monkeypatch):
    captured = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b'{"ready":true,"status":"Handled"}'

    class Opener:
        def open(self, request, timeout):
            captured.append(request)
            return Response()

    monkeypatch.setattr(gateway, 'build_opener', lambda *args: Opener())
    adapter = gateway.GatewayAdapter('https://gateway.example.test/api', 'secret')
    result = adapter.execute('charge_status', {'id': 'item'}, 'stable-key')
    assert result['status'] == 'Handled'
    request = captured[0]
    assert request.full_url == 'https://gateway.example.test/api/execute'
    assert request.get_header('Idempotency-key') == 'stable-key'
    payload = json.loads(request.data)
    assert payload['idempotency_key'] == 'stable-key'
    assert 'secret' not in request.data.decode()
