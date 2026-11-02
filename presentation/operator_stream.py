"""Read-only SSE bridge to the unique controller's operator event channel."""
import asyncio
import json

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse


def install_operator_stream(router, controller_url):
    @router.get('/operator-stream')
    async def operator_stream(request: Request):
        async def generate():
            while not await request.is_disconnected():
                if controller_url:
                    try:
                        timeout = httpx.Timeout(6, connect=2)
                        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
                            async with client.stream('GET', controller_url+'/api/operator/stream') as response:
                                response.raise_for_status()
                                async for line in response.aiter_lines():
                                    if await request.is_disconnected():
                                        return
                                    yield line+'\n'
                    except httpx.HTTPError:
                        pass
                value = {'version': 1, 'connected': None, 'phase': 'unknown', 'mode': None,
                         'reason': '控制服务未连接' if controller_url else '未配置车载控制服务'}
                yield 'event: operator\ndata: '+json.dumps(value, ensure_ascii=False)+'\n\n'
                await asyncio.sleep(2)
        return StreamingResponse(generate(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
