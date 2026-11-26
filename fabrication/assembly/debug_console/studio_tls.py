"""Serve Studio over HTTP and HTTPS on the same port (8770).

Browsers only expose WebXR on a secure origin, so the PICO page needs HTTPS.
Studio keeps one listener, one process and one application: the first byte of
each connection decides. 0x16 (a TLS handshake) is decrypted in memory and
handed to uvicorn's normal HTTP protocol; anything else goes to that protocol
unchanged, so existing ``http://`` users and health checks are unaffected.

Only used when ``.local-deploy/studio-network.json`` has a ``tls`` section;
``start-platform.ps1`` starts plain uvicorn otherwise. Run as::

    python -m fabrication.assembly.debug_console.studio_tls \
        fabrication.assembly.debug_console.platform_app:app --host 0.0.0.0 --port 8770

The application string stays on the command line so existing stop/deploy
scripts still recognise the Studio process.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
NETWORK = REPO / '.local-deploy' / 'studio-network.json'


def tls_settings(path=NETWORK):
    """The ``tls`` section with absolute paths, or None when HTTPS is off."""
    path = Path(path)
    if not path.exists():
        return None
    section = json.loads(path.read_text(encoding='utf-8-sig')).get('tls')
    if not section:
        return None
    resolved = {}
    for key in ('cert', 'key', 'ca'):
        if section.get(key):
            value = Path(section[key])
            resolved[key] = value if value.is_absolute() else REPO / value
    if not resolved.get('cert') or not resolved.get('key'):
        raise ValueError('studio-network.json 的 tls 需要 cert 与 key')
    return resolved


def server_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert), str(key))
    return context


class _TLSTransport(asyncio.Transport):
    """What the inner HTTP protocol sees for a TLS connection."""

    def __init__(self, raw, tls, outgoing, context):
        super().__init__()
        self._raw, self._tls, self._out, self._context = raw, tls, outgoing, context
        self._protocol = None
        self._closing = False

    def get_extra_info(self, name, default=None):
        if name == 'sslcontext':
            return self._context
        if name == 'ssl_object':
            return self._tls
        return self._raw.get_extra_info(name, default)

    def set_protocol(self, protocol):
        self._protocol = protocol

    def get_protocol(self):
        return self._protocol

    def is_closing(self):
        return self._closing or self._raw.is_closing()

    def pause_reading(self):
        self._raw.pause_reading()

    def resume_reading(self):
        self._raw.resume_reading()

    def is_reading(self):
        return self._raw.is_reading()

    def flush(self):
        data = self._out.read()
        if data and not self._raw.is_closing():
            self._raw.write(data)

    def write(self, data):
        if self.is_closing() or not data:
            return
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            offset += self._tls.write(view[offset:])
        self.flush()

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def can_write_eof(self):
        return False

    def get_write_buffer_size(self):
        return self._raw.get_write_buffer_size()

    def get_write_buffer_limits(self):
        return self._raw.get_write_buffer_limits()

    def set_write_buffer_limits(self, high=None, low=None):
        self._raw.set_write_buffer_limits(high, low)

    def close(self):
        if self._closing:
            return
        self._closing = True
        try:
            self._tls.unwrap()
        except (ssl.SSLError, ValueError):
            pass  # The peer's close_notify is not awaited.
        self.flush()
        self._raw.close()

    def abort(self):
        self._closing = True
        self._raw.abort()


def dual_protocol(context, inner=None):
    """An asyncio protocol class for uvicorn's ``http`` setting."""
    if inner is None:
        from uvicorn.protocols.http.auto import AutoHTTPProtocol as inner

    class DualProtocol(asyncio.Protocol):
        def __init__(self, **kwargs):
            self._kwargs = kwargs
            self._raw = None
            self._inner = None
            self._tls = None
            self._transport = None
            self._handshaken = False

        def connection_made(self, transport):
            self._raw = transport

        def data_received(self, data):
            if self._inner is None and self._tls is None:
                if data[:1] == b'\x16':
                    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
                    self._incoming = incoming
                    self._tls = context.wrap_bio(incoming, outgoing, server_side=True)
                    self._transport = _TLSTransport(self._raw, self._tls, outgoing, context)
                else:
                    # Plain HTTP: hand the socket over entirely.
                    self._inner = inner(**self._kwargs)
                    self._raw.set_protocol(self._inner)
                    self._inner.connection_made(self._raw)
                    self._inner.data_received(data)
                    return
            if self._tls is None:
                self._inner.data_received(data)
                return
            self._incoming.write(data)
            if not self._handshaken:
                try:
                    self._tls.do_handshake()
                except ssl.SSLWantReadError:
                    self._transport.flush()
                    return
                except (ssl.SSLError, OSError):
                    self._transport.flush()
                    self._raw.close()
                    return
                self._handshaken = True
                self._transport.flush()
                self._inner = inner(**self._kwargs)
                self._transport.set_protocol(self._inner)
                self._inner.connection_made(self._transport)
            chunks = []
            closed = False
            while True:
                try:
                    chunk = self._tls.read(65536)
                except ssl.SSLWantReadError:
                    break
                except ssl.SSLZeroReturnError:
                    closed = True
                    break
                except (ssl.SSLError, OSError):
                    self._raw.close()
                    return
                if not chunk:
                    closed = True
                    break
                chunks.append(chunk)
            self._transport.flush()
            for chunk in chunks:
                self._inner.data_received(chunk)
            if closed:
                self._transport.close()

        def eof_received(self):
            if self._inner is not None and hasattr(self._inner, 'eof_received'):
                return self._inner.eof_received()
            return None

        def connection_lost(self, exc):
            if self._inner is not None:
                self._inner.connection_lost(exc)

        def pause_writing(self):
            if self._inner is not None:
                self._inner.pause_writing()

        def resume_writing(self):
            if self._inner is not None:
                self._inner.resume_writing()

    return DualProtocol


def main(argv=None):
    parser = argparse.ArgumentParser(description='Studio on one port for HTTP and HTTPS')
    parser.add_argument('app')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--log-level', default='warning')
    parser.add_argument('--network', default=str(NETWORK))
    args = parser.parse_args(argv)
    settings = tls_settings(args.network)
    if not settings:
        raise SystemExit('studio-network.json 没有 tls 配置；请用原 uvicorn 命令启动')
    import uvicorn
    uvicorn.run(args.app, host=args.host, port=args.port, log_level=args.log_level,
                http=dual_protocol(server_context(settings['cert'], settings['key'])))


if __name__ == '__main__':
    main()
