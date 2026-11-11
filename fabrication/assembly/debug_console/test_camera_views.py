"""Demand leases and source freshness use generated frames and mocked HTTP."""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from fabrication.assembly.debug_console.camera_views import CameraViews


def test_reuses_fresh_authoritative_snapshots_and_tracks_only_one_viewer_task_per_camera():
    async def run():
        calls = []
        def response(request):
            calls.append(request.url.path)
            return httpx.Response(200, content=b'\xff\xd8generated\xff\xd9',
                                  headers={'X-Captured-At': '99.5', 'X-Frame-Sequence': '42'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            views = CameraViews(lambda: client, 'http://controller', wall_clock=lambda: 100.)
            try:
                data, headers = await views.frame(0)
                task = views.tasks[0]
                await views.frame(0); await views.frame(1); await asyncio.sleep(.01)
                assert views.tasks[0] is task and len(views.tasks) == 2
                assert headers['X-Frame-Ttl-Ms'] == '1500' and data.startswith(b'\xff\xd8')
                assert all('snapshot' in path for path in calls)
            finally:
                await views.close()
            assert not views.tasks
    asyncio.run(run())


@pytest.mark.parametrize('captured,sequence,data', [
    ('97', '1', b'\xff\xd8old'), ('101', '1', b'\xff\xd8future'),
    ('99.5', '1', b'not jpeg'), ('99.5', 'bad', b'\xff\xd8'), (None, '1', b'\xff\xd8')])
def test_expired_invalid_or_unattributed_frames_never_become_live(captured, sequence, data):
    async def run():
        headers = {'X-Frame-Sequence': sequence}
        if captured: headers['X-Captured-At'] = captured
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data, headers=headers))) as client:
            views = CameraViews(lambda: client, 'http://controller', wall_clock=lambda: 100.)
            try:
                with pytest.raises(HTTPException) as exc: await views.frame(0)
                assert exc.value.status_code == 409
                with pytest.raises(HTTPException) as exc: await views.frame(9)
                assert exc.value.status_code == 404
            finally: await views.close()
    asyncio.run(run())


def test_no_snapshot_starts_one_existing_mjpeg_route_and_idle_lease_closes_its_http_consumer():
    async def run():
        opened, closed = asyncio.Event(), asyncio.Event()
        clock = [10.]
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                opened.set()
                yield b'frame'
                await asyncio.Event().wait()
            async def aclose(self): closed.set()
        def response(request):
            if 'snapshot' in request.url.path: return httpx.Response(409)
            return httpx.Response(200, stream=Stream())
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            views = CameraViews(lambda: client, 'http://controller', clock=lambda: clock[0])
            try:
                with pytest.raises(HTTPException): await views.frame(2)
                await asyncio.wait_for(opened.wait(), 1)
                clock[0] += 3
                await asyncio.wait_for(closed.wait(), 1)
                assert not views.tasks
            finally: await views.close()
    asyncio.run(run())
