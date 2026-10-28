import sys
from unittest.mock import patch

from first_motion_controller import windows_ch343_ports


def test_ch343_serial_mapping_parses_one_device():
    sample = '[{"DeviceID":"COM7","PNPDeviceID":"USB\\\\VID_1A86&PID_55D3\\\\5B61034756"}]'
    with patch.object(sys, "platform", "win32"), patch("subprocess.run") as run:
        run.return_value.stdout = sample
        assert windows_ch343_ports() == {"5B61034756": "COM7"}


def test_ch343_serial_mapping_parses_two_devices():
    sample = ('[{"DeviceID":"COM8","PNPDeviceID":"USB\\\\VID_1A86&PID_55D3\\\\5B61034338"},'
              '{"DeviceID":"COM9","PNPDeviceID":"USB\\\\VID_1A86&PID_55D3\\\\5B61034756"}]')
    with patch.object(sys, "platform", "win32"), patch("subprocess.run") as run:
        run.return_value.stdout = sample
        assert windows_ch343_ports() == {"5B61034338": "COM8", "5B61034756": "COM9"}
