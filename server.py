"""MCP server for SIMION (ion/electron optics simulator).

Drives SIMION through its headless CLI (`simion.exe --nogui --noprompt ...`).
Voltage control and per-ion data capture go through a generated workbench Lua
program, because SIMION exposes no external API for either.

Set SIMION_HOME to the directory containing simion.exe.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from simion_mcp import analysis, core, doe, harness  # noqa: E402

mcp = FastMCP("SIMION")

DEFAULT_CSV = "simion_mcp_result.csv"


def _resolve_iob(iob_path: str) -> tuple:
    iob = Path(iob_path).resolve()
    if not iob.exists():
        return None, {"ok": False, "error": f"Workbench (.iob) not found: {iob}"}
    return iob, None


@mcp.tool()
def simion_check_install() -> dict:
    """Verify SIMION_HOME points at a usable SIMION installation."""
    err = core.check_install()
    if err:
        return {"ok": False, "error": err}
    return {
        "ok": True,
        "simion_home": str(core.simion_home()),
        "simion_exe": str(core.simion_exe()),
    }


@mcp.tool()
def simion_list_examples() -> dict:
    """List example workbenches bundled with the SIMION install.

    Useful as templates or as known-good targets for smoke tests.
    """
    err = core.check_install()
    if err:
        return {"ok": False, "error": err}
    examples_dir = core.simion_home() / "examples"
    if not examples_dir.exists():
        return {"ok": False, "error": f"No examples/ directory at {examples_dir}"}
    out = []
    for child in sorted(examples_dir.iterdir()):
        if child.is_dir():
            iobs = sorted(f.name for f in child.glob("*.iob"))
            if iobs:
                out.append({
                    "example": child.name,
                    "path": str(child),
                    "iob_files": iobs,
                })
    return {"ok": True, "count": len(out), "examples": out}


@mcp.tool()
def simion_prepare_workspace(
    source_dir: str,
    work_dir: str,
    overwrite: bool = False,
) -> dict:
    """Copy a workbench into a working directory before simulating.

    Strongly recommended before any run that sets voltages: driving electrodes
    requires writing a workbench Lua program next to the .iob, so working on a
    copy keeps the original untouched. `source_dir` may be a bundled example
    directory (see simion_list_examples) or any folder holding a workbench.
    """
    result = core.copy_workbench(Path(source_dir), Path(work_dir), overwrite=overwrite)
    if result.get("ok"):
        iobs = sorted(p.name for p in Path(result["work_dir"]).glob("*.iob"))
        result["iob_files"] = iobs
    return result


@mcp.tool()
def simion_inspect_workbench(iob_path: str) -> dict:
    """Inspect a workbench: electrodes, geometry metadata, and related files.

    Electrode numbers and names are read from the .gem geometry source, where
    a name is the trailing comment on the declaration (`electrode(1); lens`).
    This is the only way to learn which electrodes exist and what they are
    called -- the .iob and .pa files are binary and the CLI cannot enumerate
    them. Use the reported numbers as keys for simion_fly's `voltages`.
    """
    iob, err = _resolve_iob(iob_path)
    if err:
        return err

    work_dir = iob.parent
    gem_files = sorted(work_dir.glob("*.gem"))
    preferred = iob.with_suffix(".gem")
    if preferred.exists():
        gem_files = [preferred] + [g for g in gem_files if g != preferred]

    electrodes: dict[int, dict] = {}
    pa_define = None
    parsed_from = []
    for gem in gem_files:
        info = core.parse_gem(gem)
        if info.get("error"):
            continue
        parsed_from.append(gem.name)
        for num, entry in info.get("electrodes", {}).items():
            existing = electrodes.get(num)
            if existing is None:
                electrodes[num] = entry
            elif existing.get("name") is None and entry.get("name"):
                existing["name"] = entry["name"]
        pa_define = pa_define or info.get("pa_define")

    lua_path = iob.with_suffix(".lua")
    lua_state = "absent"
    if lua_path.exists():
        lua_state = "simion-mcp harness" if harness.is_generated(lua_path) else "user-authored"

    adjustable = sorted(n for n in electrodes if n != 0)
    return {
        "ok": True,
        "iob": str(iob),
        "work_dir": str(work_dir),
        "electrodes": [electrodes[n] for n in sorted(electrodes)],
        "adjustable_electrode_numbers": adjustable,
        "pa_define": pa_define,
        "gem_files_parsed": parsed_from,
        "workbench_program": {"path": str(lua_path), "state": lua_state},
        "files": sorted(p.name for p in work_dir.iterdir() if p.is_file()),
        "note": (
            "Electrode 0 is SIMION's implicit ground. If no electrodes were found, "
            "the geometry may be defined only inside the binary .pa files, in which "
            "case voltages cannot be driven without the original .gem source."
        ) if not adjustable else None,
    }


@mcp.tool()
def simion_gem2pa(gem_path: str, timeout_sec: int = core.DEFAULT_TIMEOUT_SEC) -> dict:
    """Compile a .gem geometry file into a potential array (.pa#).

    Run simion_refine afterwards to solve the array before flying.
    """
    gem = Path(gem_path).resolve()
    if not gem.exists():
        return {"ok": False, "error": f"gem file not found: {gem}"}
    result = core.run_simion(["gem2pa", str(gem)], cwd=gem.parent, timeout_sec=timeout_sec)
    result["parsed_geometry"] = core.parse_gem(gem)
    return result


@mcp.tool()
def simion_refine(
    pa_path: str,
    resume: int = 0,
    convergence: float | None = None,
    timeout_sec: int = core.DEFAULT_TIMEOUT_SEC,
) -> dict:
    """Refine (solve) a potential array to convergence.

    pa_path: the .pa# file to refine.
    resume: 0 to start fresh, 1 to continue from existing .pa[0-9] files.
    convergence: target in Volts; omit to use the array's own default.
    """
    pa = Path(pa_path).resolve()
    if not pa.exists():
        return {"ok": False, "error": f"PA file not found: {pa}"}
    args = ["refine", f"--resume={resume}"]
    if convergence is not None:
        args.append(f"--convergence={convergence}")
    args.append(str(pa))
    return core.run_simion(args, cwd=pa.parent, timeout_sec=timeout_sec)


@mcp.tool()
def simion_fastadj(
    pa0_path: str,
    voltages: dict,
    timeout_sec: int = core.DEFAULT_TIMEOUT_SEC,
) -> dict:
    """Write electrode voltages directly into a fast-adjustable .pa0 file.

    voltages: {electrode_number: volts}, e.g. {"1": 0, "2": 500}.

    Note this changes the array on disk but does NOT control a fly: loading a
    workbench re-applies the potentials stored in its .iob, overriding what is
    written here. To set voltages for a simulation, use simion_fly's
    `voltages` argument instead. This tool is for preparing a standalone array.
    """
    pa0 = Path(pa0_path).resolve()
    if not pa0.exists():
        return {"ok": False, "error": f"PA file not found: {pa0}"}
    if not voltages:
        return {"ok": False, "error": "No voltages given."}

    parts = []
    for key, value in voltages.items():
        try:
            num = int(key)
            volts = float(value)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"Invalid electrode/voltage pair: {key!r}={value!r}"}
        parts.append(f"{num}={volts:g}")

    return core.run_simion(
        ["fastadj", str(pa0), ",".join(parts)], cwd=pa0.parent, timeout_sec=timeout_sec
    )


@mcp.tool()
def simion_fly(
    iob_path: str,
    voltages: dict | None = None,
    lua_vars: dict | None = None,
    csv_name: str = DEFAULT_CSV,
    x_min: float | None = None,
    x_max: float | None = None,
    r_min: float | None = None,
    r_max: float | None = None,
    allow_overwrite_lua: bool = False,
    install_harness: bool = True,
    disk: int = 0,
    timeout_sec: int = core.DEFAULT_TIMEOUT_SEC,
    max_rows_returned: int = 200,
) -> dict:
    """Fly particles through a workbench and return per-ion results and metrics.

    voltages: {electrode_number: volts} to drive, e.g. {"2": 1200}. Electrodes
        omitted keep the potentials stored in the workbench. Get valid numbers
        from simion_inspect_workbench.
    lua_vars: extra Lua globals to inject before the run (for workbenches with
        their own `adjustable` variables).
    x_min/x_max/r_min/r_max: optional measurement zone. When given, metrics
        report transmission into that zone and describe only the ions that
        reached it; r is the off-axis radius sqrt(y^2+z^2).
    install_harness: writes a workbench Lua program next to the .iob to drive
        voltages and record results. Set False to run a workbench that already
        has its own program (results then come only from parsed stdout).
    allow_overwrite_lua: permit replacing a hand-written workbench program
        (a timestamped .bak is kept). Prefer simion_prepare_workspace instead.

    Returns metrics, and per-ion rows capped at max_rows_returned; the full
    table is always written to csv_name in the workbench directory.
    """
    iob, err = _resolve_iob(iob_path)
    if err:
        return err
    work_dir = iob.parent

    electrode_numbers: list[int] = []
    mcp_vars: dict = {}
    if voltages:
        for key, value in voltages.items():
            try:
                num = int(key)
                volts = float(value)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"Invalid electrode/voltage pair: {key!r}={value!r}"}
            electrode_numbers.append(num)
            mcp_vars[f"MCP_V{num}"] = volts

    harness_info = None
    if install_harness:
        harness_info = harness.install_harness(
            iob, electrodes=electrode_numbers, allow_overwrite=allow_overwrite_lua
        )
        if not harness_info.get("ok"):
            return harness_info
        mcp_vars["MCP_CSV"] = csv_name
    elif voltages:
        return {
            "ok": False,
            "error": (
                "voltages require install_harness=true, since SIMION can only set "
                "electrode potentials from a workbench Lua program."
            ),
        }

    try:
        lua_args = core.lua_assignments({**mcp_vars, **(lua_vars or {})})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    csv_path = work_dir / csv_name
    if csv_path.exists():
        csv_path.unlink()

    result = core.run_simion(
        [*lua_args, "fly", f"--disk={disk}", str(iob)],
        cwd=work_dir,
        timeout_sec=timeout_sec,
    )
    result["harness"] = harness_info
    result["csv_path"] = str(csv_path)
    if voltages:
        result["voltages_applied"] = {str(k): float(v) for k, v in voltages.items()}

    rows = analysis.read_results(csv_path)
    result["metrics"] = analysis.compute_metrics(rows, x_min, x_max, r_min, r_max)
    result["ion_count"] = len(rows)
    result["ions"] = rows[:max_rows_returned]
    if len(rows) > max_rows_returned:
        result["ions_truncated"] = (
            f"Showing {max_rows_returned} of {len(rows)} ions; full data in {csv_path}."
        )
    # stdout is long and the CSV supersedes it once the harness ran.
    if rows:
        result.pop("stdout", None)
    return result


@mcp.tool()
def simion_sweep(
    iob_path: str,
    factors: list,
    design: str = "box_behnken",
    levels: int = 3,
    center_points: int = 1,
    x_min: float | None = None,
    x_max: float | None = None,
    r_min: float | None = None,
    r_max: float | None = None,
    allow_overwrite_lua: bool = False,
    timeout_sec: int = core.DEFAULT_TIMEOUT_SEC,
    max_runs: int = 200,
) -> dict:
    """Run a designed voltage sweep and tabulate the resulting metrics.

    factors: list of [electrode_number, low_volts, high_volts], e.g.
        [[2, 0, 2000], [3, -500, 500]].
    design: "box_behnken" (3-7 factors, efficient for a quadratic model),
        "factorial" (all corners plus center), or "grid" (`levels` per factor).

    Each design point is a separate SIMION fly, so runs multiply quickly --
    max_runs guards against an accidentally huge design. Feed the returned
    `runs` into simion_fit_response_surface to find an optimum.
    """
    iob, err = _resolve_iob(iob_path)
    if err:
        return err

    parsed: list = []
    for entry in factors or []:
        if isinstance(entry, dict):
            num, low, high = entry.get("electrode"), entry.get("low"), entry.get("high")
        elif isinstance(entry, (list, tuple)) and len(entry) == 3:
            num, low, high = entry
        else:
            return {"ok": False, "error": f"Bad factor {entry!r}; expected [electrode, low, high]."}
        try:
            parsed.append((f"V{int(num)}", float(low), float(high)))
        except (TypeError, ValueError):
            return {"ok": False, "error": f"Bad factor {entry!r}; values must be numeric."}
    if not parsed:
        return {"ok": False, "error": "At least one factor is required."}

    try:
        design_runs = doe.build_design(parsed, design=design, levels=levels,
                                       center_points=center_points)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if len(design_runs) > max_runs:
        return {
            "ok": False,
            "error": (
                f"Design would require {len(design_runs)} SIMION runs (limit {max_runs}). "
                "Reduce factors/levels or raise max_runs deliberately."
            ),
        }

    electrode_numbers = [int(name[1:]) for name, _, _ in parsed]
    harness_info = harness.install_harness(
        iob, electrodes=electrode_numbers, allow_overwrite=allow_overwrite_lua
    )
    if not harness_info.get("ok"):
        return harness_info

    work_dir = iob.parent
    csv_path = work_dir / DEFAULT_CSV
    runs_out = []
    failures = 0

    for index, run in enumerate(design_runs):
        mcp_vars = {f"MCP_{name}": value for name, value in run["settings"].items()}
        mcp_vars["MCP_CSV"] = DEFAULT_CSV
        if csv_path.exists():
            csv_path.unlink()

        outcome = core.run_simion(
            [*core.lua_assignments(mcp_vars), "fly", "--disk=0", str(iob)],
            cwd=work_dir,
            timeout_sec=timeout_sec,
        )
        rows = analysis.read_results(csv_path)
        if not outcome.get("ok"):
            failures += 1
        runs_out.append({
            "run": index,
            "coded": run["coded"],
            "voltages": {name[1:]: value for name, value in run["settings"].items()},
            "ok": outcome.get("ok", False),
            "error_lines": outcome.get("error_lines") or None,
            "metrics": analysis.compute_metrics(rows, x_min, x_max, r_min, r_max),
        })

    return {
        "ok": failures == 0,
        "design": design,
        "factors": [{"electrode": int(n[1:]), "low": lo, "high": hi} for n, lo, hi in parsed],
        "n_runs": len(runs_out),
        "n_failed": failures,
        "harness": harness_info,
        "runs": runs_out,
        "next_step": (
            "Pass these runs plus a metric path (e.g. 'rms_radius_mm' or "
            "'transmission') to simion_fit_response_surface."
        ),
    }


@mcp.tool()
def simion_fit_response_surface(
    runs: list,
    metric: str,
    goal: str = "min",
    factors: list | None = None,
) -> dict:
    """Fit a quadratic response surface to sweep results and locate an optimum.

    runs: the `runs` list returned by simion_sweep.
    metric: dotted path into each run's metrics, e.g. "rms_radius_mm",
        "transmission", or "tof_usec.std".
    goal: "min" or "max".
    factors: optional [[electrode, low, high], ...] to map the optimum back to
        volts; taken from the sweep's own factor ranges when omitted.

    The recommended setting is a model prediction, not a simulated result --
    verify it with simion_fly before trusting it. If any factor lands on the
    boundary, the true optimum probably lies outside the swept range.
    """
    if goal not in ("min", "max"):
        return {"ok": False, "error": "goal must be 'min' or 'max'"}

    usable = [r for r in runs or [] if r.get("ok") and isinstance(r.get("coded"), list)]
    points, responses, skipped = [], [], 0
    for run in usable:
        value = analysis.metric_value(run.get("metrics") or {}, metric)
        if value is None:
            skipped += 1
            continue
        points.append(run["coded"])
        responses.append(value)

    if len(points) < 3:
        return {
            "ok": False,
            "error": (
                f"Only {len(points)} runs had a usable '{metric}' value "
                f"({skipped} lacked it). Check the metric name against a run's metrics."
            ),
        }

    model = doe.fit_response_surface(points, responses)
    if model is None or model.get("error"):
        return {"ok": False, "error": (model or {}).get("error", "Could not fit model."),
                "n_points": len(points)}

    resolved: list = []
    if factors:
        for entry in factors:
            if isinstance(entry, dict):
                resolved.append((f"V{int(entry['electrode'])}",
                                 float(entry["low"]), float(entry["high"])))
            else:
                num, low, high = entry
                resolved.append((f"V{int(num)}", float(low), float(high)))
    else:
        # Recover ranges from the observed voltages at the coded extremes.
        names = sorted({k for r in usable for k in (r.get("voltages") or {})}, key=int)
        for idx, name in enumerate(names):
            values = [
                float(r["voltages"][name])
                for r in usable
                if name in (r.get("voltages") or {}) and idx < len(r["coded"])
            ]
            resolved.append((f"V{name}", min(values), max(values)))

    if len(resolved) != model["k"]:
        return {
            "ok": False,
            "error": (
                f"Model has {model['k']} factors but {len(resolved)} factor ranges were "
                "resolved. Pass `factors` explicitly."
            ),
        }

    best = doe.optimize_surface(model, resolved, goal=goal)
    observed = min(responses) if goal == "min" else max(responses)
    best_run = usable[responses.index(observed)]

    return {
        "ok": True,
        "metric": metric,
        "goal": goal,
        "n_points": len(points),
        "r_squared": model["r_squared"],
        "fit_quality": (
            "good" if model["r_squared"] > 0.9
            else "moderate" if model["r_squared"] > 0.7
            else "poor - treat the recommendation as a hint only"
        ),
        "recommended_voltages": {name[1:]: volts for name, volts in best["settings"].items()},
        "predicted_value": best["predicted_value"],
        "factors_at_boundary": [n[1:] for n in best["factors_at_boundary"]] or None,
        "best_observed": {"value": observed, "voltages": best_run.get("voltages")},
        "next_step": "Verify recommended_voltages with simion_fly.",
    }


@mcp.tool()
def simion_run_lua(
    lua_code: str | None = None,
    lua_file: str | None = None,
    iob_path: str | None = None,
    fly_after: bool = False,
    timeout_sec: int = core.DEFAULT_TIMEOUT_SEC,
) -> dict:
    """Escape hatch: run arbitrary Lua through simion.exe's --lua flag.

    For anything the typed tools don't cover -- probing simion.pas, dumping
    array metadata, or driving a workbench's own Lua API.

    Two constraints worth knowing: code containing `simion.workbench_program()`
    cannot be run this way (SIMION only auto-runs workbench programs named
    after the .iob), and `simion.wb` objects are C userdata, so `pairs()` over
    them errors -- probe documented fields with pcall instead.
    """
    if bool(lua_code) == bool(lua_file):
        return {"ok": False, "error": "Provide exactly one of lua_code or lua_file"}

    if lua_file:
        path = Path(lua_file).resolve()
        if not path.exists():
            return {"ok": False, "error": f"lua_file not found: {path}"}
        args = ["--lua", f"@{path}"]
    else:
        args = ["--lua", lua_code]

    cwd = None
    if iob_path:
        iob, err = _resolve_iob(iob_path)
        if err:
            return err
        cwd = iob.parent
        if fly_after:
            args += ["fly", "--disk=0", str(iob)]

    return core.run_simion(args, cwd=cwd, timeout_sec=timeout_sec)


if __name__ == "__main__":
    mcp.run(transport="stdio")
