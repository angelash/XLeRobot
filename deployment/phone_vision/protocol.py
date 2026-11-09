"""Bounded local pipe messages between Studio and its camera subprocesses."""
import json
import struct

MAGIC = b'XLPV1\0\0\0'
HEADER = struct.Struct('>8sIIII')
LIMITS = (16384, 3_000_000, 2_000_000, 8_000_000)


def read_exact(stream, count):
    data=bytearray()
    while len(data)<count:
        part=stream.read(count-len(data))
        if not part:
            raise EOFError('Camera worker pipe closed')
        data.extend(part)
    return bytes(data)


def read_message(stream):
    magic,*sizes=HEADER.unpack(read_exact(stream,HEADER.size))
    if magic!=MAGIC or any(size>limit for size,limit in zip(sizes,LIMITS)):
        raise ValueError('Invalid phone camera packet')
    pieces=[read_exact(stream,size) for size in sizes]
    meta=json.loads(pieces[0])
    if not isinstance(meta,dict) or meta.get('kind') not in ('frame','status'):
        raise ValueError('Invalid phone camera metadata')
    if meta['kind']=='frame':
        width,height=meta.get('width'),meta.get('height')
        if type(width)!=int or type(height)!=int or not 1<=width<=4096 or not 1<=height<=4096:
            raise ValueError('Invalid image dimensions')
        if not pieces[1].startswith(b'\xff\xd8'):
            raise ValueError('Phone camera frame must be JPEG')
        if pieces[3] and len(pieces[3])!=width*height*4:
            raise ValueError('Depth dimensions do not match raw array')
    return meta,*pieces[1:]


def write_message(stream, meta, rgb=b'', depth=b'', raw=b''):
    encoded=json.dumps(meta,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
    pieces=(encoded,rgb,depth,raw)
    if any(len(value)>limit for value,limit in zip(pieces,LIMITS)):
        raise ValueError('Phone camera packet exceeds limits')
    def write_all(value):
        remaining=memoryview(value)
        while remaining:
            count=stream.write(remaining)
            if not count:raise BrokenPipeError('Camera pipe write interrupted')
            remaining=remaining[count:]
    write_all(HEADER.pack(MAGIC,*(len(value) for value in pieces)))
    for value in pieces:write_all(value)
    stream.flush()
