"""One Studio port answers both HTTP and HTTPS; local sockets only, no robot."""
import json
import socket
import ssl
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request

pytest.importorskip('cryptography')
from fabrication.assembly.debug_console import studio_cert, studio_tls


@pytest.fixture
def served(tmp_path):
    info = studio_cert.create(tmp_path / 'tls', addresses=['127.0.0.1'])
    app = FastAPI()

    @app.get('/where')
    def where(request: Request):
        return {'scheme': request.url.scheme, 'client': request.client.host}

    @app.post('/echo')
    async def echo(request: Request):
        return {'size': len(await request.body())}

    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    context = studio_tls.server_context(tmp_path / 'tls/server.crt', tmp_path / 'tls/server.key')
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning',
                                           http=studio_tls.dual_protocol(context)))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.05)
    yield port, tmp_path / 'tls', info
    server.should_exit = True; thread.join(5)


def test_same_port_serves_plain_http_and_verified_https(served):
    port, folder, info = served
    plain = httpx.get(f'http://127.0.0.1:{port}/where', trust_env=False).json()
    assert plain == {'scheme': 'http', 'client': '127.0.0.1'}
    verify = ssl.create_default_context(cafile=str(folder / 'ca.crt'))
    with httpx.Client(verify=verify, trust_env=False) as client:
        secure = client.get(f'https://127.0.0.1:{port}/where').json()
        assert secure == {'scheme': 'https', 'client': '127.0.0.1'}
        # Large bodies cross several TLS records in both directions.
        assert client.post(f'https://127.0.0.1:{port}/echo', content=b'x' * 300_000).json() == {'size': 300_000}
        assert client.get(f'https://localhost:{port}/where').status_code == 200
    assert '127.0.0.1' in info['addresses'] and info['ca_sha256'].count(':') == 31


def test_untrusted_client_is_refused_and_server_keeps_running(served):
    port, folder, _ = served
    with pytest.raises(httpx.ConnectError):
        httpx.get(f'https://127.0.0.1:{port}/where', trust_env=False)
    with socket.create_connection(('127.0.0.1', port)) as raw:
        raw.sendall(b'\x16\x03\x01\x00\x05garbage')
        raw.settimeout(2)
        try:
            raw.recv(100)
        except OSError:
            pass
    assert httpx.get(f'http://127.0.0.1:{port}/where', trust_env=False).status_code == 200


def test_ca_is_reused_when_the_server_certificate_is_renewed(tmp_path):
    first = studio_cert.create(tmp_path, addresses=['192.168.137.150'])
    ca = (tmp_path / 'ca.crt').read_bytes(); key = (tmp_path / 'server.key').read_bytes()
    second = studio_cert.create(tmp_path, addresses=['192.168.137.150', '10.0.0.5'])
    assert (tmp_path / 'ca.crt').read_bytes() == ca and first['ca_sha256'] == second['ca_sha256']
    assert (tmp_path / 'server.key').read_bytes() != key and '10.0.0.5' in second['addresses']


def test_tls_settings_are_optional_and_need_cert_and_key(tmp_path):
    path = tmp_path / 'net.json'
    assert studio_tls.tls_settings(path) is None
    path.write_text(json.dumps({'listen_host': '0.0.0.0'}), encoding='utf-8')
    assert studio_tls.tls_settings(path) is None
    path.write_text(json.dumps({'tls': {'cert': 'x.crt'}}), encoding='utf-8')
    with pytest.raises(ValueError):
        studio_tls.tls_settings(path)
    path.write_text(json.dumps({'tls': {'cert': 'a/x.crt', 'key': str(tmp_path / 'x.key'), 'ca': 'a/ca.crt'}}),
                    encoding='utf-8')
    settings = studio_tls.tls_settings(path)
    assert settings['cert'] == studio_tls.REPO / 'a/x.crt' and settings['key'] == tmp_path / 'x.key'
