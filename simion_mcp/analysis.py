"""Parsing of harness CSV output and derived figures of merit."""

import csv
import math
import statistics
from pathlib import Path
from typing import Optional

NUMERIC_COLUMNS = {
    "ion_n", "tof_usec", "x_mm", "y_mm", "z_mm",
    "vx", "vy", "vz", "ke_ev", "mass_amu", "charge",
    "hit_electrode", "initial_ke_ev",
}


def read_results(csv_path: Path) -> list[dict]:
    """Read the harness CSV into a list of per-ion dicts with numeric fields."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return []
    rows: list[dict] = []
    with csv_path.open(newline="", errors="replace") as fh:
        for raw in csv.DictReader(fh):
            row: dict = {}
            for key, value in raw.items():
                if key is None:
                    continue
                if key in NUMERIC_COLUMNS:
                    try:
                        row[key] = float(value)
                    except (TypeError, ValueError):
                        row[key] = None
                else:
                    row[key] = value
            rows.append(row)
    return rows


def in_zone(
    row: dict,
    x_min: Optional[float] = None,
    x_max: Optional[float] = None,
    r_min: Optional[float] = None,
    r_max: Optional[float] = None,
) -> bool:
    """Whether an ion's final position falls inside a measurement zone.

    r is the off-axis radius sqrt(y^2+z^2), matching SIMION's cylindrical
    convention where x is the optical axis.
    """
    x = row.get("x_mm")
    y = row.get("y_mm") or 0.0
    z = row.get("z_mm") or 0.0
    if x is None:
        return False
    if x_min is not None and x < x_min:
        return False
    if x_max is not None and x > x_max:
        return False
    r = math.hypot(y, z)
    if r_min is not None and r < r_min:
        return False
    if r_max is not None and r > r_max:
        return False
    return True


def _stats(values: list[float]) -> Optional[dict]:
    values = [v for v in values if v is not None]
    if not values:
        return None
    out = {
        "n": len(values),
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
    }
    out["std"] = statistics.pstdev(values) if len(values) > 1 else 0.0
    return out


def compute_metrics(
    rows: list[dict],
    x_min: Optional[float] = None,
    x_max: Optional[float] = None,
    r_min: Optional[float] = None,
    r_max: Optional[float] = None,
) -> dict:
    """Summarize a fly result into figures of merit for tuning.

    If any zone bound is given, ions are additionally classified by whether
    they reached that zone, and the spread/TOF statistics describe only those
    ions -- the analogue of simPyon's "good" particle filter.
    """
    if not rows:
        return {"n_ions": 0, "note": "No ion records; check that the fly actually ran."}

    zone_specified = any(v is not None for v in (x_min, x_max, r_min, r_max))
    selected = [r for r in rows if in_zone(r, x_min, x_max, r_min, r_max)] if zone_specified else rows

    n_ions = len(rows)
    n_splat = sum(1 for r in rows if (r.get("hit_electrode") or 0) >= 1)

    metrics = {
        "n_ions": n_ions,
        "n_hit_electrode": n_splat,
        "electrode_loss_fraction": n_splat / n_ions,
    }
    if zone_specified:
        metrics["n_in_zone"] = len(selected)
        metrics["transmission"] = len(selected) / n_ions

    if not selected:
        metrics["note"] = "No ions satisfied the measurement zone."
        return metrics

    tof = _stats([r.get("tof_usec") for r in selected])
    ke = _stats([r.get("ke_ev") for r in selected])
    radius = _stats([math.hypot(r.get("y_mm") or 0.0, r.get("z_mm") or 0.0) for r in selected])

    metrics["tof_usec"] = tof
    metrics["ke_ev"] = ke
    metrics["radius_mm"] = radius
    metrics["y_mm"] = _stats([r.get("y_mm") for r in selected])
    metrics["z_mm"] = _stats([r.get("z_mm") for r in selected])

    # Spot size: RMS radius is the usual focusing figure of merit -- smaller is
    # a tighter focus at the measurement plane.
    metrics["rms_radius_mm"] = math.sqrt(
        statistics.fmean([
            (r.get("y_mm") or 0.0) ** 2 + (r.get("z_mm") or 0.0) ** 2 for r in selected
        ])
    )

    # TOF resolving power t/(2*dt); meaningful for time-of-flight geometries.
    if tof and tof["std"] > 0:
        metrics["tof_resolution"] = tof["mean"] / (2.0 * tof["std"])

    return metrics


def metric_value(metrics: dict, path: str) -> Optional[float]:
    """Look up a possibly-nested metric by dotted path, e.g. 'tof_usec.std'."""
    node = metrics
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return float(node) if isinstance(node, (int, float)) else None
