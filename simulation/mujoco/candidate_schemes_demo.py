"""Compare mechanical approaches for reaching a floor toy with XLeRobot.

This model uses the upstream XLeRobot MJCF and schematic IKEA RASKOG shelves.
Arm motion is optimized with the actual jaw collision hulls as the endpoint.
Results are kinematic/collision screening, not a stability or fatigue test.
"""

from pathlib import Path
import json

import mujoco
import numpy as np
from PIL import Image, ImageDraw
from scipy.optimize import differential_evolution
from scipy.spatial.transform import Rotation


ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "simulation" / "mujoco"
ART = SIM / "artifacts" / "candidate_schemes"
ART.mkdir(parents=True, exist_ok=True)

BASE_SCENE = SIM / "scene.xml"
SCENE = SIM / "scheme_scene.xml"
TARGET = np.array([0.0, 0.40, 0.045])
ARM_JOINT_NAMES = [
    "Rotation_L", "Pitch_L", "Elbow_L",
    "Wrist_Pitch_L", "Wrist_Roll_L", "Jaw_L",
]
ARM_BODY_NAMES = [
    "Rotation_Pitch", "Upper_Arm", "Lower_Arm",
    "Wrist_Pitch_Roll", "Fixed_Jaw", "Moving_Jaw",
]
GRIPPER_BODY_NAMES = [
    "Fixed_Jaw",
    "Moving_Jaw",
]

SCHEMES = [
    dict(key="oem_top", label="A: OEM three-tier top", root_z=0.7865, forward=0.0, tilt=0.0, tool=0.0, tiers=3, complexity=0),
    dict(key="two_tier", label="B: Two-tier middle", root_z=0.5065, forward=0.0, tilt=0.0, tool=0.0, tiers=2, complexity=1),
    dict(key="two_tier_down", label="C: Middle + 90 deg down", root_z=0.5065, forward=0.0, tilt=90.0, tool=0.0, tiers=2, complexity=3),
    dict(key="middle_forward_down", label="D: Middle + forward + down", root_z=0.5065, forward=0.10, tilt=90.0, tool=0.0, tiers=2, complexity=4),
    dict(key="low_front_down", label="E: Low front bracket", root_z=0.30, forward=0.12, tilt=90.0, tool=0.0, tiers=2, complexity=4),
    dict(key="middle_down_tool", label="F: Middle/down + rigid 0.15 m tool", root_z=0.5065, forward=0.0, tilt=90.0, tool=0.15, tiers=2, complexity=3),
    dict(key="middle_forward_tool05", label="F2: Middle/forward/down + 0.05 m tool", root_z=0.5065, forward=0.10, tilt=90.0, tool=0.05, tiers=2, complexity=4),
    dict(key="middle_forward_tool10", label="F3: Middle/forward/down + 0.10 m tool", root_z=0.5065, forward=0.10, tilt=90.0, tool=0.10, tiers=2, complexity=4),
    dict(key="active_lift", label="G: Active floor-to-table lift", root_z=0.30, forward=0.12, tilt=90.0, tool=0.0, tiers=3, complexity=8),
]


def build_scene():
    text = BASE_SCENE.read_text(encoding="utf-8")
    # Remove the upstream schematic blue collision box so shelf contacts are explicit.
    text = text.replace(
        '<geom type="box" size="0.2 0.2 0.38" pos="-0.03 0 0.44" rgba="0.2 0.2 0.6 0.2"/>',
        '<geom type="box" size="0.2 0.2 0.38" pos="-0.03 0 0.44" rgba="0 0 0 0" contype="0" conaffinity="0"/>',
    )
    shelf = '''
    <body name="raskog_schematic">
      <geom name="bottom_tray" type="box" size="0.164 0.207 0.045"
            pos="0 0 0.18" rgba="0.72 0.68 0.58 0.82"/>
      <geom name="middle_tray" type="box" size="0.164 0.207 0.045"
            pos="0 0 0.45" rgba="0.78 0.74 0.64 0.82"/>
      <geom name="top_tray" type="box" size="0.164 0.207 0.045"
            pos="0 0 0.73" rgba="0.84 0.80 0.70 0.82"/>
      <geom name="front_left_post" type="box" size="0.012 0.012 0.34"
            pos="-0.164 0.207 0.455" rgba="0.18 0.18 0.18 1"/>
      <geom name="front_right_post" type="box" size="0.012 0.012 0.34"
            pos="0.164 0.207 0.455" rgba="0.18 0.18 0.18 1"/>
    </body>
    <geom name="floor_toy" type="sphere" size="0.045" pos="0 0.40 0.045"
          rgba="0.95 0.16 0.07 1" contype="0" conaffinity="0"/>
    <site name="floor_toy_center" pos="0 0.40 0.045" size="0.006" rgba="1 1 1 1"/>
'''
    text = text.replace(
        '<geom name="floor" size="0 0 0.05" type="plane" material="groundplane"/>',
        '<geom name="floor" size="0 0 0.05" type="plane" material="groundplane"/>' + shelf,
    )
    SCENE.write_text(text, encoding="utf-8")


def ids_for_joints(model):
    return np.array([
        int(model.jnt_qposadr[
            mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        ])
        for name in ARM_JOINT_NAMES
    ])


def apply_scheme(model, scheme):
    # Exact chassis heights inferred from RASKOG URDF tray centers.
    for name in ("Rotation_Pitch", "Rotation_Pitch_R"):
        bid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)
        base = model.body_pos[bid].copy()
        # In this parent frame, local X maps to world forward Y.
        base[0] = 0.1352 + scheme["forward"]
        base[2] = scheme["root_z"]
        model.body_pos[bid] = base
        if scheme["tilt"]:
            rv = Rotation.from_euler("x", scheme["tilt"], degrees=True).as_quat()
            rq = np.array([rv[3], rv[0], rv[1], rv[2]])
            q1 = np.zeros(4)
            mujoco.mju_mulQuat(q1, model.body_quat[bid].copy(), rq)
            model.body_quat[bid] = q1
    head = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "head_pan_link")
    head_pos = model.body_pos[head].copy()
    head_pos[2] = 0.91 - (0.7865 - scheme["root_z"])
    model.body_pos[head] = head_pos

    top = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "top_tray")
    if scheme["tiers"] == 2:
        model.geom_contype[top] = 0
        model.geom_conaffinity[top] = 0
        model.geom_rgba[top, 3] = 0
    else:
        model.geom_contype[top] = 1
        model.geom_conaffinity[top] = 1
        model.geom_rgba[top, 3] = 0.82


def gripper_points(model, data):
    pts = []
    body_ids = {
        mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)
        for name in GRIPPER_BODY_NAMES
    }
    for gid in range(model.ngeom):
        if int(model.geom_bodyid[gid]) not in body_ids:
            continue
        mid = int(model.geom_dataid[gid])
        if mid < 0 or model.geom_type[gid] != mujoco.mjtGeom.mjGEOM_MESH:
            continue
        start = int(model.mesh_vertadr[mid])
        count = int(model.mesh_vertnum[mid])
        verts = model.mesh_vert[start:start + count]
        pts.append(verts @ data.geom_xmat[gid].reshape(3, 3).T + data.geom_xpos[gid])
    if not pts:
        raise RuntimeError("No gripper mesh vertices found")
    return np.concatenate(pts, axis=0)


def collision_penetration(model, data, arm_body_ids):
    total = 0.0
    for contact in data.contact[:data.ncon]:
        b1, b2 = int(model.geom_bodyid[contact.geom1]), int(model.geom_bodyid[contact.geom2])
        g1 = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, contact.geom1) or ""
        g2 = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, contact.geom2) or ""
        is_arm = b1 in arm_body_ids or b2 in arm_body_ids
        obstacle = any(
            tag in g1 or tag in g2
            for tag in ("tray", "post", "floor", "table")
        )
        if is_arm and obstacle and contact.dist < 0:
            total += float(-contact.dist)
    return total


def optimize_scheme(scene_path, scheme, maxiter=210):
    model = mujoco.MjModel.from_xml_path(str(scene_path.resolve()))
    data = mujoco.MjData(model)
    apply_scheme(model, scheme)
    mujoco.mj_forward(model, data)

    qidx = ids_for_joints(model)
    bounds = []
    for name in ARM_JOINT_NAMES:
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        bounds.append(tuple(float(x) for x in model.jnt_range[jid]))
    arm_body_ids = {
        mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)
        for name in ARM_BODY_NAMES
    }

    def evaluate(q):
        data.qpos[qidx] = q
        data.qvel[:] = 0
        mujoco.mj_forward(model, data)
        pts = gripper_points(model, data)
        if scheme["tool"]:
            pts = pts + np.array([0.0, 0.0, -scheme["tool"]])
        distance = float(np.min(np.linalg.norm(pts - TARGET, axis=1)))
        penetration = collision_penetration(model, data, arm_body_ids)
        return distance + 12.0 * penetration * penetration + 0.25 * penetration

    result = differential_evolution(
        evaluate,
        bounds,
        seed=19,
        maxiter=maxiter,
        popsize=14,
        tol=1e-6,
        polish=True,
        workers=1,
        updating="immediate",
    )
    data.qpos[qidx] = result.x
    data.qvel[:] = 0
    mujoco.mj_forward(model, data)
    pts = gripper_points(model, data)
    actual_near = float(np.min(np.linalg.norm(pts - TARGET, axis=1)))
    tool_pts = pts + np.array([0.0, 0.0, -scheme["tool"]])
    effective_near = float(np.min(np.linalg.norm(tool_pts - TARGET, axis=1)))
    penetration = collision_penetration(model, data, arm_body_ids)
    closest_i = int(np.argmin(np.linalg.norm(tool_pts - TARGET, axis=1)))
    closest = tool_pts[closest_i].copy()
    return model, data, {
        "key": scheme["key"],
        "label": scheme["label"],
        "root_z_m": scheme["root_z"],
        "forward_shift_m": scheme["forward"],
        "mount_tilt_deg": scheme["tilt"],
        "tool_extension_m": scheme["tool"],
        "tip_to_toy_m": effective_near,
        "actual_gripper_to_toy_m": actual_near,
        "gripper_min_z_m": float(np.min(tool_pts[:, 2])),
        "closest_point_m": closest.tolist(),
        "toy_center_m": TARGET.tolist(),
        "collision_penetration_m": penetration,
        "q": np.asarray(result.x).tolist(),
        "optimizer_calls": int(result.nfev),
    }


def render(model, data, scheme, result, path):
    renderer = mujoco.Renderer(model, height=480, width=640)
    camera = mujoco.MjvCamera()
    mujoco.mjv_defaultCamera(camera)
    camera.lookat[:] = [0.0, 0.25, 0.32]
    camera.distance = 2.15
    camera.azimuth = 135.0
    camera.elevation = -18.0
    renderer.update_scene(data, camera=camera)
    pixels = renderer.render()
    renderer.close()
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle((0, 0, image.width, 88), fill=(4, 14, 28, 218))
    draw.text((20, 15), scheme["label"], fill="white")
    draw.text((20, 40), f"Tip-toy: {result['tip_to_toy_m']:.3f} m  Collision: {result['collision_penetration_m']:.4f} m", fill=(215, 225, 238, 255))
    draw.text((20, 62), f"Gripper min Z: {result['gripper_min_z_m']:.3f} m", fill=(215, 225, 238, 255))
    image.save(path)


def main():
    build_scene()
    all_results = []
    images = []
    for i, scheme in enumerate(SCHEMES):
        model, data, result = optimize_scheme(SCENE, scheme)
        result["feasible"] = result["tip_to_toy_m"] <= 0.06 and result["collision_penetration_m"] < 0.003
        result["score"] = max(0.0, 100.0 * (1.0 - result["tip_to_toy_m"] / 0.8) - 6.0 * scheme["complexity"] - 120.0 * result["collision_penetration_m"])
        all_results.append(result)
        image_path = ART / f"{i + 1:02d}_{scheme['key']}.png"
        render(model, data, scheme, result, image_path)
        images.append(image_path)
        print(f"{scheme['key']}: distance={result['tip_to_toy_m']:.4f} penetration={result['collision_penetration_m']:.4f} feasible={result['feasible']}")

    all_results.sort(key=lambda r: (-int(r["feasible"]), -r["score"]))
    report = {
        "target_m": TARGET.tolist(),
        "root_drop_middle_m": 0.28,
        "success_threshold_m": 0.06,
        "ranked": all_results,
        "scope": "Actual XLeRobot arm hulls, schematic RASKOG shelves. Long tool and lift are conceptual attachments.",
    }
    (ART / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    thumbs = [Image.open(p).convert("RGB") for p in images]
    thumb_w, thumb_h = 640, 480
    sheet = Image.new("RGB", (thumb_w * 2, thumb_h * 4), "white")
    for idx, img in enumerate(thumbs):
        sheet.paste(img, ((idx % 2) * thumb_w, (idx // 2) * thumb_h))
    sheet.save(ART / "comparison.png")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
