"""Generate first-article CAD for the XLeRobot 0.4 low-front concept.

The arm-interface coordinates are measurements from the upstream
hardware/step/XLeRobot_040/XLeRobot040_armbase.step file. The surrounding
frame is a concept assembly only: it deliberately uses aluminium extrusion
for the load path and is not a claim that a 3D-printed cantilever is safe.

Run with the isolated environment:
    .venv-cad\\Scripts\\python.exe fabrication\\cad\\xlerobot_low_front_prototype.py
"""

from __future__ import annotations

import json
from pathlib import Path

import cadquery as cq
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import trimesh
from mpl_toolkits.mplot3d.art3d import Poly3DCollection


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "fabrication" / "export"
OUT.mkdir(parents=True, exist_ok=True)

# Bounds from the central dual-arm mounting interface in XLeRobot040_armbase.step.
PLATE_W = 150.5
PLATE_D = 140.0
M3_CLEARANCE = 3.4
M5_CLEARANCE = 5.5

# Coordinates are expressed relative to the measured plate centre
# (original centre: x=7.43 mm, y=15.83 mm).
INTERFACE_M3_HOLES = (
    (-31.74, -66.09),
    (14.25, -66.09),
    (60.25, -66.09),
    (-31.74, 63.91),
    (14.25, 63.91),
    (60.25, 63.91),
)

# Four low-profile fasteners join this printed arm interface to a 6 mm
# aluminium backing plate. They remain clear of the six measured M3 holes.
FRAME_M5_HOLES = ((-55.0, -50.0), (55.0, -50.0), (-55.0, 55.0), (55.0, 55.0))


def through_cylinder(x: float, y: float, diameter: float, height: float) -> cq.Workplane:
    return (
        cq.Workplane("XY")
        .center(x, y)
        .circle(diameter / 2.0)
        .extrude(height)
    )


def arm_interface_plate(thickness: float = 8.0) -> cq.Workplane:
    plate = cq.Workplane("XY").rect(PLATE_W, PLATE_D).extrude(thickness)
    plate = plate.edges("|Z").fillet(6.0)
    for x, y in INTERFACE_M3_HOLES:
        plate = plate.cut(through_cylinder(x, y, M3_CLEARANCE, thickness + 2.0).translate((0, 0, -1)))
    for x, y in FRAME_M5_HOLES:
        plate = plate.cut(through_cylinder(x, y, M5_CLEARANCE, thickness + 2.0).translate((0, 0, -1)))
    return plate


def box(w: float, d: float, h: float, x: float, y: float, z: float) -> cq.Workplane:
    return cq.Workplane("XY").box(w, d, h).translate((x, y, z))


def build_low_front_concept() -> cq.Assembly:
    """Create a non-printable assembly showing the intended load path.

    Overall geometry: 120 mm forward reach and 207 mm drop. Two 2040
    uprights and two forward rails carry a metal backing plate. The printed
    arm interface is joined to that metal plate.
    """
    asm = cq.Assembly(name="xlerobot_040_low_front_concept")

    asm.add(box(150.0, 110.0, 6.0, 0, 0, 207.0), name="cart_backing_plate", color=cq.Color(0.55, 0.58, 0.62))
    asm.add(box(160.0, 30.0, 20.0, 0, 0, 194.0), name="rear_2040_crossbar", color=cq.Color(0.12, 0.45, 0.72))

    for side, x in (("left", -55.0), ("right", 55.0)):
        asm.add(box(20.0, 40.0, 207.0, x, 120.0, 103.5), name=f"{side}_vertical_2040", color=cq.Color(0.12, 0.45, 0.72))
        asm.add(box(20.0, 120.0, 40.0, x, 60.0, 207.0), name=f"{side}_forward_2040", color=cq.Color(0.12, 0.45, 0.72))

    asm.add(box(130.0, 40.0, 20.0, 0, 120.0, 0.0), name="front_2040_crossbar", color=cq.Color(0.12, 0.45, 0.72))
    asm.add(box(158.0, 145.0, 6.0, 0, 120.0, 11.0), name="arm_backing_plate", color=cq.Color(0.55, 0.58, 0.62))
    asm.add(arm_interface_plate().translate((0, 120.0, -3.0)), name="printed_arm_interface", color=cq.Color(0.95, 0.55, 0.12))

    return asm


def render_preview(mesh_path: Path, png_path: Path) -> None:
    mesh = trimesh.load_mesh(mesh_path, force="mesh")
    triangles = mesh.triangles
    fig = plt.figure(figsize=(13, 7), dpi=160)
    for index, (azimuth, elevation, title) in enumerate(
        ((34, 24, "Isometric"), (-135, 10, "Rear / side"))
    ):
        ax = fig.add_subplot(1, 2, index + 1, projection="3d")
        collection = Poly3DCollection(
            triangles,
            facecolor="#d9822b",
            edgecolor="#6d3a12",
            linewidths=0.05,
            alpha=1.0,
        )
        ax.add_collection3d(collection)
        bounds = mesh.bounds
        ax.set_xlim(bounds[0, 0], bounds[1, 0])
        ax.set_ylim(bounds[0, 1], bounds[1, 1])
        ax.set_zlim(bounds[0, 2], bounds[1, 2])
        ax.view_init(elev=elevation, azim=azimuth)
        ax.set_box_aspect((170, 150, 225))
        ax.set_title(title)
        ax.set_xlabel("X (mm)")
        ax.set_ylabel("Y (mm)")
        ax.set_zlabel("Z (mm)")
        ax.grid(False)
    fig.suptitle("XLeRobot 0.4 low-front concept: 120 mm reach / 207 mm drop", fontsize=14)
    fig.tight_layout()
    fig.savefig(png_path, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    plate = arm_interface_plate()
    cq.exporters.export(plate, str(OUT / "xlerobot_040_arm_interface.step"))
    cq.exporters.export(
        plate,
        str(OUT / "xlerobot_040_arm_interface.stl"),
        tolerance=0.05,
        angularTolerance=0.1,
    )
    (OUT / "xlerobot_040_arm_interface.svg").write_text(
        plate.toSvg(
            opts={
                "width": 900,
                "height": 840,
                "marginLeft": 70,
                "marginTop": 50,
                "showAxes": True,
                "strokeWidth": 0.5,
            }
        ),
        encoding="utf-8",
    )

    concept = build_low_front_concept()
    concept.save(str(OUT / "xlerobot_040_low_front_concept.step"), exportType="STEP")
    preview_compound = concept.toCompound()
    cq.exporters.export(
        preview_compound,
        str(OUT / "xlerobot_040_low_front_concept_preview.stl"),
        tolerance=0.2,
        angularTolerance=0.2,
    )
    render_preview(
        OUT / "xlerobot_040_low_front_concept_preview.stl",
        OUT / "xlerobot_040_low_front_concept_preview.png",
    )

    metadata = {
        "source_step": "hardware/step/XLeRobot_040/XLeRobot040_armbase.step",
        "source_solid_count": 14,
        "arm_interface_plate_mm": {"width": PLATE_W, "depth": PLATE_D, "thickness": 8.0},
        "dual_arm_interface_m3_clearance_holes_mm": [
            {"x": x, "y": y, "diameter": M3_CLEARANCE} for x, y in INTERFACE_M3_HOLES
        ],
        "frame_m5_clearance_holes_mm": [
            {"x": x, "y": y, "diameter": M5_CLEARANCE} for x, y in FRAME_M5_HOLES
        ],
        "concept_dimensions_mm": {"forward_reach": 120.0, "vertical_drop": 207.0},
        "status": "first-article concept; dimensional fit and structural test pending",
        "printable_parts": ["xlerobot_040_arm_interface.stl"],
        "non_printable_parts_in_preview": ["2020/2040 aluminium rails", "steel M5 fasteners"],
    }
    (OUT / "xlerobot_040_low_front_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Wrote artifacts to {OUT}")


if __name__ == "__main__":
    main()
