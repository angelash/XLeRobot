"""MiniMax voice metadata. Credentials and account-specific voices stay local."""
from __future__ import annotations

import json
from pathlib import Path
import re


SNAPSHOT=Path(__file__).with_name('voices.json')
SAMPLE_TEXT='我是 MOSS。当前系统运行正常。请告诉我，接下来需要做什么。'
RECOMMENDED={
    'Chinese (Mandarin)_Reliable_Executive':('沉稳高管','冷静、可靠的中年男声'),
    'Robot_Armor':('机械战甲','电子、机械质感男声'),
    'Chinese (Mandarin)_Male_Announcer':('播报男声','清晰、克制的播报感'),
    'Chinese (Mandarin)_Radio_Host':('电台男主播','低沉、平稳的叙述感'),
    'Chinese (Mandarin)_Lyrical_Voice':('抒情男声','可比较低音与语速'),
}


def snapshot():
    return json.loads(SNAPSHOT.read_text(encoding='utf-8'))


def normalize_voices(payload):
    known={row['id']:row for row in snapshot()['voices']}
    result=[];seen=set()
    for category in ('system_voice','voice_cloning','voice_generation'):
        for voice in payload.get(category,[]) or []:
            identifier=str(voice.get('voice_id',''))
            if not identifier or len(identifier)>200 or identifier in seen:continue
            seen.add(identifier)
            previous=known.get(identifier,{})
            description=voice.get('description',[])
            if isinstance(description,list):description=' '.join(str(item) for item in description)
            name=RECOMMENDED.get(identifier,(previous.get('name') or voice.get('voice_name') or identifier,''))[0]
            gender='female' if re.search(r'\bfemale\b',str(description),re.I) else 'male' if re.search(r'\bmale\b',str(description),re.I) else previous.get('gender','unknown')
            result.append({'id':identifier,'name':name,'provider_name':str(voice.get('voice_name') or identifier),
                           'language':previous.get('language') or language_for(identifier),'gender':gender,
                           'description':str(description)[:1200],'category':category,
                           'recommended':identifier in RECOMMENDED,'hint':RECOMMENDED.get(identifier,('',''))[1]})
    return result


def language_for(identifier):
    if identifier.startswith('Chinese (Mandarin)') or identifier in RECOMMENDED:return '普通话'
    if identifier.startswith('Cantonese'):return '粤语'
    first=identifier.split('_',1)[0]
    return {'English':'英语','Japanese':'日语','Korean':'韩语','Spanish':'西班牙语','French':'法语',
            'German':'德语','Russian':'俄语','Portuguese':'葡萄牙语','Italian':'意大利语',
            'Arabic':'阿拉伯语','Indonesian':'印尼语','Turkish':'土耳其语','Vietnamese':'越南语',
            'Dutch':'荷兰语','Ukrainian':'乌克兰语','Thai':'泰语','Polish':'波兰语','Romanian':'罗马尼亚语',
            'Greek':'希腊语','Czech':'捷克语','Finnish':'芬兰语','Hindi':'印地语','Swedish':'瑞典语',
            'Danish':'丹麦语','Malay':'马来语','Hungarian':'匈牙利语','Norwegian':'挪威语',
            'Slovak':'斯洛伐克语','Catalan':'加泰罗尼亚语','Croatian':'克罗地亚语','Filipino':'菲律宾语'}.get(first,first)
