"""Design of experiments and quadratic response-surface modelling.

Mirrors the approach SIMIONtuneR uses for SIMION tuning -- a Box-Behnken
design over the tunable voltages, then a second-order response surface fitted
to the resulting figures of merit -- but implemented in pure stdlib so the
server has no numeric dependencies.
"""

import itertools
from typing import Optional, Sequence

Factor = tuple[str, float, float]  # (name, low, high)


# --- designs (in coded units, each factor in [-1, 1]) ----------------------

def coded_box_behnken(k: int, center_points: int = 1) -> list[list[float]]:
    """Box-Behnken design: every factor pair at its four corners, rest centered.

    Valid for 3 to 7 factors, which is the range SIMIONtuneR supports.
    """
    if not 3 <= k <= 7:
        raise ValueError("Box-Behnken requires 3 to 7 factors")
    points: list[list[float]] = []
    for i, j in itertools.combinations(range(k), 2):
        for a, b in itertools.product((-1.0, 1.0), repeat=2):
            row = [0.0] * k
            row[i], row[j] = a, b
            points.append(row)
    points.extend([[0.0] * k for _ in range(max(1, center_points))])
    return points


def coded_factorial(k: int, center_points: int = 1) -> list[list[float]]:
    """Two-level full factorial (all corners) plus center points."""
    points = [list(combo) for combo in itertools.product((-1.0, 1.0), repeat=k)]
    points.extend([[0.0] * k for _ in range(max(0, center_points))])
    return points


def coded_grid(k: int, levels: int = 3) -> list[list[float]]:
    """Full-factorial grid with `levels` evenly spaced levels per factor."""
    if levels < 2:
        raise ValueError("grid requires at least 2 levels")
    axis = [-1.0 + 2.0 * i / (levels - 1) for i in range(levels)]
    return [list(combo) for combo in itertools.product(axis, repeat=k)]


def build_design(
    factors: Sequence[Factor],
    design: str = "box_behnken",
    levels: int = 3,
    center_points: int = 1,
) -> list[dict]:
    """Produce concrete factor settings (real units) for a named design."""
    k = len(factors)
    if k == 0:
        raise ValueError("At least one factor is required")

    if design == "box_behnken":
        coded = coded_box_behnken(k, center_points)
    elif design == "factorial":
        coded = coded_factorial(k, center_points)
    elif design == "grid":
        coded = coded_grid(k, levels)
    else:
        raise ValueError(f"Unknown design {design!r}; use box_behnken, factorial, or grid")

    runs = []
    for row in coded:
        settings = {}
        for value, (name, low, high) in zip(row, factors):
            mid, half = (low + high) / 2.0, (high - low) / 2.0
            settings[name] = mid + value * half
        runs.append({"coded": row, "settings": settings})
    return runs


def to_coded(value: float, low: float, high: float) -> float:
    mid, half = (low + high) / 2.0, (high - low) / 2.0
    return 0.0 if half == 0 else (value - mid) / half


def from_coded(value: float, low: float, high: float) -> float:
    mid, half = (low + high) / 2.0, (high - low) / 2.0
    return mid + value * half


# --- quadratic response surface -------------------------------------------

def _quadratic_terms(x: Sequence[float]) -> list[float]:
    """Design-matrix row: intercept, linear, pure quadratic, two-way products."""
    k = len(x)
    row = [1.0]
    row.extend(x)
    row.extend(xi * xi for xi in x)
    row.extend(x[i] * x[j] for i, j in itertools.combinations(range(k), 2))
    return row


def _solve(matrix: list[list[float]], rhs: list[float]) -> Optional[list[float]]:
    """Gaussian elimination with partial pivoting; None if singular."""
    n = len(matrix)
    aug = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_val = aug[col][col]
        for r in range(col + 1, n):
            factor = aug[r][col] / pivot_val
            if factor:
                for c in range(col, n + 1):
                    aug[r][c] -= factor * aug[col][c]
    solution = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = aug[row][n] - sum(aug[row][c] * solution[c] for c in range(row + 1, n))
        solution[row] = total / aug[row][row]
    return solution


def fit_response_surface(
    coded_points: Sequence[Sequence[float]],
    responses: Sequence[float],
) -> Optional[dict]:
    """Least-squares fit of a second-order model via the normal equations.

    Returns coefficients plus R^2, or None when there are too few runs (or a
    degenerate design) to identify the model.
    """
    k = len(coded_points[0]) if coded_points else 0
    if k == 0:
        return None
    n_terms = 1 + 2 * k + k * (k - 1) // 2
    if len(coded_points) < n_terms:
        return {
            "error": (
                f"Need at least {n_terms} runs to fit a quadratic model in {k} "
                f"factors; got {len(coded_points)}."
            )
        }

    design = [_quadratic_terms(p) for p in coded_points]
    normal = [
        [sum(design[r][i] * design[r][j] for r in range(len(design))) for j in range(n_terms)]
        for i in range(n_terms)
    ]
    rhs = [sum(design[r][i] * responses[r] for r in range(len(design))) for i in range(n_terms)]

    coeffs = _solve(normal, rhs)
    if coeffs is None:
        return {"error": "Design matrix is singular; vary the factors more or add runs."}

    predicted = [sum(c * t for c, t in zip(coeffs, row)) for row in design]
    mean = sum(responses) / len(responses)
    ss_tot = sum((y - mean) ** 2 for y in responses)
    ss_res = sum((y - p) ** 2 for y, p in zip(responses, predicted))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-15 else 1.0

    return {"coefficients": coeffs, "n_terms": n_terms, "r_squared": r2, "k": k}


def predict(model: dict, coded: Sequence[float]) -> float:
    return sum(c * t for c, t in zip(model["coefficients"], _quadratic_terms(coded)))


def optimize_surface(
    model: dict,
    factors: Sequence[Factor],
    goal: str = "min",
    grid_steps: int = 21,
) -> dict:
    """Search the fitted surface over the coded box for its best point.

    A grid search is used rather than the analytic stationary point because the
    stationary point of a quadratic is often a saddle or lies outside the
    design region, which would be meaningless as a recommended setting.
    """
    k = model["k"]
    steps = max(3, grid_steps)
    axis = [-1.0 + 2.0 * i / (steps - 1) for i in range(steps)]

    best_point: Optional[tuple] = None
    best_value: Optional[float] = None
    for combo in itertools.product(axis, repeat=k):
        value = predict(model, combo)
        if best_value is None or (value < best_value if goal == "min" else value > best_value):
            best_value, best_point = value, combo

    settings = {
        name: from_coded(coded, low, high)
        for coded, (name, low, high) in zip(best_point, factors)
    }
    at_edge = [
        name for coded, (name, _, _) in zip(best_point, factors) if abs(abs(coded) - 1.0) < 1e-9
    ]
    return {
        "predicted_value": best_value,
        "settings": settings,
        "coded": list(best_point),
        "factors_at_boundary": at_edge,
    }
