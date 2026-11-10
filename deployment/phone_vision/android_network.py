"""Explicit phone transport selection and bounded relative video backlog."""
import ipaddress
import math


def wifi_endpoint(value):
    if not isinstance(value,str) or value.count(':')!=1:raise ValueError('需要局域网 IPv4:端口')
    address,port=value.split(':')
    try:
        ip=ipaddress.IPv4Address(address)
        number=int(port)
    except (ValueError,TypeError):raise ValueError('无线地址格式无效') from None
    networks=('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')
    if not any(ip in ipaddress.IPv4Network(n) for n in networks) or not 1024<=number<=65535:
        raise ValueError('无线摄像头只接受局域网地址和非系统端口')
    if str(ip)!=address or str(number)!=port:raise ValueError('无线地址格式无效')
    return value


def select_device(rows,transport,endpoint=''):
    """Never switch to another phone when the configured rear camera is absent."""
    if transport=='wifi':
        endpoint=wifi_endpoint(endpoint)
        state=next((row[1] for row in rows if len(row)>1 and row[0]==endpoint),None)
        if state=='device':return endpoint,None
        if state=='unauthorized':return None,'请在红米上允许这台车载电脑调试，再等待连接'
        return None,'后视手机 Wi-Fi 未连接，请检查热点、手机电量与网络调试'
    if transport!='usb':raise ValueError('Invalid Android transport')
    candidates=[row[0] for row in rows if len(row)>1 and row[1]=='device'
                and ':' not in row[0] and not row[0].endswith('._tcp')]
    if len(candidates)!=1:return None,'请通过 USB 连接一台安卓手机，解锁并接受调试授权'
    return candidates[0],None


class VideoClock:
    """Delay above the best observed transport delay, not absolute optical latency."""
    def __init__(self):self.offset=None

    def observe(self,pts_us,received):
        if type(pts_us)!=int or pts_us<0 or not math.isfinite(received):raise ValueError('Invalid video clock')
        offset=received-pts_us/1_000_000
        self.offset=offset if self.offset is None else min(self.offset,offset)
        lag=max(0.,offset-self.offset)
        return received-lag,lag
