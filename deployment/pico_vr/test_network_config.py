"""Run the real config merge block with Windows PowerShell, without firewall calls."""
import json
import os
from pathlib import Path

import pytest

from deployment.pico_vr.test_install_package import ps

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell network configuration')


@pytest.mark.parametrize('previous', [None, {
    'fieldmap_data_dir': 'D:\\Robot\\FieldMap',
    'tls': {'cert': 'existing.crt', 'key': 'existing.key', 'ca': 'existing-ca.crt'},
    'future_setting': {'nested': 'keep-me'},
    'client_addresses': ['192.168.137.1'],
}])
def test_adding_headset_keeps_existing_map_root_tls_and_unknown_settings(tmp_path, previous):
    source = Path(__file__).parents[1] / 'windows/configure-studio-network.ps1'
    config_path = tmp_path / 'network.json'
    if previous is not None:
        config_path.write_text(json.dumps(previous), encoding='utf-8')
    script = tmp_path / 'merge.ps1'
    script.write_text(r'''
param($Source,$Path)
$ErrorActionPreference='Stop'
$sourceText=[System.IO.File]::ReadAllText($Source)
$start=$sourceText.IndexOf('$previous = if (Test-Path')
$end=$sourceText.IndexOf('New-Item -ItemType Directory -Path (Split-Path -Parent $path)',$start)
if($start -lt 0 -or $end -lt 0) {throw 'Cannot locate actual config merge'}
$ListenAddress='192.168.137.150';$origin='http://192.168.137.150:8770'
$ClientAddress=@('192.168.137.1','192.168.137.124')
. ([scriptblock]::Create($sourceText.Substring($start,$end-$start)))
$config | ConvertTo-Json -Depth 8 -Compress
''', encoding='utf-8')
    result = ps(script, source, config_path)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(result.stdout)
    assert actual['listen_host'] == '0.0.0.0'
    assert actual['public_origin'] == 'http://192.168.137.150:8770'
    assert actual['client_addresses'] == ['192.168.137.1', '192.168.137.124']
    if previous:
        assert actual['fieldmap_data_dir'] == previous['fieldmap_data_dir']
        assert actual['tls'] == previous['tls']
        assert actual['future_setting'] == previous['future_setting']
        assert actual['allowed_origins'] == ['http://192.168.137.150:8770', 'https://192.168.137.150:8770']
    else:
        assert 'tls' not in actual
        assert actual['allowed_origins'] == ['http://192.168.137.150:8770']
