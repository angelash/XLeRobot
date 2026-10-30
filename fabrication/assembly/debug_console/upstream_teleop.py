"""Upstream Xbox semantics, dependency-free kinematics and deterministic preview.

Adapted from software/examples/5_xlerobot_teleop_xbox.py and SO101Robot.py.
No serial, SDL, framework or hardware imports. Angles are in degrees;
gripper uses the upstream RANGE_0_100 normalized command (2 closed / 90 open).
"""
from __future__ import annotations
import math
from copy import deepcopy

L1,L2=0.1159,0.1350
O1=math.atan2(.028,.11257)
O2=math.atan2(.0052,.1349)+O1
JOINTS=('pan','lift','elbow','wrist','roll','grip')


def forward(lift,elbow):
    # Inverse of upstream IK: theta2 is elbow bend, not its supplement.
    a=math.radians(90-lift)-O1
    b=math.radians(elbow+90)-O2
    return (L1*math.cos(a)+L2*math.cos(a-b),
            L1*math.sin(a)+L2*math.sin(a-b))


def inverse(x,z):
    r=math.hypot(x,z)
    if not abs(L1-L2)+1e-6<r<L1+L2-1e-6:
        raise ValueError('末端超出二连杆工作空间')
    b=math.acos(max(-1,min(1,(r*r-L1*L1-L2*L2)/(2*L1*L2))))
    a=math.atan2(z,x)+math.atan2(L2*math.sin(b),L1+L2*math.cos(b))
    q2=90-math.degrees(a+O1);q3=math.degrees(b+O2)-90
    if not -.1<=a+O1<=3.45 or not -.2<=b+O2<=math.pi:
        raise ValueError('末端逆解超过上游模型关节范围')
    return q2,q3


def axis(value,deadzone=.15):
    value=max(-1.,min(1.,float(value)))
    if abs(value)<=deadzone:return 0.
    return math.copysign((abs(value)-deadzone)/(1-deadzone),value)


def neutral(raw):
    if not raw.get('connected'):return False
    return (not any(raw.get('buttons',[])) and not any(raw.get('hat',[0,0]))
            and all(abs(float(x))<=.15 for x in raw.get('axes',[])[:4])
            and all(float(x)<=.12 for x in raw.get('axes',[])[4:6]))


def intentions(raw):
    b=raw.get('buttons',[]);a=raw.get('axes',[])
    pressed=lambda i:bool(b[i]) if i<len(b) else False
    get=lambda i:axis(a[i]) if i<len(a) else 0.
    result={}
    for side,offset,bumper,click,trigger in [('left',0,4,8,4),('right',2,5,9,5)]:
        mode='wrist' if pressed(bumper) else 'pan' if pressed(click) else 'position'
        result[side]={'mode':mode,'h':get(offset),'v':get(offset+1),
                      'trigger':max(0,min(1,float(a[trigger]))) if trigger<len(a) else 0.}
    h=raw.get('hat',[0,0])
    # Upstream get_base_action: up -> 'k' (commented "Forward"). Although the
    # 2-wheel config names 'k' backward in its x frame, a field test on
    # 2026-10-30 confirmed up drives this robot's physical front. Keep the
    # upstream sign; the "forward" short test uses the same D-pad-up command.
    result['base']={'v':-.04*h[1],'omega':-.25*h[0]}
    result['head']={'pan':int(pressed(2))-int(pressed(1)),
                    'tilt':int(pressed(0))-int(pressed(3))}
    result['stop']=pressed(6)
    result['pause']=pressed(7)
    return result


def wheel_raw(v,omega,radius=.05,track=.25,max_raw=500):
    # Same mirrored-left differential geometry as the upstream 2-wheel driver.
    values=[-(v-omega*track/2)/radius,(v+omega*track/2)/radius]
    raw=[round(x*4096/(2*math.pi)) for x in values]
    scale=min(1,max_raw/max(1,max(abs(x) for x in raw)))
    return [round(x*scale) for x in raw]


def encode_velocity(value):
    value=round(value)
    if abs(value)>32767:raise ValueError('轮速超过寄存器范围')
    return abs(value)|(0x8000 if value<0 else 0)


class UpstreamTeleop:
    def __init__(self,angles=None,radius=.05,track=.25,max_raw=500):
        self.radius=radius;self.track=track;self.max_raw=max_raw
        self.q=deepcopy(angles or {s:dict(zip(JOINTS,[0,0,2,-2,0,90])) for s in ('left','right')})
        self.head=[0.,0.];self.base=[0.,0.,0.];self.wheels=[0.,0.]
        self.speed_level=1;self.last_bumpers=[False,False]
        self.last_input={};self.blockers=[]
        self.bounds={s:{j:(-120.,120.) for j in JOINTS} for s in self.q}
        for s in self.q:self.bounds[s]['grip']=(2.,90.)
        self.reset_origins()

    def reset_origins(self):
        self.origins=deepcopy(self.q)
        self.pitch={s:self.q[s]['lift']+self.q[s]['elbow']+self.q[s]['wrist'] for s in self.q}

    def step(self,raw,dt):
        dt=max(0,min(.1,float(dt)));intent=intentions(raw)
        self.last_input=intent;self.blockers=[]
        for side in self.q:
            q=self.q[side];i=intent[side];candidate=q.copy()
            if i['mode']=='position':
                x,z=forward(q['lift'],q['elbow'])
                # Exact upstream plane semantics: vertical stick -> radial x,
                # horizontal stick -> plane height (upstream calls it y).
                x-=i['v']*.01*dt;z+=i['h']*.01*dt
                if i['v'] or i['h']:
                    try:candidate['lift'],candidate['elbow']=inverse(x,z)
                    except ValueError as e:self.blockers.append(side+': '+str(e))
                    candidate['wrist']=self.pitch[side]-candidate['lift']-candidate['elbow']
            elif i['mode']=='pan':candidate['pan']+=i['h']*5*dt
            else:
                candidate['roll']+=i['h']*5*dt
                candidate['wrist']-=i['v']*5*dt
            if all(self.bounds[side][j][0]<=candidate[j]<=self.bounds[side][j][1] for j in JOINTS):
                self.q[side]=candidate
                if i['mode']=='wrist':self.pitch[side]=candidate['lift']+candidate['elbow']+candidate['wrist']
            else:self.blockers.append(side+': 关节范围边界，保留目标，可反向')
            # Upstream closes with trigger and opens on release. Slew-limit it.
            target=2. if i['trigger']>.75 else 90.
            q=self.q[side];delta=max(-20*dt,min(20*dt,target-q['grip']))
            low,high=self.bounds[side]['grip']
            q['grip']=max(low,min(high,q['grip']+delta))
        self.head[0]=max(-90,min(90,self.head[0]+intent['head']['pan']*5*dt))
        self.head[1]=max(-40,min(70,self.head[1]+intent['head']['tilt']*5*dt))
        b=raw.get('buttons',[])
        bumpers=[bool(b[i]) if len(b)>i else False for i in (4,5)]
        # Preserve base speed keys, but edge-trigger and only while driving.
        if any(raw.get('hat',[0,0])):
            if bumpers[0] and not self.last_bumpers[0]:self.speed_level=max(1,self.speed_level-1)
            if bumpers[1] and not self.last_bumpers[1]:self.speed_level=min(3,self.speed_level+1)
        self.last_bumpers=bumpers
        v=intent['base']['v']*self.speed_level;w=intent['base']['omega']*self.speed_level
        self.wheels=wheel_raw(v,w,self.radius,self.track,self.max_raw)
        self.base[0]+=v*math.cos(self.base[2])*dt
        self.base[1]+=v*math.sin(self.base[2])*dt
        self.base[2]+=w*dt
        return self.snapshot()

    def snapshot(self):
        return {'arms':{s:{'joints':dict(q),'plane_m':list(forward(q['lift'],q['elbow'])),
                          'mode':self.last_input.get(s,{}).get('mode','position')} for s,q in self.q.items()},
                'head_deg':list(self.head),'base_pose':list(self.base),'wheel_raw':list(self.wheels),
                'speed_level':self.speed_level,'blockers':list(self.blockers),
                'kind':'kinematic_preview_not_physics','frame':'各臂二连杆平面：径向距离/高度'}
