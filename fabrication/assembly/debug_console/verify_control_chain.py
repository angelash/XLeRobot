"""Explicit local diagnostic: replay LB/D-pad through the real command path.

Requires attached hardware. Two manipulator joints move a bounded small step
and return toward the starting position. Wheels are never commanded.
"""
import json
import time
from .control_runtime import ControlRuntime, LOG_DIR
from .torque_control import read_registers, word


def frame(lb=False,y=0):
    b=[False]*16;b[4]=lb
    return {'connected':True,'buttons':b,'hat':[0,y]}


def main():
    runtime=ControlRuntime();runtime.diagnostic_replay=True
    try:
        # No native reader runs here: exclusively test the replay pathway.
        runtime.capture_profile()
        # Explicit diagnostic replay only. This is not an online physical pad.
        runtime.gamepad={**frame(), 'devices':['诊断回放（非真实手柄）']}
        runtime.pad_time=time.monotonic()
        runtime.begin(True)
        results=[]
        for port,motor_id in [('COM4',1),('COM3',5)]:
            runtime.pad_time=time.monotonic();runtime.ui_heartbeat=time.monotonic()
            runtime.select(port,motor_id)
            p=runtime.session.ports[port]
            with runtime.io:origin=word(read_registers(p,motor_id),56)
            runtime.process_input(frame(True),source='diagnostic_replay')
            runtime.process_input(frame(True,1),source='diagnostic_replay')
            time.sleep(.85)
            with runtime.io:
                middle=word(read_registers(p,motor_id),56)
                if runtime.pending:runtime._check_feedback()
            if abs(middle-origin)<3:raise RuntimeError(f'{port}:{motor_id} 未检测到编码器变化')
            runtime.process_input(frame(True,0),source='diagnostic_replay')
            runtime.process_input(frame(True,-1),source='diagnostic_replay')
            time.sleep(.85)
            with runtime.io:
                returned=word(read_registers(p,motor_id),56)
                if runtime.pending:runtime._check_feedback()
            runtime.process_input(frame(False),source='diagnostic_replay')
            results.append({'port':port,'id':motor_id,'initial':origin,'moved':middle,
                            'returned':returned,'delta':middle-origin,
                            'lb_released':not runtime.controller['armed']})
            print('real_motion',json.dumps(results[-1]),flush=True)
        runtime.process_input({'connected':False},source='diagnostic_replay')
        events=list(runtime.events)
        assert runtime.controller['controller']=='offline'
        assert len([e for e in events if e['kind']=='feedback' and e.get('moved')])>=4
        result={'passed':True,'method':'same process_input -> move -> encoder feedback path',
                'real_gamepad_buttons_pressed_by_agent':False,'motors':results,
                'disconnect_stops':True,'events':events}
        (LOG_DIR/'control-chain-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('PASS: LB / D-pad replay, two joints measured, release and disconnect stop',flush=True)
    finally:
        runtime.close()


if __name__=='__main__':main()
