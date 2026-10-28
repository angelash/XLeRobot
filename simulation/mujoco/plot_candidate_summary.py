"""Create a compact floor/table comparison chart from experiment JSON."""

import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle


ART = Path(__file__).resolve().parent / "artifacts" / "candidate_schemes"
FLOOR = ART / "report.json"
TABLE = ART / "table_reach_with_collision.json"


def index_rows(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return {row["key"]: row for row in data["ranked"]}


def main():
    floor = index_rows(FLOOR)
    table = index_rows(TABLE)
    keys = list(floor)
    labels = [floor[k]["label"].split(":", 1)[0] for k in keys]
    floor_dist = np.array([floor[k]["tip_to_toy_m"] for k in keys])
    table_dist = np.array([table[k]["tip_to_toy_m"] for k in keys])
    floor_col = ["#24a36a" if floor[k]["feasible"] else "#d85648" for k in keys]
    table_col = ["#24a36a" if table[k]["feasible"] else "#d85648" for k in keys]

    with (ART / "summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "key", "label", "floor_distance_m", "floor_collision_m",
            "floor_feasible", "table_distance_m", "table_collision_m",
            "table_feasible",
        ])
        for key in keys:
            writer.writerow([
                key, floor[key]["label"], floor[key]["tip_to_toy_m"],
                floor[key]["collision_penetration_m"],
                floor[key]["feasible"], table[key]["tip_to_toy_m"],
                table[key]["collision_penetration_m"],
                table[key]["feasible"],
            ])

    fig, axes = plt.subplots(1, 2, figsize=(15, 6), sharey=True)
    positions = np.arange(len(keys))
    for ax, vals, cols, title in (
        (axes[0], floor_dist, floor_col, "Floor toy"),
        (axes[1], table_dist, table_col, "Tabletop toy"),
    ):
        bars = ax.barh(positions, vals, color=cols)
        ax.axvline(0.06, color="#263238", linestyle="--", linewidth=1.2)
        ax.set_xlim(0, max(0.60, float(vals.max()) + 0.05))
        ax.set_yticks(positions, labels)
        ax.invert_yaxis()
        ax.set_title(title)
        ax.set_xlabel("Nearest gripper point to toy (m; lower is better)")
        ax.grid(axis="x", alpha=0.25)
        for bar, value in zip(bars, vals):
            ax.text(
                value + 0.006,
                bar.get_y() + bar.get_height() / 2,
                f"{value:.3f}",
                va="center",
                fontsize=9,
            )
    fig.suptitle(
        "XLeRobot pickup design screening "
        "(green = collision-free within 6 cm)"
    )
    fig.tight_layout()
    fig.savefig(ART / "scheme_scores.png", dpi=170)
    plt.close(fig)

    draw_matrix(floor, table, keys)
    print(ART / "scheme_scores.png")
    print(ART / "scheme_matrix.png")
    print(ART / "summary.csv")


def draw_matrix(floor, table, keys):
    columns = [
        "Candidate",
        "Floor reach",
        "Floor collision",
        "Table reach",
        "Table collision",
        "Both locations",
    ]
    rows = []
    for key in keys:
        f = floor[key]
        t = table[key]
        ff = f["feasible"]
        tf = t["feasible"]
        rows.append([
            f["label"].split(":", 1)[0],
            f"{f['tip_to_toy_m']:.3f} m",
            "PASS" if f["collision_penetration_m"] < 0.003 else
            f"{f['collision_penetration_m'] * 1000:.1f} mm",
            f"{t['tip_to_toy_m']:.3f} m",
            "PASS" if t["collision_penetration_m"] < 0.003 else
            f"{t['collision_penetration_m'] * 1000:.1f} mm",
            "PASS" if ff and tf else "FAIL",
        ])

    fig, ax = plt.subplots(figsize=(12, 8))
    ax.axis("off")
    ax.set_title(
        "XLeRobot floor / tabletop design screening",
        fontsize=18,
        pad=20,
    )
    col_widths = [0.13, 0.16, 0.18, 0.16, 0.18, 0.19]
    x_edges = np.concatenate(([0.0], np.cumsum(col_widths)))
    y_top = len(rows) + 1
    row_h = 1.0
    header_color = "#20384f"
    pass_color = "#dff2e7"
    fail_color = "#f8deda"
    neutral_color = "#f5f7f9"

    for col, (name, width) in enumerate(zip(columns, col_widths)):
        ax.add_patch(Rectangle(
            (x_edges[col], len(rows)), width, row_h,
            facecolor=header_color, edgecolor="white", linewidth=1,
        ))
        ax.text(
            x_edges[col] + width / 2, len(rows) + 0.5, name,
            ha="center", va="center", color="white", fontweight="bold",
        )

    for row_idx, row in enumerate(rows):
        y = len(rows) - row_idx - 1
        f = floor[keys[row_idx]]
        t = table[keys[row_idx]]
        statuses = [
            neutral_color,
            pass_color if f["feasible"] else fail_color,
            pass_color if f["collision_penetration_m"] < 0.003 else fail_color,
            pass_color if t["feasible"] else fail_color,
            pass_color if t["collision_penetration_m"] < 0.003 else fail_color,
            pass_color if f["feasible"] and t["feasible"] else fail_color,
        ]
        for col, (value, width, fill) in enumerate(
            zip(row, col_widths, statuses)
        ):
            ax.add_patch(Rectangle(
                (x_edges[col], y), width, row_h,
                facecolor=fill, edgecolor="white", linewidth=1,
            ))
            ax.text(
                x_edges[col] + width / 2,
                y + 0.5,
                value,
                ha="center",
                va="center",
                fontsize=11,
                fontweight="bold" if col in (0, 5) else "normal",
            )

    ax.set_xlim(0, 1)
    ax.set_ylim(0, y_top)
    fig.text(
        0.5, 0.035,
        "Reach passes at <= 0.06 m. Collision passes below 3 mm. "
        "Long-tool collision was not fully modeled.",
        ha="center", fontsize=10, color="#37474f",
    )
    fig.tight_layout(rect=(0, 0.06, 1, 0.97))
    fig.savefig(ART / "scheme_matrix.png", dpi=180)
    plt.close(fig)


if __name__ == "__main__":
    main()
