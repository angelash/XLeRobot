"""Installer preflight must preserve local edits before copying anything."""
import hashlib
import json
import pytest
from deployment.phone_vision.install_kit import install


def make_kit(tmp_path):
    kit=tmp_path/'kit';kit.mkdir()
    robot=tmp_path/'robot'
    marker=robot/'fabrication/assembly/debug_console/platform_app.py'
    marker.parent.mkdir(parents=True);marker.write_bytes(b'local user edit\n')
    source=kit/'source/fabrication/assembly/debug_console/platform_app.py'
    source.parent.mkdir(parents=True);source.write_bytes(b'new source\n')
    sha=lambda data:hashlib.sha256(data).hexdigest()
    manifest={'files':[{'path':str(source.relative_to(kit)),'bytes':source.stat().st_size,'sha256':sha(source.read_bytes())}],
              'source_files':[{'target':str(marker.relative_to(robot)),'base_text_sha256':sha(b'old base\n'),'text_sha256':sha(b'new source\n')}]}
    (kit/'manifest.json').write_text(json.dumps(manifest))
    return kit,robot,marker,source


def test_existing_source_edits_abort_before_native_or_file_changes(tmp_path):
    kit,robot,marker,source=make_kit(tmp_path)
    with pytest.raises(ValueError,match='已有其他修改'):install(kit,robot)
    assert marker.read_bytes()==b'local user edit\n'
    assert not (robot/'.local-deploy').exists()


def test_corrupt_package_aborts_before_touching_existing_source(tmp_path):
    kit,robot,marker,source=make_kit(tmp_path);source.write_bytes(b'changed package')
    with pytest.raises(ValueError,match='安装包校验失败'):install(kit,robot)
    assert marker.read_bytes()==b'local user edit\n'
