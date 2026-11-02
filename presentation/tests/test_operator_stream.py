import asyncio
import json

from fastapi import FastAPI
import httpx

from presentation.operator_stream import install_operator_stream


class Request:
    async def is_disconnected(self):return False


def test_bridge_only_reads_the_fixed_controller_stream_and_releases_connection(monkeypatch):
    original=httpx.AsyncClient;requests=[];closed=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: operator\ndata: {"version":1,"connected":true}\n\n'
        async def aclose(self):closed.append(True)
    def reply(request):
        requests.append(request)
        return httpx.Response(200,headers={'Content-Type':'text/event-stream'},stream=Stream())
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(reply)))
    app=FastAPI();install_operator_stream(app,'http://127.0.0.1:8765')
    endpoint=app.routes[-1].endpoint
    async def check():
        response=await endpoint(Request())
        chunks=[]
        async for chunk in response.body_iterator:
            chunks.append(chunk)
            if ''.join(chunks).endswith('\n\n'):break
        assert '"connected":true' in ''.join(chunks)
        await response.body_iterator.aclose()
    asyncio.run(check())
    assert len(requests)==1 and requests[0].method=='GET'
    assert requests[0].url=='http://127.0.0.1:8765/api/operator/stream' and closed


def test_missing_or_unavailable_controller_is_unknown_not_stopped(monkeypatch):
    original=httpx.AsyncClient
    def reply(request):return httpx.Response(503,json={'detail':'generated offline'})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(reply)))
    async def check(url):
        app=FastAPI();install_operator_stream(app,url)
        response=await app.routes[-1].endpoint(Request())
        chunk=await anext(response.body_iterator)
        value=json.loads(chunk.split('data: ')[1])
        assert value['phase']=='unknown' and value['connected'] is None
        await response.body_iterator.aclose()
    asyncio.run(check(None));asyncio.run(check('http://127.0.0.1:8765'))
