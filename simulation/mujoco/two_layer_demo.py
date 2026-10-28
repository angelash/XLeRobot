"""Compare the upstream XLeRobot height with a schematic two-tier cart.

This is a reachability study, not an exact IKEA RASKOG collision model.
It low ers the arm roots by 0.24 m, removes the third (top) visual tier, and
places a floor-level toy in front of the robot.
"""

from pathlib import Path
import json

import mujoco
import numpy as np
from PIL import Image, ImageDraw
from scipy.optimize import differential_evolution


ROOT = Path(__file__).resolve().parents[2]
SIM_DIR = ROOT / "simulation" / "mujoco"
ARTIFACTS = SIM_DIR / "artifacts" / "two_layer"
ARTIFACTS.mkdir(parents=True, exist_ok=True)

ORIGINAL_ROBOT = SIM_DIR / "xlerobot.xml"
ORIGINAL_SCENE = SIM_DIR / "scene.xml"
LOW_ROBOT = SIM_DIR / "xlerobot_two_layer.xml"
LOW_SCENE = SIM_DIR / "scene_two_layer.xml"

ROOT_DROP_M = 0.24
ARM_ROOT_Z = 0.7865 - ROOT_DROP_M
HEAD_ROOT_Z = 0.91 - ROOT_DROP_M
TARGET = np.array([0.50, -0.10, 0.045])
ARM_JOINTS = [
    "Rotation_L",
    "Pitch_L",
    "Elbow_L",
    "Wrist_Pitch_L",
    "Wrist_Roll_L",
    "Jaw_L",
]


def generate_two_tier_xml() -> None:
    robot = ORIGINAL_ROBOT.read_text(encoding="utf-8")
    robot = robot.replace(
        'name="Rotation_Pitch" pos="0.1352 -0.15 0.7865"',
        f'name="Rotation_Pitch" pos="0.1352 -0.15 {ARM_ROOT_Z:.6f}"',
    )
    robot = robot.replace(
        'name="Rotation_Pitch_R" pos="0.1352 0.15 0.7865"',
        f'name="Rotation_Pitch_R" pos="0.1352 0.15 {ARM_ROOT_Z:.6f}"',
    )
    robot = robot.replace(
        'name="head_pan_link" pos="-0.125 0 0.91"',
        f'name="head_pan_link" pos="-0.125 0 {HEAD_ROOT_Z:.6f}"',
    )
    # The upstream blue torso box is only a visual stand-in. Disable its
    # collision so joint-reach sampling does not collide with an unmodeled cart.
    robot = robot.replace(
        '<geom type="box" size="0.2 0.2 0.38" pos="-0.03 0 0.44" rgba="0.2 0.2 0.6 0.2"/>',
        '<geom type="box" size="0.2 0.2 0.38" pos="-0.03 0 0.44" rgba="0.16 0.2 0.3 0.08" contype="0" conaffinity="0"/>',
    )
    LOW_ROBOT.write_text(robot, encoding="utf-8")

    scene = ORIGINAL_SCENE.read_text(encoding="utf-8")
    scene = scene.replace(
        '<include file="./xlerobot.xml"/>',
        '<include file="./xlerobot_two_layer.xml"/>',
    )
    schematic = '''
    <body name="two_tier_tray_schematic">
      <geom name="bottom_tray" type="box" size="0.225 0.18 0.012"
            pos="0 0 0.14" rgba="0.76 0.72 0.62 0.85"/>
      <geom name="middle_tray" type="box" size="0.225 0.18 0.012"
            pos="0 0 0.39" rgba="0.82 0.78 0.68 0.85"/>
      <geom name="left_upright" type="box" size="0.012 0.012 0.24"
            pos="-0.21 -0.17 0.36" rgba="0.22 0.22 0.22 1"/>
      <geom name="right_upright" type="box" size="0.012 0.012 0.24"
            pos="-0.21 0.17 0.36" rgba="0.22 0.22 0.22 1"/>
    </body>
    <geom name="floor_toy" type="sphere" size="0.045" pos="0.50 -0.10 0.045"
          rgba="0.9 0.15 0.08 1" contype="0" conaffinity="0"/>
    <site name="floor_toy_center" pos="0.50 -0.10 0.045" size="0.008"
          rgba="1 1 1 1"/>
'''
    scene = scene.replace(
        '<geom name="floor" size="0 0 0.05" type="plane" material="groundplane"/>',
        '<geom name="floor" size="0 0 0.05" type="plane" material="groundplane"/>' + schematic,
    )
    LOW_SCENE.write_text(scene, encoding="utf-8")


def load_model(path: Path):
    model = mujoco.MjModel.from_xml_path(str(path.resolve()))
    data = mujoco.MjData(model)
    return model, data


def addr_for_joint(model, joint_name: str) -> int:
    jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, joint_name)
    return int(model.jnt_qposadr[jid])


def solve_floor_reach(model, data):
    qidx = np.array([addr_for_joint(model, name) for name in ARM_JOINTS])
    bounds = []
    for name in ARM_JOINTS:
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        lo, hi = model.jnt_range[jid]
        bounds.append((float(lo), float(hi)))
    fixed_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "Fixed_Jaw")
    moving_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "Moving_Jaw")
    calls = 0

    def evaluate(q):
        nonlocal calls
        calls += 1
        data.qpos[qidx] = q
        data.qvel[:] = 0.0
        mujoco.mj_forward(model, data)
        point = 0.5 * (data.xpos[fixed_id] + data.xpos[moving_id])
        return float(np.linalg.norm(point - TARGET))

    result = differential_evolution(
        evaluate,
        bounds,
        seed=7,
        maxiter=180,
        popsize=16,
        tol=1e-7,
        polish=True,
        workers=1,
        updating="immediate",
    )
    data.qpos[qidx] = result.x
    mujoco.mj_forward(model, data)
    fixed = data.xpos[fixed_id].copy()
    moving = data.xpos[moving_id].copy()
    point = 0.5 * (fixed + moving)
    return {
        "distance_to_toy_m": float(result.fun),
        "endpoint_m": point.tolist(),
        "fixed_jaw_origin_m": fixed.tolist(),
        "moving_jaw_origin_m": moving.tolist(),
        "q": np.asarray(result.x).tolist(),
        "optimizer_calls": calls,
    }


def render(model, data, path: Path, label: str):
    renderer = mujoco.Renderer(model, height=480, width=640)
    camera = mujoco.MjvCamera()
    mujoco.mjv_defaultCamera(camera)
    camera.lookat[:] = [0.18, 0.0, 0.36]
    camera.distance = 2.45
    camera.azimuth = 145.0
    camera.elevation = -15.0
    renderer.update_scene(data, camera=camera)
    pixels = renderer.render()
    renderer.close()
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle((0, 0, image.width, 82), fill=(5, 15, 28, 210))
    draw.text((24, 18), label, fill=(255, 255, 255, 255))
    draw.text((24, 49), "Red: floor toy | Tan: schematic two tiers", fill=(210, 220, 235, 255))
    image.save(path)


def main():
    generate_two_tier_xml()

    normal_model, normal_data = load_model(ORIGINAL_SCENE)
    low_model, low_data = load_model(LOW_SCENE)

    normal_result = solve_floor_reach(normal_model, normal_data)
    low_result = solve_floor_reach(low_model, low_data)

    render(normal_model, normal_data, ARTIFACTS / "normal_reach.png", "Original top-mounted arms")
    render(low_model, low_data, ARTIFACTS / "two_layer_reach.png", "Two-tier setup: arms lowered 24 cm")

    left = Image.open(ARTIFACTS / "normal_reach.png").convert("RGB")
    right = Image.open(ARTIFACTS / "two_layer_reach.png").convert("RGB")
    combined = Image.new("RGB", (left.width + right.width, max(left.height, right.height)), "white")
    combined.paste(left, (0, 0))
    combined.paste(right, (left.width, 0))
    combined.save(ARTIFACTS / "comparison.png")

    report = {
        "root_drop_m": ROOT_DROP_M,
        "normal_arm_root_z_m": 0.7865,
        "two_tier_arm_root_z_m": ARM_ROOT_Z,
        "target_m": TARGET.tolist(),
        "normal": normal_result,
        "two_tier": low_result,
        "normal_model": {
            "nq": normal_model.nq,
            "nv": normal_model.nv,
            "nu": normal_model.nu,
        },
        "two_tier_model": {
            "nq": low_model.nq,
            "nv": low_model.nv,
            "nu": low_model.nu,
        },
        "scope": "kinematic reachability only; cart collision, stability, camera view, and cable routing remain unverified",
    }
    (ARTIFACTS / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"artifact_dir={ARTIFACTS.resolve()}")


if __name__ == "__main__":
    main()
