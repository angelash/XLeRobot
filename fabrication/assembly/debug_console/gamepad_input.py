"""Windows Xbox state from XInput: no focus/event-pump or cached SDL handles."""
import ctypes
from ctypes import wintypes


class Gamepad(ctypes.Structure):
    _fields_=[('buttons',wintypes.WORD),('lt',ctypes.c_ubyte),('rt',ctypes.c_ubyte),
              ('lx',ctypes.c_short),('ly',ctypes.c_short),('rx',ctypes.c_short),('ry',ctypes.c_short)]


class State(ctypes.Structure):
    _fields_=[('packet',wintypes.DWORD),('pad',Gamepad)]


class XboxReader:
    def __init__(self):
        self.dll=ctypes.WinDLL('xinput1_4')
        self.dll.XInputGetState.argtypes=[wintypes.DWORD,ctypes.POINTER(State)]
        self.dll.XInputGetState.restype=wintypes.DWORD

    def read(self):
        for slot in range(4):
            state=State()
            if self.dll.XInputGetState(slot,ctypes.byref(state))!=0:continue
            p=state.pad
            masks=[0x1000,0x2000,0x4000,0x8000,0x100,0x200,0x20,0x10,0x40,0x80]
            buttons=[bool(p.buttons & x) for x in masks]+[p.lt>30,p.rt>30,
                        bool(p.buttons&1),bool(p.buttons&2),bool(p.buttons&4),bool(p.buttons&8),False]
            axes=[max(-1,round(v/32767,3)) for v in (p.lx,-p.ly,p.rx,-p.ry)]+[round(p.lt/255,3),round(p.rt/255,3)]
            return {'connected':True,'name':f'Xbox / XInput 槽 {slot}',
                'devices':[f'Xbox / XInput 槽 {slot}'],'buttons':buttons,'axes':axes,
                'hat':[int(bool(p.buttons&8))-int(bool(p.buttons&4)),int(bool(p.buttons&1))-int(bool(p.buttons&2))],
                'mapping':{'a':'b0','b':'b1','x':'b2','y':'b3','leftshoulder':'b4','rightshoulder':'b5',
                    'back':'b6','start':'b7','leftstick':'b8','rightstick':'b9','lefttrigger':'b10',
                    'righttrigger':'b11','dpup':'b12','dpdown':'b13','dpleft':'b14','dpright':'b15','guide':'b16'},
                'lb_index':4,'back_index':6,'unavailable_buttons':[16],'packet_number':state.packet,
                'axis_labels':['左摇杆X','左摇杆Y','右摇杆X','右摇杆Y','LT扳机','RT扳机']}
        return {'connected':False,'devices':[],'buttons':[],'axes':[],'hat':[0,0]}
