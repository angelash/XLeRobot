"""Optional shared secret for state-changing Studio requests from other machines; off unless configured.

With XLEROBOT_STUDIO_TOKEN unset nothing changes. When it is set, a request that changes state and comes
from another machine must carry it in the X-XLeRobot-Token header or in the cookie the /access page sets.
Requests from this computer (the on-board browser, local services, an SSH tunnel) are not asked, and worker
routes keep their own token. Reads stay open.
"""
import hmac
import os

ENV='XLEROBOT_STUDIO_TOKEN'
HEADER='x-xlerobot-token'
COOKIE='xlerobot_studio'
LOGIN='/api/platform/access'
OWN_TOKEN=('/api/platform/autonomy/worker/',)
PAGE='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XLeRobot Studio 访问口令</title>
<body style="font:16px system-ui;max-width:26rem;margin:4rem auto;padding:0 1rem">
<h1 style="font-size:1.2rem">XLeRobot Studio 访问口令</h1>
<p>从其他设备操作这台机器人前，输入一次口令。口令只保存在这个浏览器里。</p>
<form id="f"><input id="t" type="password" autocomplete="current-password" required style="width:100%;padding:.5rem;box-sizing:border-box">
<button style="margin-top:.75rem;padding:.5rem 1rem">确认</button></form><p id="m" role="status"></p>
<script>
document.getElementById('f').addEventListener('submit',async e=>{e.preventDefault();
  const r=await fetch('/api/platform/access',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:document.getElementById('t').value})});
  document.getElementById('m').textContent=r.ok?'已确认，可以回到 Studio。':'口令不对。';
  if(r.ok)location.href='/';});
</script></body></html>'''


def configured(environ=os.environ):
    """The configured secret, or None when the check is off. A short secret stops the start-up."""
    token=environ.get(ENV,'').strip()
    if token and len(token)<16:raise ValueError(ENV+' must be at least 16 characters')
    return token or None


def matches(given,token):
    return bool(given) and bool(token) and hmac.compare_digest(given.encode(),token.encode())


def allowed(request,token):
    if not token or request.method in ('GET','HEAD','OPTIONS'):return True
    if request.client and request.client.host in ('127.0.0.1','::1'):return True
    path=request.url.path
    if path==LOGIN or path.startswith(OWN_TOKEN):return True
    return matches(request.headers.get(HEADER),token) or matches(request.cookies.get(COOKIE),token)
