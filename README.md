# simion-mcp

MCP server for [SIMION](https://simion.com), the ion/electron optics simulator,
plus a companion Claude skill that encodes how to drive it well.

SIMION exposes no external control API, so everything here wraps its headless
CLI (`simion.exe --nogui --noprompt`) plus a generated workbench Lua program.
Design borrows from two existing SIMION automation projects:
[simPyon](https://github.com/jonbowr/simPyon) (electrode names live in `.gem`
comments; filter results to a measurement zone) and
[SIMIONtuneR](https://github.com/pasturm/SIMIONtuneR) (Box-Behnken designs and
second-order response surfaces for voltage tuning).

Requires a licensed SIMION installation. This project ships no SIMION code or
data files.

## Install

```bash
pip install -r requirements.txt
```

Set `SIMION_HOME` to the directory containing `simion.exe`, then register the
server. For Claude Code:

```bash
claude mcp add simion --env SIMION_HOME="<path to SIMION>" -- python /path/to/simion-mcp/server.py
```

Any MCP client works — the server speaks stdio and has no client-specific code.

No numeric dependencies: the DoE and response-surface maths are pure stdlib.

## Companion skill

`skill/simion-ion-optics/` is a Claude skill covering the workflow the tools
support — inspect before guessing electrode numbers, work on a copy, choose a
measurement zone from an actual fly, and read a response-surface fit honestly.
`skill/simion-ion-optics/references/mechanics.md` documents the underlying CLI
and Lua behaviour, including three failure modes that produce no error.

To use it with Claude Code, symlink it into your skills directory:

```bash
ln -s /path/to/simion-mcp/skill/simion-ion-optics ~/.claude/skills/simion-ion-optics
```

## Tools

| Tool | Purpose |
|---|---|
| `simion_check_install` | Verify `SIMION_HOME` |
| `simion_list_examples` | List bundled example workbenches |
| `simion_prepare_workspace` | Copy a workbench to a scratch dir before simulating |
| `simion_inspect_workbench` | Electrode numbers **and names**, geometry, existing Lua program |
| `simion_gem2pa` | Compile `.gem` geometry into a potential array |
| `simion_refine` | Solve the array; produces the fast-adjust `.paN` set |
| `simion_fastadj` | Write voltages into a `.pa0` (static prep only — see below) |
| `simion_fly` | Fly particles with driven voltages; returns per-ion records + metrics |
| `simion_sweep` | Designed voltage sweep (box_behnken / factorial / grid) |
| `simion_fit_response_surface` | Fit a quadratic surface and recommend an optimum |
| `simion_run_lua` | Escape hatch for arbitrary Lua |

## How voltage control works

Electrode voltages can only be driven from a workbench Lua program named after
the `.iob`. `simion_fly` generates one, marks it with a sentinel comment, and
refuses to overwrite a hand-written program unless `allow_overwrite_lua=true`
(keeping a timestamped `.bak` either way). Prefer `simion_prepare_workspace`
so the user's originals are never touched.

Two approaches that look correct but silently fail, and are therefore *not*
used: setting `adj_electNN` directly via `--lua` (discarded before the fly),
and `fastadj` on the `.pa0` (the `.iob`'s stored potentials override it at load
time). Details in `skill/simion-ion-optics/references/mechanics.md`.

## Result capture

Per-ion results come from a `segment.other_actions` recorder written to CSV.
The obvious alternative, `segment.terminate`, only fires for ions that strike an
electrode — ions leaving the array boundary vanish from the record without any
error. In the test lens that silently dropped 5 of 11 ions.

Metrics include `transmission`, `rms_radius_mm`, `tof_usec` statistics,
`tof_resolution`, and `electrode_loss_fraction`. Pass `x_min`/`x_max`/`r_min`/
`r_max` to scope them to a measurement plane.

## Known constraints

- **Workbenches (`.iob`) cannot be created headlessly.** SIMION only builds them
  in its GUI; `simion.wb` offers `load`/`save` but no `new`, and `load` accepts
  only `.iob`. Arrays can be generated from `.gem`, but a new workbench cannot.
- A quadratic response surface over-extrapolates a sharp optimum. Treat
  `simion_fit_response_surface` output as a region to verify with `simion_fly`,
  then narrow the ranges and re-sweep.
- `.fly`, `.iob`, and `.pa*` are binary. `.gem` and `.fly2` are text.

## Test project and validation

`test_project/lens.gem` is a self-contained three-element einzel lens with named
electrodes. `test_project/validate.py` exercises every tool against a real
SIMION install and asserts on the results, so a regression in the CLI wrapping,
the Lua harness, the CSV capture, or the response-surface maths shows up as a
failing check rather than a plausible-looking number.

**The binary half of the fixture is not distributed.** Only the `.gem` source is
in the repo; see [test_project/README.md](test_project/README.md) for how to
rebuild the workbench before running the validation.

```bash
SIMION_HOME="<path to SIMION>" python test_project/validate.py
```

49/49 checks pass as of 2026-08-21 against SIMION 2024, covering electrode-name
parsing, geometry compilation and refining, complete ion capture (including
boundary-exiting ions), voltage control actually changing trajectories,
measurement-zone metrics, workbench-program overwrite safety, a 9-point sweep,
response-surface fitting (R² = 0.977), and verification that the tuned lens
focuses 3.7× tighter than the unenergised one (0.853 mm vs 3.162 mm).

## License

MIT — see [LICENSE](LICENSE). SIMION itself is commercial software from Adaptas
Solutions / Scientific Instrument Services and is not included or redistributed
here in any form.
