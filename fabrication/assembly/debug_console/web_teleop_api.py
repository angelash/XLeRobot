"""Exact browser command routes installed into the existing controller app."""
from typing import Annotated, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class Start(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: bool = False
    mode: Literal['keys', 'vr'] = 'keys'


class Ref(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(min_length=32, max_length=32)


class Input(Ref):
    seq: int = Field(ge=0, strict=True)
    actions: list[str] = Field(default_factory=list, max_length=20)
    arm: Literal['left', 'right', 'both'] = 'left'
    scale: float = Field(default=.5, ge=.2, le=1, allow_inf_nan=False)


Unit = Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]
Metre = Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]
Angle = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
Trigger = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Hand(BaseModel):
    """A held grip: displacement and orientation change since the client's anchor."""
    model_config = ConfigDict(extra='forbid')
    engage: int = Field(ge=0, le=2**31 - 1, strict=True)
    pos: tuple[Metre, Metre, Metre]
    pitch: Angle = 0.
    roll: Angle = 0.
    orient: bool = True


class Hands(BaseModel):
    model_config = ConfigDict(extra='forbid')
    left: Optional[Hand] = None
    right: Optional[Hand] = None


class Triggers(BaseModel):
    model_config = ConfigDict(extra='forbid')
    left: Optional[Trigger] = None
    right: Optional[Trigger] = None


class Jog(BaseModel):
    model_config = ConfigDict(extra='forbid')
    side: Literal['left', 'right']
    joint: int = Field(ge=1, le=5, strict=True)
    value: Unit
    engage: int = Field(ge=1, le=2**31 - 1, strict=True)


class VRInput(Ref):
    seq: int = Field(ge=0, strict=True)
    base: tuple[Unit, Unit] = (0., 0.)
    head: tuple[Unit, Unit] = (0., 0.)
    arms: Hands = Field(default_factory=Hands)
    grip: Triggers = Field(default_factory=Triggers)
    scale: float = Field(default=.5, ge=.2, le=1, allow_inf_nan=False)
    ratio: float = Field(default=1., ge=.25, le=2, allow_inf_nan=False)
    jog: Optional[Jog] = None


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

    @app.get('/api/web/vr-readiness')
    def vr_readiness():
        try:
            return channel().vr_readiness()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/web/start')
    def start(payload: Start):
        try:
            return channel().start(payload.confirmed, payload.mode)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/web/input')
    def input_state(payload: Input):
        try:
            return channel().input(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/web/vr-input')
    def vr_input(payload: VRInput):
        data = payload.model_dump()
        try:
            return channel().vr_input(data['session_id'], data['seq'], data['base'], data['head'],
                                      data['arms'], data['grip'], data['scale'], data['ratio'], data['jog'])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/web/stop')
    def stop(payload: Ref):
        try:
            return channel().stop(session_id=payload.session_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
