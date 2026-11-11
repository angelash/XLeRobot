"""Demand-leased HTTP viewers of the existing controller's camera collectors.

Reuse an existing collector's snapshots first. If none is active, keep exactly
one MJPEG HTTP consumer alive per camera; never open a camera device here.
"""
from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import HTTPException


class CameraViews:
    LEASE_S = 2.
    FRESH_S = 2.

    def __init__(self, client, upstream, clock=time.monotonic, wall_clock=time.time):
        self.client = client
        self.upstream = upstream
        self.clock = clock
        self.wall_clock = wall_clock
        self.tasks = {}
        self.demand = {}
        self.expiry_task = None
        self.closed = False

    def touch(self, camera):
        if self.closed:
            raise HTTPException(503, '相机观察正在关闭')
        self.demand[camera] = self.clock()
        if camera not in self.tasks or self.tasks[camera].done():
            self.tasks[camera] = asyncio.create_task(self._observe(camera))
        if not self.expiry_task or self.expiry_task.done():
            self.expiry_task = asyncio.create_task(self._expire())

    async def _expire(self):
        while self.tasks and not self.closed:
            await asyncio.sleep(.2)
            for camera, task in list(self.tasks.items()):
                if self.clock() - self.demand.get(camera, 0) > self.LEASE_S:
                    self.tasks.pop(camera, None)
                    self.demand.pop(camera, None)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def _observe(self, camera):
        while not self.closed:
            try:
                response = await self.client().get(self.upstream + f'/api/camera/{camera}/snapshot', timeout=1.5)
                if response.status_code == 200:
                    await asyncio.sleep(.35)
                    continue
                # 409 includes an absent/expired shared collector. The serial
                # owner rejects a second collector if another caller owns it.
                async with self.client().stream('GET', self.upstream + f'/camera/{camera}.mjpg',
                        timeout=httpx.Timeout(3., read=5.)) as stream:
                    if stream.status_code == 200:
                        async for _ in stream.aiter_bytes():
                            pass  # The authoritative snapshot stays in 8765.
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.5)

    async def frame(self, camera):
        if camera not in (0, 1, 2):
            raise HTTPException(404, '相机编号无效')
        self.touch(camera)
        try:
            response = await self.client().get(self.upstream + f'/api/camera/{camera}/snapshot', timeout=1.5)
        except httpx.HTTPError as exc:
            raise HTTPException(503, '相机服务暂不可达') from exc
        if response.status_code != 200:
            raise HTTPException(409, '正在等待此相机的新画面')
        try:
            captured = float(response.headers['X-Captured-At'])
            sequence = int(response.headers['X-Frame-Sequence'])
            age = self.wall_clock() - captured
            if not 0 <= age < self.FRESH_S or not response.content.startswith(b'\xff\xd8'):
                raise ValueError('stale or invalid image')
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, '相机画面已过期，等待新帧') from exc
        return response.content, {'Cache-Control': 'no-store', 'X-Captured-At': str(captured),
            'X-Frame-Sequence': str(sequence), 'X-Frame-Age-Ms': str(round(age * 1000, 1)),
            'X-Frame-Ttl-Ms': str(int((self.FRESH_S - age) * 1000))}

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values()) + ([self.expiry_task] if self.expiry_task else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        self.demand.clear()
