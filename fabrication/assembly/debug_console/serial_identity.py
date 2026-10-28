"""Stable robot bus roles across Windows COM reassignment; enumeration only."""
EXPECTED_SERIALS = {'COM3': '5B61034756', 'COM4': '5B61034338'}


def resolve_ports(devices=None):
    if devices is None:
        from serial.tools.list_ports import comports
        devices = list(comports())
    devices = list(devices)
    resolved = {}
    for role, identity in EXPECTED_SERIALS.items():
        matches = [d.device for d in devices if d.serial_number == identity]
        if len(matches) != 1:
            raise RuntimeError(f'{role} USB身份 {identity} 应恰好出现一次，当前匹配 {matches}')
        resolved[role] = matches[0]
    if len(set(resolved.values())) != len(resolved):
        raise RuntimeError('两块控制板不能指向同一串口')
    return resolved
