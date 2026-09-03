"""End-to-end validation of the simion-mcp server against the test project.

Exercises every exposed tool against a real SIMION install and asserts on the
results, so a regression in the CLI wrapping, the Lua harness, the CSV capture,
or the response-surface maths shows up as a failing check rather than a
plausible-looking number.

Run:  SIMION_HOME=... python test_project/validate.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_ROOT.parent
sys.path.insert(0, str(REPO_ROOT))

import server  # noqa: E402
from simion_mcp import harness  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []

# Geometry-specific constants for the bundled test lens (see lens.gem).
# The potential array spans x = 0..90 mm; ions that clear x = 85 have exited.
EXIT_PLANE_MM = 85.0
# Focus voltage range over which every ion still reaches the exit plane.
# Above roughly 280 V the centre ring reflects the beam entirely.
FOCUS_FACTORS = [[2, 120, 240], [3, -60, 60]]


def check(name: str, condition: bool, detail: str = "") -> bool:
    results.append((PASS if condition else FAIL, name, detail))
    marker = "[ok]" if condition else "[XX]"
    print(f"  {marker} {name}" + (f" -- {detail}" if detail else ""))
    return condition


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    if not os.environ.get("SIMION_HOME"):
        print("SIMION_HOME is not set; cannot validate against a real install.")
        return 2

    if not (PROJECT_ROOT / "lens.iob").exists():
        print("test_project/lens.iob is missing. The binary half of the fixture is")
        print("not distributed -- see test_project/README.md for how to rebuild it.")
        return 2

    work_root = Path(tempfile.mkdtemp(prefix="simion_mcp_validate_"))
    work_dir = work_root / "lens"
    print(f"Workspace: {work_dir}")

    # -- install ----------------------------------------------------------
    section("1. Installation")
    install = server.simion_check_install()
    if not check("SIMION install resolves", install.get("ok"), install.get("error", "")):
        return 1

    examples = server.simion_list_examples()
    check("bundled examples enumerated",
          examples.get("ok") and examples.get("count", 0) > 0,
          f"{examples.get('count')} examples")

    # -- workspace --------------------------------------------------------
    section("2. Workspace preparation")
    prepared = server.simion_prepare_workspace(str(PROJECT_ROOT), str(work_dir))
    if not check("workbench copied to scratch dir", prepared.get("ok"), prepared.get("error", "")):
        return 1
    check("copy includes the .iob", "lens.iob" in prepared.get("copied", []))
    check("copy leaves originals untouched", (PROJECT_ROOT / "lens.gem").exists())

    iob = str(work_dir / "lens.iob")

    # -- geometry introspection (the simPyon insight) ----------------------
    section("3. Workbench inspection")
    info = server.simion_inspect_workbench(iob)
    if not check("workbench inspected", info.get("ok"), info.get("error", "")):
        return 1

    names = {e["number"]: e.get("name") for e in info.get("electrodes", [])}
    check("all three electrodes found", sorted(names) == [1, 2, 3], f"found {sorted(names)}")
    check("electrode names parsed from .gem",
          names.get(1) == "entrance_barrel"
          and names.get(2) == "focus_ring"
          and names.get(3) == "exit_barrel",
          str(names))
    check("pa_define captured", bool(info.get("pa_define")), str(info.get("pa_define")))
    check("no workbench program present initially",
          info.get("workbench_program", {}).get("state") == "absent")

    # -- geometry -> array -------------------------------------------------
    section("4. Geometry compilation and refining")
    built = server.simion_gem2pa(str(work_dir / "lens.gem"))
    check("gem2pa compiles geometry", built.get("ok"), str(built.get("error_lines") or ""))

    refined = server.simion_refine(str(work_dir / "lens.pa#"), resume=0)
    check("refine solves the array", refined.get("ok"), str(refined.get("error_lines") or ""))
    check("fast-adjust arrays produced",
          all((work_dir / f"lens.pa{n}").exists() for n in (0, 1, 2, 3)))

    # -- flying ------------------------------------------------------------
    section("5. Particle flight and data capture")
    baseline = server.simion_fly(iob, voltages={"2": 0})
    if not check("baseline fly succeeds", baseline.get("ok"),
                 str(baseline.get("error_lines") or "")):
        return 1

    n_base = baseline.get("ion_count", 0)
    check("per-ion records captured", n_base > 0, f"{n_base} ions")
    check("CSV written to disk", Path(baseline["csv_path"]).exists())

    row = (baseline.get("ions") or [{}])[0]
    check("records carry full kinematics",
          all(row.get(k) is not None for k in ("tof_usec", "x_mm", "ke_ev", "mass_amu")),
          f"tof={row.get('tof_usec')}, x={row.get('x_mm')}, ke={row.get('ke_ev')}")

    # other_actions must capture boundary-exiting ions, not just electrode hits
    n_splat = baseline["metrics"].get("n_hit_electrode", 0)
    check("boundary-exiting ions recorded, not only electrode hits",
          n_base > n_splat,
          f"{n_base} recorded vs {n_splat} electrode hits")

    # -- voltage control actually changes physics --------------------------
    section("6. Electrode voltage control")
    energized = server.simion_fly(iob, voltages={"2": 1200})
    check("energized fly succeeds", energized.get("ok"),
          str(energized.get("error_lines") or ""))

    def axial_positions(res):
        return [r.get("x_mm") for r in res.get("ions", [])]

    check("focus_ring voltage changes trajectories",
          axial_positions(baseline) != axial_positions(energized),
          f"baseline x[0]={axial_positions(baseline)[:1]}, "
          f"energized x[0]={axial_positions(energized)[:1]}")

    check("voltages echoed back", energized.get("voltages_applied") == {"2": 1200.0})

    # -- measurement zone (simPyon's "good particle" filter) ---------------
    # The array spans x = 0..90 mm, so the exit plane sits just past x = 85.
    section("7. Measurement zone metrics")
    zoned = server.simion_fly(iob, voltages={"2": 0}, x_min=EXIT_PLANE_MM)
    metrics = zoned.get("metrics", {})
    transmission = metrics.get("transmission")
    check("transmission reported for a zone", "transmission" in metrics,
          f"transmission={transmission}")
    check("transmission is a fraction",
          transmission is not None and 0.0 <= transmission <= 1.0)
    check("undeflected beam reaches the exit plane", transmission == 1.0,
          f"transmission={transmission}")
    check("rms spot size computed", metrics.get("rms_radius_mm") is not None,
          f"rms_radius_mm={metrics.get('rms_radius_mm')}")

    # -- static PA adjustment ---------------------------------------------
    section("8. Static fast-adjust")
    pa0 = work_dir / "lens.pa0"
    before = pa0.read_bytes()
    adjusted = server.simion_fastadj(str(pa0), {"2": 750})
    check("fastadj runs", adjusted.get("ok"), str(adjusted.get("error_lines") or ""))
    check("fastadj mutates the array on disk", pa0.read_bytes() != before)
    server.simion_refine(str(work_dir / "lens.pa#"), resume=0)  # restore

    # -- harness safety ----------------------------------------------------
    section("9. Workbench-program safety")
    lua_path = work_dir / "lens.lua"
    check("harness marked as generated", harness.is_generated(lua_path))

    lua_path.write_text("-- hand written workbench program\nsimion.workbench_program()\n")
    refused = server.simion_fly(iob, voltages={"2": 100})
    check("refuses to clobber a hand-written program",
          not refused.get("ok") and "allow_overwrite" in (refused.get("error") or ""),
          (refused.get("error") or "")[:80])

    allowed = server.simion_fly(iob, voltages={"2": 100}, allow_overwrite_lua=True)
    check("overwrites when explicitly allowed", allowed.get("ok"))
    check("backup of the replaced program kept",
          bool((allowed.get("harness") or {}).get("backup"))
          and Path(allowed["harness"]["backup"]).exists())

    # -- designed sweep ----------------------------------------------------
    # Focus quality (rms spot radius at the exit plane) is the objective: it
    # varies smoothly with the centre-ring voltage while every ion still gets
    # through, which is what makes it fittable.
    section("10. Designed voltage sweep")
    sweep = server.simion_sweep(
        iob,
        factors=FOCUS_FACTORS,
        design="grid",
        levels=3,
        x_min=EXIT_PLANE_MM,
        allow_overwrite_lua=True,
    )
    if not check("sweep completes", sweep.get("ok"), f"{sweep.get('n_failed')} failed runs"):
        print("   sweep error:", sweep.get("error"))
        return 1
    check("grid design has 3^2 points", sweep.get("n_runs") == 9, f"{sweep.get('n_runs')} runs")
    check("each run carries metrics",
          all(r.get("metrics") for r in sweep.get("runs", [])))
    check("each run records its voltages",
          all(set(r.get("voltages", {})) == {"2", "3"} for r in sweep.get("runs", [])))
    check("beam survives the whole design region",
          all(r["metrics"].get("transmission") == 1.0 for r in sweep["runs"]))

    spots = [r["metrics"].get("rms_radius_mm") for r in sweep["runs"]]
    check("sweep explores distinct focus qualities", len(set(spots)) == len(spots),
          f"rms range {min(spots):.3f}..{max(spots):.3f} mm")

    # -- response surface ---------------------------------------------------
    section("11. Response-surface optimisation")
    fit = server.simion_fit_response_surface(
        sweep["runs"], metric="rms_radius_mm", goal="min", factors=FOCUS_FACTORS,
    )
    if not check("surface fitted", fit.get("ok"), fit.get("error", "")):
        return 1
    check("R^2 reported", isinstance(fit.get("r_squared"), float),
          f"R^2={fit.get('r_squared'):.4f}")
    check("fit quality described", bool(fit.get("fit_quality")), str(fit.get("fit_quality")))
    check("recommendation covers both electrodes",
          set(fit.get("recommended_voltages", {})) == {"2", "3"},
          str(fit.get("recommended_voltages")))
    check("recommendation lies inside the swept range",
          FOCUS_FACTORS[0][1] <= fit["recommended_voltages"]["2"] <= FOCUS_FACTORS[0][2]
          and FOCUS_FACTORS[1][1] <= fit["recommended_voltages"]["3"] <= FOCUS_FACTORS[1][2])

    verify = server.simion_fly(
        iob,
        voltages=fit["recommended_voltages"],
        x_min=EXIT_PLANE_MM,
        allow_overwrite_lua=True,
    )
    check("recommended setting is simulable", verify.get("ok"))
    achieved = verify["metrics"].get("rms_radius_mm")
    check("verification reports a spot size", achieved is not None,
          f"predicted={fit.get('predicted_value'):.4f} mm, achieved={achieved:.4f} mm"
          if achieved is not None else "no spot size")

    # The whole point of tuning: the recommendation must actually focus better
    # than the unenergised lens.
    unfocused = zoned["metrics"].get("rms_radius_mm")
    check("tuned lens focuses better than the unenergised lens",
          achieved is not None and unfocused is not None and achieved < unfocused,
          f"tuned={achieved:.4f} mm vs unenergised={unfocused:.4f} mm"
          if achieved and unfocused else "")
    # A single quadratic pass over-extrapolates a sharp optimum -- the model
    # predicts a smaller spot than the lens can actually deliver. What the fit
    # must get right is the *region*; closing the remaining gap is the job of a
    # second sweep narrowed around this point. Asserting the recommendation
    # beats every swept point would be asserting something RSM does not promise.
    check("tuned setting lands in the right region",
          achieved is not None and achieved <= min(spots) * 2.0,
          f"tuned={achieved:.4f} mm vs best swept={min(spots):.4f} mm"
          if achieved else "")
    if achieved is not None:
        gap = achieved - fit["predicted_value"]
        print(f"     note: model under-predicted spot size by {gap:.3f} mm; "
              "narrow the ranges and re-sweep to refine.")

    # -- escape hatch -------------------------------------------------------
    section("12. Lua escape hatch")
    lua = server.simion_run_lua(lua_code='print("MCP_LUA_OK " .. (1+1))')
    check("arbitrary Lua executes",
          lua.get("ok") and "MCP_LUA_OK 2" in lua.get("stdout", ""))

    bad = server.simion_run_lua(lua_code="x=1", lua_file="whatever.lua")
    check("rejects ambiguous lua_code + lua_file", not bad.get("ok"))

    missing = server.simion_fly(str(work_dir / "does_not_exist.iob"))
    check("missing workbench reported cleanly",
          not missing.get("ok") and "not found" in (missing.get("error") or ""))

    # -- report -------------------------------------------------------------
    failed = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 60)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFailed checks:")
        for _, name, detail in failed:
            print(f"  - {name}" + (f" ({detail})" if detail else ""))
    print("=" * 60)

    shutil.rmtree(work_root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
