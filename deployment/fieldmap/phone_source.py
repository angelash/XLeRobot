"""Choose an available phone for observation, without granting motion."""
from __future__ import annotations


def phone_observation(phones):
    devices=phones.get('devices',{})
    # Prefer the independent depth phone when present; fall back to Android
    # only when it actually has a fresh frame. Disabled/stale data never wins.
    selected=next((key for key in ('iphone','android')
                   if devices.get(key,{}).get('enabled') and
                   devices[key].get('frame_available')),None)
    source=devices.get(selected,{}) if selected else {}
    meta=source.get('meta') or {}
    return dict(device=selected,phase=source.get('phase','waiting'),
                frame_available=bool(selected),mode=source.get('mode'),
                session=source.get('session'),sequence=source.get('sequence'),
                rgbd=meta.get('rgbd',False),tracking=meta.get('tracking'),
                depth_source=meta.get('depth_source'),
                sensor_raw_depth=meta.get('sensor_raw_depth'),
                positive_depth_fraction=meta.get('positive_depth_fraction'),
                depth_02_to_5m_fraction=meta.get('depth_02_to_5m_fraction'),
                metric_clearance_verified=False)


def capture_phone(service,evidence):
    if service is None or evidence is None:return None
    snapshot=service.status()
    first=phone_observation(snapshot)['device']
    if first is None:return None
    for device in (first,)+tuple(d for d in ('iphone','android') if d!=first):
        source=snapshot.get('devices',{}).get(device,{})
        if not source.get('enabled') or not source.get('frame_available'):continue
        try:frame=service.frame(device)
        except LookupError:continue
        return evidence.capture(device,frame,'本地扫描手机观察；保留深度来源，未做度量验收')
    return None
