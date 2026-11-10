import json,struct
import pytest
from deployment.phone_vision.arcore_capture import HEADER,parse_snapshot


def test_atomic_native_packet_preserves_depth_and_confidence_and_rejects_mixed_lengths():
    meta={'schema':1,'capture_session':'local-sample','depth_width':2,'depth_height':1,'raw_depth_width':2,'raw_depth_height':1,'confidence_width':2,'confidence_height':1}
    parts=[json.dumps(meta).encode(),b'\xff\xd8generated-jpeg',struct.pack('<HH',1000,0),struct.pack('<HH',900,0),bytes([220,0])]
    raw=HEADER.pack(b'XLAR1\0\0\0',*(len(p) for p in parts))+b''.join(parts)
    parsed=parse_snapshot(raw)
    assert parsed[0]['capture_session']=='local-sample' and parsed[2:]==tuple(parts[2:])
    with pytest.raises(ValueError,match='bounds'):parse_snapshot(raw[:-1])
    parts[4]=b'bad length';raw=HEADER.pack(b'XLAR1\0\0\0',*(len(p) for p in parts))+b''.join(parts)
    with pytest.raises(ValueError,match='dimensions'):parse_snapshot(raw)
