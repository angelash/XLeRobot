"""Split Jom's static arm into rigid links and author display joint pivots.

Triangle positions, UVs, normals, material definitions and embedded images are
retained. Connected components are classified as whole rigid pieces, so seams
are never sliced across a triangle. This is a visual rig, not robot calibration.
"""
from __future__ import annotations

import argparse
import copy
from hashlib import sha256
import json
from pathlib import Path
import struct

import numpy as np


PIVOTS = {'ceiling': [-.25696,1.218,.01375], 'elbow': [.67139,.67,.01375],
          'wrist': [.67139,-.65122,.01375], 'body': [.14368,-.65122,.01375]}


def rig(data):
    size=struct.unpack_from('<I',data,12)[0]
    document=json.loads(data[20:20+size]);binary=bytearray(data[28+size:])
    if any(node.get('name')=='MOSS Ceiling Swivel' for node in document['nodes']):
        raise ValueError('Model is already rigged; start from the unrigged conversion')
    def values(index):
        accessor=document['accessors'][index];view=document['bufferViews'][accessor['bufferView']]
        return np.frombuffer(binary,dtype='<f4',count=accessor['count']*3,
                     offset=view.get('byteOffset',0)+accessor.get('byteOffset',0)).reshape(-1,3).copy()
    arm_index=next(index for index,mesh in enumerate(document['meshes'])
                   if document['materials'][mesh['primitives'][0]['material']]['name']=='Moss_arm')
    primitive=document['meshes'][arm_index]['primitives'][0]
    if 'indices' in primitive:raise ValueError('Expected the original non-indexed FBX conversion')
    positions=values(primitive['attributes']['POSITION']);faces=np.arange(len(positions)).reshape(-1,3)
    _,inverse=np.unique(np.round(positions,5),axis=0,return_inverse=True)
    parents=list(range(int(inverse.max())+1))
    def find(index):
        while parents[index]!=index:parents[index]=parents[parents[index]];index=parents[index]
        return index
    for a,b,c in inverse[faces]:
        ra,rb,rc=find(a),find(b),find(c);parents[rb]=ra;parents[rc]=ra
    components={}
    for index,face in enumerate(faces):components.setdefault(find(inverse[face[0]]),[]).append(index)
    parts={name:[] for name in ['fixed','upper','shaft','lower']}
    for indices in components.values():
        points=positions[faces[indices].reshape(-1)]
        low,high=points.min(axis=0),points.max(axis=0);center=(low+high)/2
        name='fixed' if low[1]>1.19 else 'upper' if center[1]>.70 else 'lower' if center[1]<-.45 else 'shaft'
        parts[name].extend(indices)
    original_meshes=document['meshes'];meshes=[]
    nodes=[]
    def node(name,parent=None,translation=None,**extra):
        index=len(nodes);entry={'name':name,**extra}
        if translation is not None:entry['translation']=list(translation)
        nodes.append(entry)
        if parent is not None:nodes[parent].setdefault('children',[]).append(index)
        return index
    root=node('MOSS Suspended Assembly',extras={
        'displayRig':{'version':1,'ceilingY':1.575,'pivots':PIVOTS,'axes':{'ceiling':'Y','elbow':'Y','wrist':'Y','body':'X'},
                      'note':'Visual pivots inferred from geometry; not engineering measurements or hardware calibration.'}})
    def pivot(name,key,parent,reference):
        return node(name,parent,np.subtract(PIVOTS[key],reference).tolist())
    shoulder=pivot('MOSS Ceiling Swivel','ceiling',root,[0,0,0])
    elbow=pivot('MOSS Elbow Swivel','elbow',shoulder,PIVOTS['ceiling'])
    wrist=pivot('MOSS Wrist Swivel','wrist',elbow,PIVOTS['elbow'])
    body=pivot('MOSS Body Pitch','body',wrist,PIVOTS['wrist'])
    def add_mesh(name,mesh,parent,origin):
        index=len(meshes);meshes.append({'name':name,**mesh})
        node(name,parent,(-np.asarray(origin)).tolist(),mesh=index)
    counts={}
    for name,indices in parts.items():
        flattened=faces[sorted(indices)].reshape(-1).astype('<u2')
        offset=len(binary);chunk=flattened.tobytes();binary.extend(chunk);binary.extend(b'\0'*(-len(binary)%4))
        view=len(document['bufferViews']);document['bufferViews'].append({'buffer':0,'byteOffset':offset,'byteLength':len(chunk),'target':34963})
        accessor=len(document['accessors']);document['accessors'].append({'bufferView':view,'componentType':5123,'count':len(flattened),'type':'SCALAR'})
        part=copy.deepcopy(primitive);part['indices']=accessor
        parent,origin={'fixed':(root,[0,0,0]),'upper':(shoulder,PIVOTS['ceiling']),
                       'shaft':(elbow,PIVOTS['elbow']),'lower':(wrist,PIVOTS['wrist'])}[name]
        add_mesh('MOSS '+name.title()+' Link',{'primitives':[part]},parent,origin)
        counts[name]=len(indices)
    for index,mesh in enumerate(original_meshes):
        if index!=arm_index:
            name=document['materials'][mesh['primitives'][0]['material']]['name']
            add_mesh(name,copy.deepcopy(mesh),body,PIVOTS['body'])
    assert sum(counts.values())==17548 and all(counts.values())
    document['meshes']=meshes;document['nodes']=nodes
    document['scene']=0;document['scenes']=[{'name':'Suspended MOSS','nodes':[root]}]
    # Keep source credit and modification history with the rigged export.
    source=next((n['extras'] for n in json.loads(data[20:20+size])['nodes'] if 'extras' in n and 'author' in n['extras']),{})
    nodes[root]['extras'].update(source)
    nodes[root]['extras']['rig_modifications']='Static arm split into four rigid component sets; authored ceiling, elbow, wrist and body pitch pivots. No triangles removed.'
    document['buffers'][0]['byteLength']=len(binary)
    payload=json.dumps(document,separators=(',',':'),ensure_ascii=False).encode();payload+=b' '*(-len(payload)%4)
    output=struct.pack('<4sII',b'glTF',2,28+len(payload)+len(binary))+struct.pack('<I4s',len(payload),b'JSON')+payload+struct.pack('<I4s',len(binary),b'BIN\0')+binary
    return output,{'parts':counts,'components':len(components),'triangles':32982,'meshes':len(meshes),'pivots':PIVOTS,
                   'source_sha256':sha256(data).hexdigest(),'sha256':sha256(output).hexdigest(),'bytes':len(output)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();data,receipt=rig(args.source.read_bytes());args.output.write_bytes(data)
    print(json.dumps(receipt,indent=2))


if __name__=='__main__':main()
