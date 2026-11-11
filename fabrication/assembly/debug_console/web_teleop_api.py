"""Exact browser command routes installed into the existing controller app."""
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class Start(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: bool = False


class Ref(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(min_length=32, max_length=32)


class Input(Ref):
    seq: int = Field(ge=0, strict=True)
    actions: list[str] = Field(default_factory=list, max_length=20)
    arm: Literal['left', 'right', 'both'] = 'left'
    scale: float = Field(default=.5, ge=.2, le=1, allow_inf_nan=False)


def install(app, runtime):
    def channel():
        r = runtime()
        from .original_runtime import PreviewRuntime
        if isinstance(r, PreviewRuntime):
            raise HTTPException(409, '隔离预览不能开启实机网页控制')
        return r.web_control

    @app.get('/api/web/status')
    def status():
        return channel().snapshot()

    @app.post('/api/web/start')
    def start(payload: Start):
        try:
            return channel().start(payload.confirmed)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/web/input')
    def input_state(payload: Input):
        try:
            return channel().input(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/web/stop')
    def stop(payload: Ref):
        try:
            return channel().stop(session_id=payload.session_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
