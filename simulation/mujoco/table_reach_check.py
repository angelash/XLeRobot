"""Check whether each candidate can also reach a counter-height toy."""

import json

import candidate_schemes_demo as demo


TABLE_TARGET = [0.0, 0.40, 0.72]


def build_table_scene():
    demo.build_scene()
    scene = demo.SCENE
    text = scene.read_text(encoding="utf-8")
    text = text.replace(
        'pos="0 0.40 0.045"\n          rgba="0.95 0.16 0.07 1"',
        'pos="0 0.40 0.72"\n          rgba="0.95 0.16 0.07 1"',
    )
    text = text.replace(
        '<site name="floor_toy_center" pos="0 0.40 0.045" size="0.006" rgba="1 1 1 1"/>',
        '<site name="table_toy_center" pos="0 0.40 0.72" size="0.006" rgba="1 1 1 1"/>'
        '<geom name="table_top" type="box" size="0.35 0.20 0.03" '
        'pos="0 0.45 0.65" rgba="0.40 0.28 0.16 1"/>',
    )
    table_scene = demo.SIM / "scheme_table_scene.xml"
    table_scene.write_text(text, encoding="utf-8")
    return table_scene


def main():
    table_scene = build_table_scene()
    original_target = demo.TARGET.copy()
    demo.TARGET[:] = TABLE_TARGET
    rows = []
    try:
        for scheme in demo.SCHEMES:
            candidate = dict(scheme)
            if candidate["key"] == "active_lift":
                candidate["root_z"] = 0.7865
                candidate["tilt"] = 0.0
            model, data, result = demo.optimize_scheme(
                table_scene, candidate, maxiter=100
            )
            result["feasible"] = (
                result["tip_to_toy_m"] <= 0.06
                and result["collision_penetration_m"] < 0.003
            )
            rows.append(result)
            print(
                f"{candidate['key']}: distance={result['tip_to_toy_m']:.4f} "
                f"collision={result['collision_penetration_m']:.4f} "
                f"feasible={result['feasible']}"
            )
    finally:
        demo.TARGET[:] = original_target

    rows.sort(key=lambda row: (-int(row["feasible"]), row["tip_to_toy_m"]))
    report = {
        "target_m": TABLE_TARGET,
        "success_threshold_m": 0.06,
        "ranked": rows,
        "scope": "Kinematic tabletop check with a schematic tabletop obstacle.",
    }
    path = demo.ART / "table_reach_with_collision.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
