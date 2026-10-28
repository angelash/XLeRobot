from pathlib import Path
import json
import numpy as np
import mujoco
from scipy.optimize import differential_evolution

from two_layer_demo import generate_two_tier_xml, load_model, addr_for_joint, ARM_JOINTS, LOW_SCENE, ARTIFACTS, ROOT_DROP_M

generate_two_tier_xml()
model, data = load_model(LOW_SCENE)
qidx = np.array([addr_for_joint(model, name) for name in ARM_JOINTS])
bounds = []
for name in ARM_JOINTS:
    jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
    lo, hi = model.jnt_range[jid]
    bounds.append((float(lo), float(hi)))
fixed_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "Fixed_Jaw")
moving_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "Moving_Jaw")

def pose(q):
    data.qpos[qidx] = q
    data.qvel[:] = 0
    mujoco.mj_forward(model, data)
    return 0.5 * (data.xpos[fixed_id] + data.xpos[moving_id])

result = differential_evolution(
    lambda q: float(pose(q)[2]),
    bounds, seed=11, maxiter=250, popsize=20, tol=1e-8,
    polish=True, workers=1, updating="immediate"
)
point = pose(result.x)

targets=[]
for x in np.round(np.arange(0.25,0.56,0.05),2):
  for y in np.round(np.arange(-0.20,0.21,0.10),2):
    target=np.array([x,y,0.045])
    targets.append((target, pose(result.x)))

from PIL import Image, ImageDraw
renderer = mujoco.Renderer(model, height=480, width=640)
camera = mujoco.MjvCamera()
mujoco.mjv_defaultCamera(camera)
camera.lookat[:] = [0.16, 0.0, 0.30]
camera.distance = 2.3
camera.azimuth = 145.0
camera.elevation = -14.0
renderer.update_scene(data, camera=camera)
pixels = renderer.render()
renderer.close()
image = Image.fromarray(pixels)
draw = ImageDraw.Draw(image, "RGBA")
draw.rectangle((0, 0, image.width, 82), fill=(5, 15, 28, 210))
draw.text((24, 18), "Two-tier low-arm minimum-height pose", fill="white")
draw.text((24, 49), f"Gripper midpoint z = {point[2]:.3f} m", fill=(220, 220, 235, 255))
image.save(ARTIFACTS / "min_z_pose.png")
report={
 "root_drop_m": ROOT_DROP_M,
 "min_endpoint_z_m": float(point[2]),
 "min_endpoint_m": point.tolist(),
 "q": result.x.tolist(),
 "distance_from_arm_root_m": float(np.linalg.norm(point-np.array([0.1352,-0.15,0.5465]))),
 "note":"Min-z endpoint optimization. Endpoint is midpoint between Fixed_Jaw and Moving_Jaw body origins; it is an approximation of the gripper center, not the mesh tip."
}
(ARTIFACTS/'min_z.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps(report,indent=2,ensure_ascii=False))
