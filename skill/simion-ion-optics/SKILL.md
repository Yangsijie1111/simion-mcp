---
name: simion-ion-optics
description: Run and tune SIMION ion/electron optics simulations through the simion MCP server - inspecting workbench geometry and electrode names, compiling .gem geometry into refined potential arrays, flying particles with driven electrode voltages, and tuning voltages against a figure of merit (focus spot size, transmission, time-of-flight spread) using designed sweeps and response-surface optimisation. Use whenever the user asks to simulate, fly, focus, tune, optimise, or troubleshoot an ion lens, einzel lens, ion funnel, mass spectrometer, time-of-flight, quadrupole, ion trap, or any SIMION workbench, .iob, .gem, .pa, or .fly file.
---

# SIMION ion optics

Drive SIMION through the `simion` MCP server. SIMION has no external API, so
every tool wraps its headless CLI (`simion.exe --nogui --noprompt`) plus a
generated workbench Lua program. Understanding that shapes what is and is not
possible below.

## Before anything else

Never guess electrode numbers. Call `simion_inspect_workbench` first — it reads
electrode numbers and names out of the `.gem` source, which is the only place
they exist in readable form (`.iob` and `.pa` files are binary). The numbers it
reports are the keys for every voltage argument.

If it reports no electrodes, the `.gem` source is missing. Voltages cannot be
driven without it; say so rather than proceeding.

## Work on a copy

Driving voltages requires writing a workbench Lua program next to the `.iob`.
Call `simion_prepare_workspace` to copy the workbench into a scratch directory
and work there. This matters because the user's originals are simulation
inputs they may have spent real effort building.

If a hand-written workbench program already exists, `simion_fly` refuses to
replace it. That refusal is correct — do not reflexively pass
`allow_overwrite_lua=true`. Either work in a copy, or read the existing
program first (it may define the very `adjustable` variables the user wants
driven, which you can then set via `lua_vars` instead).

## The normal path

1. `simion_inspect_workbench` — electrodes, names, geometry, existing program.
2. `simion_gem2pa` then `simion_refine` — only if the `.pa0`/`.pa1`… arrays are
   missing or the geometry changed. Refining is what makes electrodes
   fast-adjustable; without it, voltages cannot be driven.
3. `simion_fly` — one run with `voltages={"2": 500}`.
4. `simion_sweep` → `simion_fit_response_surface` — tuning.

## Choosing a measurement zone

Metrics are far more useful when scoped to where the beam is supposed to land.
Pass `x_min`/`x_max`/`r_min`/`r_max` to `simion_fly` and `simion_sweep`; `x` is
the optical axis and `r` is the off-axis radius. This is what turns raw
end-positions into `transmission` and `rms_radius_mm`.

Do not invent a zone. Fly once with no zone, look at the actual spread of
`x_mm` in the returned ions, and place the plane just inside where the beam
exits. A zone outside the array silently yields `transmission = 0` and no spot
size — if you see that, the zone is wrong, not the optics.

## Tuning

Pick the figure of merit from what the user actually wants:

| Goal | `metric` | `goal` |
|---|---|---|
| Tighter focus | `rms_radius_mm` | `min` |
| More signal through an aperture | `transmission` | `max` |
| Sharper TOF peak | `tof_usec.std` | `min` |
| TOF resolving power | `tof_resolution` | `max` |

Designs: `box_behnken` for 3–7 factors (fewest runs for a quadratic model, and
what SIMIONtuneR uses); `grid` for 1–2 factors; `factorial` to screen which
electrodes matter before tuning them.

Every design point is a separate SIMION run, so cost is the product of the
levels. Check the run count before launching a large design.

**Choose ranges the beam survives.** A response surface fitted across a cliff
where ions stop arriving is meaningless. If `transmission` collapses to 0 in
part of the design, narrow the range and re-sweep rather than fitting through it.

## Reading the fit honestly

`simion_fit_response_surface` returns a *model prediction*, not a simulated
result. Three things to check before reporting it:

- **Always verify** with `simion_fly` at the recommended voltages, and report
  the achieved value, not the predicted one.
- **`factors_at_boundary`** non-empty means the optimum is pinned to the edge of
  the swept range — the true optimum probably lies outside it. Widen and re-sweep.
- **A sharp optimum gets over-extrapolated.** A quadratic cannot represent a
  narrow focus minimum, so the prediction is often optimistic. The fit locates
  the *region*; narrow the ranges around it and sweep again to close the gap.
  Report the verified number and, if the user wants better, do the second pass.

Low `r_squared` means the response is not quadratic over that range — usually
too wide a range, or a discontinuity where ions start being lost.

## Interpreting results

`simion_fly` returns per-ion records and metrics. `hit_electrode=1` means the
ion struck an electrode; ions that simply left the array have `hit_electrode=0`.
A high `electrode_loss_fraction` means the beam is being clipped, which is a
geometry or voltage problem, not a numerical one.

Both are captured: results come from a `segment.other_actions` recorder, so
ions leaving the array boundary appear too. (A `segment.terminate` recorder,
the obvious approach, silently drops them — it only fires on electrode hits.)

## Known constraints

- **Workbenches (`.iob`) cannot be created headlessly.** SIMION only builds them
  in its GUI. Tools can generate and refine `.pa` arrays from `.gem` geometry,
  but a new workbench needs the user to make it. If they need one, tell them to
  create it in the SIMION GUI rather than attempting a workaround.
- **`simion_fastadj` does not control a fly.** It writes voltages into a `.pa0`,
  but loading a workbench re-applies the potentials stored in its `.iob`, which
  overrides them. Use `simion_fly(voltages=...)` for simulations.
- **`simion.wb` Lua objects are C userdata**, so `pairs()` over them errors.
  Probe documented fields with `pcall` in `simion_run_lua`.
- A workbench program cannot be supplied via `--lua @file`; SIMION only runs one
  named after the `.iob`.

See `references/mechanics.md` for the underlying CLI and Lua details, and
`references/tuning-workflow.md` for a worked tuning example.
