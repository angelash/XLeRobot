"""MiniMax playback adapter; public text only, independent of robot control.

Request/SSE/error handling adapted from live-avatar's MiniMaxTTSHandler
(MIT; see LICENSE_LIVE_AVATAR.txt). No S2S or LiveTalking session is opened.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import time
import wave

import httpx

from .voice_catalogue import normalize_voices, snapshot


ENDPOINTS={'https://api.minimax.io/v1/t2a_v2','https://api.minimaxi.com/v1/t2a_v2','https://api.minimax.cn/v1/t2a_v2'}
SAMPLE_RATE=16000
MAX_PCM=4*1024**2


class SpeechError(Exception):
    def __init__(self,message,status=503):
        super().__init__(message);self.status=status


def spoken_parts(text,limit=220):
    text=re.sub(r'```.*?```','',text,flags=re.S)
    text=re.sub(r'\[([^\]]+)\]\([^\)]+\)',r'\1',text)
    text=re.sub(r'https?://\S+','',text)
    text=re.sub(r'(?m)^\s*(?:#{1,6}|[-*+]|\d+[.)])\s+','',text)
    text=re.sub(r'[`*_~]','',text)
    text=re.sub(r'\s+',' ',text).strip()
    sentences=re.findall(r'[^。！？!?；;\n]+[。！？!?；;]?|[。！？!?；;]',text)
    result=[];chunk=''
    for sentence in sentences:
        while len(sentence)>limit:
            if chunk:result.append(chunk.strip());chunk=''
            result.append(sentence[:limit].strip());sentence=sentence[limit:]
        if len(chunk)+len(sentence)>limit:
            result.append(chunk.strip());chunk=''
        chunk+=sentence
    if chunk.strip():result.append(chunk.strip())
    return [part for part in result if part]


def parse_sse(line):
    line=line.strip()
    if not line or line.startswith(':'):return None
    if line.startswith('data:'):line=line[5:].strip()
    if line=='[DONE]':return None
    if not line.startswith('{'):return None
    try:value=json.loads(line)
    except ValueError:raise SpeechError('语音服务返回的数据无效') from None
    if not isinstance(value,dict):raise SpeechError('语音服务返回的数据无效')
    try:code=int((value.get('base_resp') or {}).get('status_code',0) or 0)
    except (TypeError,ValueError):raise SpeechError('语音服务返回的数据无效') from None
    if code:raise SpeechError(f'MiniMax 未完成合成（代码 {code}），文字已保留')
    return value


class SpeechService:
    def __init__(self,root,*,transport=None):
        self.root=Path(root);self.transport=transport
        self.lock=asyncio.Lock()
        self.catalogue_lock=asyncio.Lock()
        self.rows=snapshot()['voices'];self.catalogue_at=0;self.catalogue_source='snapshot'
        self.catalogue_note='系统目录快照；可重新获取当前账号的完整目录'

    def configuration(self):
        config={}
        path=self.root/'speech-provider.json'
        try:
            if path.is_file():config=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):raise SpeechError('本机语音服务配置无效') from None
        key=os.environ.get('MINIMAX_API_KEY') or config.get('api_key','')
        endpoint=os.environ.get('MINIMAX_TTS_BASE_URL') or config.get('base_url','https://api.minimax.io/v1/t2a_v2')
        model=os.environ.get('MINIMAX_TTS_MODEL') or config.get('model','speech-2.8-turbo')
        if endpoint not in ENDPOINTS:raise SpeechError('MiniMax 服务地址配置无效')
        if model not in ('speech-2.8-turbo','speech-2.8-hd'):raise SpeechError('MiniMax 模型配置无效')
        return {'key':key,'endpoint':endpoint,'model':model}

    def status(self):
        try:ready=bool(self.configuration()['key'])
        except SpeechError:ready=False
        return {'available':ready,'provider':'MiniMax','default_enabled':False,
                'note':'只朗读新完成的公开回复；文字始终保留' if ready else '本机尚未配置 MiniMax；文字正常显示'}

    async def catalogue(self,refresh=False):
        async with self.catalogue_lock:
            config=self.configuration()
            if config['key'] and (refresh or time.time()-self.catalogue_at>21600):
                try:
                    async with httpx.AsyncClient(transport=self.transport,trust_env=False,timeout=20) as client:
                        response=await client.post(config['endpoint'].replace('t2a_v2','get_voice'),
                            headers={'Authorization':'Bearer '+config['key']},json={'voice_type':'all'})
                    response.raise_for_status();value=response.json()
                    if (value.get('base_resp') or {}).get('status_code'):raise ValueError()
                    rows=normalize_voices(value)
                    if not rows:raise ValueError()
                    self.rows=rows;self.catalogue_at=time.time();self.catalogue_source='provider'
                    self.catalogue_note='当前账号接口返回的全部系统、克隆与设计声线'
                except (httpx.HTTPError,ValueError,TypeError,KeyError):
                    self.catalogue_at=time.time()  # Do not repeatedly retry during a provider outage.
                    self.catalogue_note='实时目录暂不可用，显示上次目录；可手动重试'
            return {**self.status(),'voices':self.rows,'total':len(self.rows),
                    'source':self.catalogue_source,'retrieved_at':self.catalogue_at or None,'note':self.catalogue_note,
                    'moss_named_voice_found':any('moss' in (row['id']+' '+row['name']).lower() for row in self.rows)}

    async def synthesize(self,text,settings,*,cancelled=None):
        config=self.configuration()
        if not config['key']:raise SpeechError('本机尚未配置 MiniMax；文字已保留')
        if settings.voice_id not in {row['id'] for row in self.rows}:await self.catalogue()
        if settings.voice_id not in {row['id'] for row in self.rows}:
            raise SpeechError('所选声线不在当前目录，请刷新声线目录',422)
        identity=json.dumps({'text':text,'voice':settings.voice_id,'speed':settings.speed,
                             'pitch':settings.pitch,'emotion':settings.emotion,'model':config['model'],
                             'account':sha256(config['key'].encode()).hexdigest()},sort_keys=True,ensure_ascii=False)
        key=sha256(identity.encode()).hexdigest();directory=self.root/'speech-cache';path=directory/(key+'.wav')
        async with self.lock:
            if cancelled and await cancelled():raise SpeechError('语音已停止',409)
            if path.is_file():return path.read_bytes(),True
            request={'model':config['model'],'text':text,'stream':True,
                     'stream_options':{'exclude_aggregated_audio':True},'language_boost':'auto',
                     'voice_setting':{'voice_id':settings.voice_id,'speed':settings.speed,'pitch':settings.pitch,'vol':1},
                     'audio_setting':{'sample_rate':SAMPLE_RATE,'format':'pcm','channel':1},
                     'subtitle_enable':False,'output_format':'hex'}
            if settings.emotion!='auto':request['voice_setting']['emotion']=settings.emotion
            pcm=bytearray();finished=False
            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(transport=self.transport,trust_env=False,
                        timeout=httpx.Timeout(60,connect=10)) as client:
                        async with client.stream('POST',config['endpoint'],headers={
                            'Authorization':'Bearer '+config['key'],'Accept':'text/event-stream'},json=request) as response:
                            response.raise_for_status()
                            async for line in response.aiter_lines():
                                if cancelled and await cancelled():raise SpeechError('语音已停止',409)
                                value=parse_sse(line)
                                if value is None:continue
                                data=value.get('data') or {};encoded=data.get('audio') or ''
                                if not isinstance(encoded,str) or len(encoded)%2:raise SpeechError('语音音频数据无效')
                                try:pcm.extend(bytes.fromhex(encoded))
                                except ValueError:raise SpeechError('语音音频数据无效') from None
                                if len(pcm)>MAX_PCM:raise SpeechError('语音片段超过长度限制，文字已保留')
                                if data.get('status')==2:finished=True
                    break
                except httpx.HTTPError:
                    if pcm or attempt:raise SpeechError('MiniMax 暂时不可用，文字已保留') from None
            if not pcm or len(pcm)%2 or not finished:raise SpeechError('语音未完整生成，文字已保留')
            output=BytesIO()
            with wave.open(output,'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(SAMPLE_RATE);wav.writeframes(pcm)
            data=output.getvalue();directory.mkdir(parents=True,exist_ok=True)
            self.trim_cache(directory,len(data))
            temporary=path.with_suffix('.tmp');temporary.write_bytes(data);temporary.replace(path)
            return data,False

    @staticmethod
    def trim_cache(directory,incoming):
        files=[p for p in directory.glob('*.wav') if re.fullmatch(r'[a-f0-9]{64}\.wav',p.name) and not p.is_symlink()]
        total=sum(p.stat().st_size for p in files)
        for path in sorted(files,key=lambda p:p.stat().st_mtime):
            if total+incoming<=32*1024**2:break
            total-=path.stat().st_size;path.unlink()
