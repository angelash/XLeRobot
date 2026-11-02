from fastapi import HTTPException, Request
from fastapi.responses import Response
from pydantic import Field

from .speech import SpeechError, SpeechService, spoken_parts
from .state import Contract, Speech
from .voice_catalogue import SAMPLE_TEXT


class SpeechPart(Contract):
    epoch: str = Field(min_length=1,max_length=120)
    event_id: str = Field(min_length=1,max_length=120)
    part: int = Field(0,ge=0,le=100)


class Preview(Contract):
    voice_id: str = Field(min_length=1,max_length=200)
    text: str = Field(SAMPLE_TEXT,min_length=1,max_length=500)
    speed: float = Field(1,ge=.5,le=2)
    pitch: int = Field(0,ge=-12,le=12)
    emotion: str = Field('auto',pattern=r'^(auto|calm)$')


def install_speech(router,store,guard):
    service=SpeechService(store.root)
    store.speech_service=service

    def audio(data,cached,parts):
        return Response(data,media_type='audio/wav',headers={'Cache-Control':'no-store',
                        'X-Content-Type-Options':'nosniff','X-Speech-Parts':str(parts),
                        'X-Speech-Cached':str(cached).lower()})

    @router.get('/speech/status')
    async def status():return service.status()

    @router.get('/voices')
    async def voices():
        try:return await service.catalogue()
        except SpeechError as exc:raise HTTPException(exc.status,str(exc)) from None

    @router.post('/voices/refresh')
    async def refresh(request:Request):
        await guard(request)
        try:return await service.catalogue(refresh=True)
        except SpeechError as exc:raise HTTPException(exc.status,str(exc)) from None

    @router.post('/speech/preview')
    async def preview(value:Preview,request:Request):
        await guard(request)
        if not value.text.strip():raise HTTPException(422,'请输入试听文本')
        # This route is an explicit audition. It never enables automatic speech.
        settings=Speech(voice_id=value.voice_id,speed=value.speed,pitch=value.pitch,emotion=value.emotion)
        try:
            data,cached=await service.synthesize(value.text.strip(),settings,cancelled=request.is_disconnected)
            return audio(data,cached,1)
        except SpeechError as exc:raise HTTPException(exc.status,str(exc)) from None

    @router.post('/speech')
    async def speak(value:SpeechPart,request:Request):
        await guard(request)
        profile=store.profile()
        if not profile.speech.enabled:raise HTTPException(409,'自动语音已关闭，文字正常显示')
        batch=store.journal.read()
        if value.epoch!=batch['epoch']:raise HTTPException(410,'历史已更新，不重放旧语音')
        event=next((row for row in batch['events'] if row['event_id']==value.event_id),None)
        if not event or event['kind']!='message' or event['phase']!='completed' or event['evidence']=='demo':
            raise HTTPException(422,'只朗读已完成的真实公开回复')
        parts=spoken_parts(event['text'])
        if not parts:return Response(status_code=204,headers={'X-Speech-Parts':'0','Cache-Control':'no-store'})
        if value.part>=len(parts):raise HTTPException(422,'语音片段不存在')
        async def cancelled():return not store.profile().speech.enabled or await request.is_disconnected()
        try:
            data,cached=await service.synthesize(parts[value.part],profile.speech,cancelled=cancelled)
            return audio(data,cached,len(parts))
        except SpeechError as exc:raise HTTPException(exc.status,str(exc)) from None
