import json
import numpy as np
import pytest
from deployment.phone_vision import depth_motion
from deployment.phone_vision.depth_accuracy import camera_points
from deployment.phone_vision.depth_motion import clearance,floor_pose,wall,wall_change

META={'width':240,'height':320,'intrinsics':{'fx':220.,'fy':220.,'tx':120.,'ty':160.}}


def scene(*,height=.9,pitch_deg=25.,yaw_left_deg=0.,at=(0.,0.),wall_z=None,boxes=(),floor_ends=None,drop=.2,meta=META):
    """Depth image of a floor, a wall across the way and boxes, seen from a camera above the floor.

    World axes are right, up and forward; `at` is the camera's (right, forward) position; a box is
    (right_min, right_max, bottom, top, forward_min, forward_max). Past `floor_ends` the floor is `drop` lower.
    """
    pitch,yaw=np.radians([pitch_deg,yaw_left_deg])
    def turned(v):return np.array([v[0]*np.cos(yaw)-v[2]*np.sin(yaw),v[1],v[0]*np.sin(yaw)+v[2]*np.cos(yaw)])
    right,down,forward=(turned(v) for v in ((1,0,0),(0,-np.cos(pitch),-np.sin(pitch)),(0,-np.sin(pitch),np.cos(pitch))))
    k=meta['intrinsics']
    y,x=np.mgrid[0:meta['height'],0:meta['width']]
    ray=((x-k['tx'])/k['fx'])[...,None]*right+((y-k['ty'])/k['fy'])[...,None]*down+forward
    origin=np.array([at[0],height,at[1]])
    with np.errstate(divide='ignore',invalid='ignore'):
        def plane(axis,value):
            t=(value-origin[axis])/ray[...,axis]
            return np.where(t>0,t,np.inf)
        depth=plane(1,0.)
        if floor_ends is not None:
            lower=plane(1,-drop)
            depth=np.where(origin[2]+depth*ray[...,2]>floor_ends,lower,depth)
        if wall_z is not None:depth=np.minimum(depth,plane(2,wall_z))
        for x0,x1,bottom,top,z0,z1 in boxes:
            near=np.full(depth.shape,-np.inf);far=np.full(depth.shape,np.inf)
            for axis,(low,high) in ((0,(x0,x1)),(1,(bottom,top)),(2,(z0,z1))):
                a,b=(low-origin[axis])/ray[...,axis],(high-origin[axis])/ray[...,axis]
                near=np.maximum(near,np.minimum(a,b));far=np.minimum(far,np.maximum(a,b))
            depth=np.where((near<=far)&(near>0),np.minimum(depth,near),depth)
    return np.where(np.isfinite(depth),depth,0).astype('<f4').tobytes()


def test_floor_gives_the_camera_height_pitch_and_roll():
    points,valid=camera_points(scene(height=.9,pitch_deg=25,wall_z=2.5),META)
    pose=floor_pose(points,valid)
    assert pose['height_m']==pytest.approx(.9,abs=.003)
    assert pose['pitch_down_deg']==pytest.approx(25,abs=.2) and abs(pose['roll_deg'])<.2
    assert pose['floor_rms_m']<.002 and pose['floor_fraction']>.3


def test_a_view_without_floor_has_no_pose_and_no_clearance():
    raw=scene(height=8,pitch_deg=0,wall_z=1.)
    points,valid=camera_points(raw,META)
    assert floor_pose(points,valid) is None
    result=clearance(raw,META,half_width_m=.3)
    assert not result['floor_found'] and result['free_to_m'] is None
    assert not result['motion_enabled'] and not result['metric_clearance_verified']


def test_open_floor_is_free_up_to_the_wall_and_never_before_the_first_seen_floor():
    result=clearance(scene(wall_z=2.2),META,half_width_m=.3)
    assert result['nearest_obstacle_m']==pytest.approx(2.2,abs=.11) and result['obstacle_kind']=='raised'
    assert result['free_to_m']==pytest.approx(2.2,abs=.11)
    # The lowest ray of this camera meets the floor about half a metre ahead; nearer floor is not in view.
    assert .4<=result['blind_to_m']<=.6


def test_a_box_in_the_corridor_limits_the_free_distance_and_one_beside_it_does_not():
    inside=clearance(scene(wall_z=2.5,boxes=[(-.1,.1,0.,.3,1.2,1.4)]),META,half_width_m=.3)
    assert inside['free_to_m']==pytest.approx(1.2,abs=.11) and inside['obstacle_kind']=='raised'
    beside=clearance(scene(wall_z=2.5,boxes=[(.5,.7,0.,.3,1.2,1.4)]),META,half_width_m=.3)
    assert beside['free_to_m']==pytest.approx(2.5,abs=.11)
    # The same box is in the way when the camera sits 0.4 m left of the body centreline.
    offset=clearance(scene(wall_z=2.5,boxes=[(.5,.7,0.,.3,1.2,1.4)]),META,half_width_m=.3,camera_left_m=.4)
    assert offset['nearest_obstacle_m']==pytest.approx(1.2,abs=.11)


def test_an_overhang_counts_only_below_the_body_height():
    beam=scene(pitch_deg=10,wall_z=2.8,boxes=[(-.5,.5,1.4,1.6,1.5,1.7)])
    assert clearance(beam,META,half_width_m=.3,max_height_m=1.3)['free_to_m']==pytest.approx(2.8,abs=.11)
    assert clearance(beam,META,half_width_m=.3,max_height_m=1.7)['nearest_obstacle_m']==pytest.approx(1.5,abs=.11)
    table=clearance(scene(pitch_deg=10,wall_z=2.8,boxes=[(-.5,.5,.6,.7,1.5,1.7)]),META,half_width_m=.3)
    assert table['free_to_m']==pytest.approx(1.5,abs=.11) and table['obstacle_kind']=='raised'


def test_an_obstacle_nearer_than_the_first_seen_floor_leaves_nothing_free():
    # A post 0.3 m ahead stands where this camera cannot see the floor yet.
    result=clearance(scene(wall_z=2.5,boxes=[(-.05,.05,0.,1.,.3,.4)]),META,half_width_m=.3)
    assert result['floor_found'] and result['nearest_obstacle_m']==pytest.approx(.3,abs=.11)
    assert result['free_to_m'] is None


def test_a_step_down_ends_the_free_distance_where_the_floor_stops():
    result=clearance(scene(floor_ends=1.5),META,half_width_m=.3)
    assert result['floor_seen_to_m']==pytest.approx(1.5,abs=.11)
    assert result['obstacle_kind']=='drop' and result['nearest_obstacle_m']>1.7
    assert result['free_to_m']==result['floor_seen_to_m']


def test_floor_hidden_on_one_side_of_the_corridor_is_not_reported_as_free():
    depth=np.frombuffer(scene(wall_z=2.5),dtype='<f4').reshape(320,240).copy()
    full=clearance(depth.tobytes(),META,half_width_m=.3)
    depth[:,:100]=0
    # The left of the corridor comes into view only farther out, so the known floor starts later.
    part=clearance(depth.tobytes(),META,half_width_m=.3)
    assert part['blind_to_m']>full['blind_to_m']+.2 and part['free_to_m']==full['free_to_m']
    depth[:,:140]=0
    # The left of the corridor is never in view: the wall is still an obstacle, but nothing is known free.
    none=clearance(depth.tobytes(),META,half_width_m=.3)
    assert none['floor_found'] and none['floor_seen_to_m'] is None and none['free_to_m'] is None
    assert none['nearest_obstacle_m']==pytest.approx(2.5,abs=.11)


@pytest.mark.parametrize('view',[{'height':.9,'pitch_deg':20.},{'height':8.,'pitch_deg':0.}])
def test_a_turn_is_read_from_the_wall_normal_with_or_without_floor_in_view(view):
    before=wall(scene(wall_z=1.5,**view),META)
    after=wall(scene(wall_z=1.5,yaw_left_deg=12,**view),META)
    change=wall_change(before,after)
    assert change['yaw_left_deg']==pytest.approx(12,abs=.3)
    assert abs(change['closer_m'])<.004
    assert change['up_assumed']==(view['pitch_deg']==0)
    assert wall_change(after,before)['yaw_left_deg']==pytest.approx(-12,abs=.3)
    assert not change['direction_verified_on_vehicle'] and not change['motion_enabled']
    assert change['ledger_truth']['yaw']=={'kind':'depth_plane','unit':'deg','value':change['yaw_left_deg']}


def test_a_straight_drive_toward_the_wall_is_read_as_travel_even_when_not_square_to_it():
    square=wall_change(wall(scene(wall_z=1.5,pitch_deg=20),META),wall(scene(wall_z=1.5,pitch_deg=20,at=(0,.1)),META))
    assert square['closer_m']==pytest.approx(.1,abs=.003) and square['travel_m']==pytest.approx(.1,abs=.003)
    step=.1*np.array([-np.sin(np.radians(20)),np.cos(np.radians(20))])
    skew=wall_change(wall(scene(wall_z=1.5,pitch_deg=20,yaw_left_deg=20),META),
                     wall(scene(wall_z=1.5,pitch_deg=20,yaw_left_deg=20,at=tuple(step)),META))
    assert skew['closer_m']==pytest.approx(.1*np.cos(np.radians(20)),abs=.003)
    assert skew['travel_m']==pytest.approx(.1,abs=.004) and skew['heading_to_wall_deg']==pytest.approx(20,abs=.5)
    assert abs(skew['yaw_left_deg'])<.3


def test_a_view_that_is_not_a_wall_is_refused():
    floor_only=wall(scene(pitch_deg=60),META)
    with pytest.raises(ValueError):wall_change(floor_only,floor_only)


def save(folder,raws):
    folder.mkdir(parents=True)
    listing=[]
    for i,raw in enumerate(raws):
        (folder/f'{i:03d}-raw.f32').write_bytes(raw)
        listing.append({'meta':META,'files':{'raw':{'name':f'{i:03d}-raw.f32'}}})
    (folder/'result.json').write_text(json.dumps({'frames':listing}))


def test_command_line_reads_saved_probe_frames(tmp_path,capsys):
    save(tmp_path/'a',[scene(wall_z=1.5,pitch_deg=20)]*2)
    save(tmp_path/'b',[scene(wall_z=1.5,pitch_deg=20,yaw_left_deg=-8)]*2)
    assert depth_motion.main(['change','--before',str(tmp_path/'a'),'--after',str(tmp_path/'b')])==0
    assert json.loads(capsys.readouterr().out)['yaw_left_deg']==pytest.approx(-8,abs=.3)
    save(tmp_path/'c',[scene(wall_z=2.5),scene(wall_z=2.5,boxes=[(-.1,.1,0.,.3,1.2,1.4)])])
    assert depth_motion.main(['clearance','--frames',str(tmp_path/'c'),'--half-width-cm','30'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['free_to_m']==pytest.approx(1.2,abs=.11) and result['frames']==2
    save(tmp_path/'d',[scene(pitch_deg=60)])
    assert depth_motion.main(['change','--before',str(tmp_path/'d'),'--after',str(tmp_path/'a')])==2
    assert json.loads(capsys.readouterr().out)['ok'] is False
