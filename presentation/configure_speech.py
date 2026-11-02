"""Copy explicitly selected live-avatar MiniMax settings into local runtime data.

Reads only set statements; never executes config.local.bat or starts Live Avatar.
"""
import argparse
import json
from pathlib import Path
import re

from .speech import ENDPOINTS
from .state import DEFAULT_DATA


def from_live_avatar(repository,data_dir=DEFAULT_DATA):
    path=Path(repository)/'scripts/config.local.bat'
    values={}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        match=re.fullmatch(r'\s*@?set\s+"?(MINIMAX_[A-Z_]+)=(.*?)"?\s*',line,re.I)
        if match:values[match[1].upper()]=match[2].removesuffix('"')
    if not values.get('MINIMAX_API_KEY'):raise ValueError('所选 live-avatar 尚未配置 MiniMax')
    endpoint=values.get('MINIMAX_TTS_BASE_URL','https://api.minimax.io/v1/t2a_v2')
    if endpoint not in ENDPOINTS:raise ValueError('不支持此 MiniMax 服务地址')
    result={'api_key':values['MINIMAX_API_KEY'],'base_url':endpoint,
            'model':values.get('MINIMAX_TTS_MODEL','speech-2.8-turbo')}
    directory=Path(data_dir);directory.mkdir(parents=True,exist_ok=True)
    path=directory/'speech-provider.json';temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result,indent=2),encoding='utf-8');temporary.replace(path)
    return {'configured':True,'provider':'MiniMax','automatic_speech_enabled':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-avatar',type=Path,required=True)
    parser.add_argument('--data-dir',type=Path,default=DEFAULT_DATA)
    args=parser.parse_args()
    print(json.dumps(from_live_avatar(args.live_avatar,args.data_dir)))


if __name__=='__main__':main()
