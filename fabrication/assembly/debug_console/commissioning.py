"""Explicit maintenance of bounded trial ranges under one serial owner.

These are local test windows, never a full mechanical range or an IK reference.
Goal writes on the observed firmware enable torque; limit output to zero until
the goal is latched, torque is independently disabled and configuration agrees.
"""
from .torque_control import read_registers,word,write_register


def _zero_output(port,motor_id):
    write_register(port,motor_id,48,0,2)
    write_register(port,motor_id,40,0,1)
    r=read_registers(port,motor_id)
    if word(r,48)!=0 or r[40]!=0:
        raise RuntimeError('不能确认零输出及扭矩关闭，停止标定写入')
    return r


def _limits(port,motor_id,low,high):
    r=read_registers(port,motor_id)
    if word(r,48)!=0 or r[40]!=0:raise RuntimeError('限位写入需要零输出并关闭扭矩')
    write_register(port,motor_id,55,0,1)
    # Avoid an invalid intermediate interval when the current pose is already
    # completely outside a stale interval.
    order=[(11,high),(9,low)] if low>word(r,11) else [(9,low),(11,high)]
    for address,value in order:write_register(port,motor_id,address,value,2)
    r=read_registers(port,motor_id)
    if (word(r,9),word(r,11))!=(low,high):raise RuntimeError('限位回读不一致')


def _finish_off(port,motor_id,before,expected_limits,speed):
    r=read_registers(port,motor_id)
    if word(r,48)!=0:raise RuntimeError('写保持目标前输出上限不是0')
    pos=word(r,56);low,high=expected_limits
    write_register(port,motor_id,46,speed,2)
    write_register(port,motor_id,42,max(low,min(high,pos)),2)
    write_register(port,motor_id,40,0,1)
    write_register(port,motor_id,55,before[55],1)
    r=read_registers(port,motor_id)
    if r[40]!=0 or word(r,48)!=0:raise RuntimeError('不能确认目标写入后的独立关扭矩')
    if (word(r,9),word(r,11))!=expected_limits or r[55]!=before[55]:raise RuntimeError('范围/写锁回读不一致')
    for address in range(40):
        if address not in (9,10,11,12) and r[address]!=before[address]:
            raise RuntimeError('非限位EEPROM参数发生变化')
    write_register(port,motor_id,48,word(before,48),2)
    final=read_registers(port,motor_id)
    if final[40]!=0 or word(final,48)!=word(before,48):raise RuntimeError('输出上限恢复后扭矩状态不一致')
    return final


def restore_test_window(port,motor_id,before):
    """Restore a backed-up interval, leaving torque off even on failure."""
    try:
        _zero_output(port,motor_id)
        expected=(word(before,9),word(before,11))
        _limits(port,motor_id,*expected)
        for address in (21,22):
            write_register(port,motor_id,address,before[address],1)
        return _finish_off(port,motor_id,before,expected,word(before,46))
    except Exception:
        # If restoration cannot be verified, retain zero output rather than
        # re-enable an uncertain configuration through a later goal write.
        _zero_output(port,motor_id)
        raise


def configure_test_window(port,motor_id,save_backup,half_width=32,position_gains=None):
    """Persist the backup before establishing a maximum +/-32-count window."""
    if not callable(save_backup) or not 16<=half_width<=32:
        raise ValueError('需要持久备份回调及16–32计数的半窗口')
    if position_gains not in (None,(32,32)):
        raise ValueError('本工具只允许显式对照厂家P32/D32默认值')
    before=read_registers(port,motor_id)
    if word(before,3)!=777 or before[33]!=0 or before[40]!=0:
        raise ValueError('试控范围准备需要STS3215、位置模式和扭矩关闭')
    if not 10<=before[62]/10<=13 or before[63]>=50 or (word(before,60)&0x3ff)>500:
        raise ValueError('电压、温度或负载不满足试控准备')
    pos=word(before,56)
    if not 0<=pos<=4095 or not 0<=word(before,9)<word(before,11)<=4095:
        raise ValueError('位置或原有限位无效')
    again=read_registers(port,motor_id)
    if abs(word(again,56)-pos)>3:raise ValueError('关节还在手动移动，暂不写入试控范围')
    save_backup(list(before))
    low,high=max(0,pos-half_width),min(4095,pos+half_width)
    try:
        _zero_output(port,motor_id)
        _limits(port,motor_id,low,high)
        configured=list(before)
        if position_gains is not None:
            for address,value in zip((21,22),position_gains):
                write_register(port,motor_id,address,value,1);configured[address]=value
        after=_finish_off(port,motor_id,configured,(low,high),60)
        if abs(word(after,56)-pos)>3:raise RuntimeError('零输出准备期间编码器发生位移')
        return {'before':before,'after':after,'min':low,'max':high,'origin':pos,
                'full_manual_range_verified':False}
    except Exception as error:
        try:restore_test_window(port,motor_id,before)
        except Exception as cleanup:raise RuntimeError(f'{error}；回滚未确认：{cleanup}') from error
        raise
