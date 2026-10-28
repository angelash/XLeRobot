"""Inventory only: no serial open, camera capture, torque or movement."""
import json
import platform
import sys
from pathlib import Path


def report():
    import serial.tools.list_ports
    from fabrication.assembly.debug_console.serial_identity import resolve_ports
    ports=list(serial.tools.list_ports.comports())
    data={'python':sys.version,'executable':sys.executable,'architecture':platform.machine(),
          'serial_ports':[{'device':p.device,'serial_number':p.serial_number,'description':p.description,'vid':p.vid,'pid':p.pid} for p in ports],
          'serial_opened':False,'camera_opened':False,'hardware_writes':False}
    try:data['bus_mapping']=resolve_ports(ports)
    except RuntimeError as exc:data['bus_mapping_error']=str(exc)
    if sys.platform=='win32':
        try:
            from fabrication.assembly.debug_console.gamepad_input import XboxReader
            pad=XboxReader().read()
            data['xinput']={k:pad.get(k) for k in ['connected','name','devices','buttons','axes','hat']}
        except (OSError,ImportError) as exc:data['xinput_error']=str(exc)
    return data


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(report(),ensure_ascii=False,indent=2))
