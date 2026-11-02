"""Host resource snapshot without third-party dependencies (no psutil on the laptop)."""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import threading
import time

_cpu_lock = threading.Lock()
_cpu_previous = None
_gpu_cache = {'at': -1e9, 'value': None}


class _MemoryStatus(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong), ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong), ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong), ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong), ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


class _ProcessCounters(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong), ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t), ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t), ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t), ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t)]


def _windows_memory():
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    counters = _ProcessCounters()
    counters.cb = ctypes.sizeof(_ProcessCounters)
    rss = None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessCounters), ctypes.c_ulong]
        if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            rss = counters.WorkingSetSize
    except (OSError, AttributeError):
        pass
    return {'total_mb': round(status.ullTotalPhys/2**20), 'available_mb': round(status.ullAvailPhys/2**20),
            'load_percent': int(status.dwMemoryLoad), 'process_mb': None if rss is None else round(rss/2**20)}


def _windows_cpu():
    """System CPU busy percentage since the previous call (None on the first call)."""
    global _cpu_previous
    idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    current = (idle.value, kernel.value+user.value, time.monotonic())
    with _cpu_lock:
        previous, _cpu_previous = _cpu_previous, current
    if not previous or current[2]-previous[2] < 0.2:
        return None
    total = current[1]-previous[1]
    return None if total <= 0 else round(100*(1-(current[0]-previous[0])/total), 1)


def _linux_memory():
    try:
        values = {}
        with open('/proc/meminfo', encoding='ascii') as handle:
            for line in handle:
                key, value = line.split(':', 1)
                values[key] = int(value.split()[0])
        return {'total_mb': round(values['MemTotal']/1024), 'available_mb': round(values['MemAvailable']/1024),
                'load_percent': round(100*(1-values['MemAvailable']/values['MemTotal'])), 'process_mb': None}
    except (OSError, KeyError, ValueError):
        return None


def gpu(max_age=30.):
    """NVIDIA GPU memory via nvidia-smi, cached; None when unavailable."""
    now = time.monotonic()
    if now-_gpu_cache['at'] < max_age:
        return _gpu_cache['value']
    value = None
    tool = shutil.which('nvidia-smi')
    if tool:
        try:
            output = subprocess.run([tool, '--query-gpu=name,memory.total,memory.used,utilization.gpu',
                                     '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=4,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
            value = []
            for line in output.strip().splitlines():
                name, total, used, util = (part.strip() for part in line.split(','))
                value.append({'name': name, 'memory_total_mb': int(total), 'memory_used_mb': int(used),
                              'memory_free_mb': int(total)-int(used),
                              'utilization_percent': int(util) if util.isdigit() else None})
        except (OSError, ValueError, subprocess.SubprocessError):
            value = None
    _gpu_cache.update(at=now, value=value)
    return value


def snapshot(include_gpu=False):
    windows = os.name == 'nt'
    memory = _windows_memory() if windows else _linux_memory()
    cpu = _windows_cpu() if windows else None
    if cpu is None and not windows and hasattr(os, 'getloadavg'):
        cpu = round(100*os.getloadavg()[0]/max(1, os.cpu_count() or 1), 1)
    result = {'host': platform.node(), 'platform': platform.platform(), 'python': platform.python_version(),
              'logical_cpus': os.cpu_count(), 'cpu_percent': cpu, 'memory': memory, 'pid': os.getpid()}
    if include_gpu:
        result['gpu'] = gpu()
    return result
