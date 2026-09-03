# SIMION control mechanics

Everything here was verified against SIMION 2024 on Windows. It documents *why*
the MCP tools work the way they do, which matters when something behaves oddly
or when you need `simion_run_lua` to go beyond the typed tools.

## Command line

SIMION's headless mode is `simion.exe --nogui --noprompt <command>`. Commands:

| Command | Syntax | Notes |
|---|---|---|
| `fly` | `fly [--disk=0\|1] [--remove-pas=N] [--refine-convergence=V] <file.iob>` | Requires a real `.iob`; a `.pa` is rejected with "IOB corrupt: Invalid workspace size" |
| `refine` | `refine [--resume=0\|1] [--convergence=V] <file.pa#>` | Produces `.pa0` plus one `.paN` per electrode |
| `gem2pa` | `gem2pa <file.gem> [<out.pa#>]` | Without an output name it writes `<name>.pa`, which `refine` will not pick up — always pass `<name>.pa#` |
| `fastadj` | `fastadj <file.pa0> "1=0,2=500"` | Comma-separated `n=v`; other separators are rejected |
| `--lua` | `--lua "x=1"` or `--lua "@script.lua"` | Repeatable; runs before the main command |

`--quiet` suppresses status lines.

Failures are reported as `error,...` lines on **stdout** while the process still
exits 0, so exit code alone is not a success test. `core.run_simion` checks both.

## Electrode names live in the .gem file

Nothing else exposes them. The convention, which simPyon established, is a
trailing comment on the declaration:

```
electrode(1); entrance_barrel
{ rotate_fill(360) { within { box(0,18, 28,19) } } }
```

`e(1)` is an accepted short form. Electrode 0 is implicit ground. Geometries
split across files via `include(...)` need those followed to see every
electrode — `core.parse_gem` does this recursively.

## Voltage control: what works and what silently does not

**Works.** A workbench program named `<workbench>.lua` sitting next to the
`.iob`, with a `fast_adjust` segment reading globals injected from the CLI:

```lua
simion.workbench_program()
function segment.fast_adjust()
  if MCP_V2 ~= nil then adj_elect02 = MCP_V2 end
end
```

```
simion.exe --nogui --noprompt --lua "MCP_V2=1200" fly --disk=0 lens.iob
```

**Does not work — setting `adj_electNN` directly from the CLI.**
`--lua "adj_elect02=1200"` runs before the fly and is discarded; SIMION
re-derives those variables per time step inside `fast_adjust`. It fails
silently, producing identical results with no error, which makes it an easy
mistake to believe.

**Does not work for flying — `fastadj` on the `.pa0`.** It genuinely rewrites
the array on disk (the file hash changes), but loading a workbench re-applies
the potentials stored in the `.iob`, overriding it. Useful for preparing a
standalone array; useless for controlling a simulation.

**Does not work — a workbench program loaded via `--lua @file.lua`.** SIMION
rejects any file containing `simion.workbench_program()` this way:
"You attempted to execute a special workbench program". The file must be named
after the `.iob`. This is why voltage control necessarily writes a file, and
why working in a copied workspace is the safe default.

Assign only the electrodes you intend to drive. Electrodes left unassigned in
`fast_adjust` keep the potentials stored in the workbench.

## Capturing results: use other_actions, not terminate

`segment.terminate` fires **only for ions that strike an electrode**. Ions that
leave the array boundary never trigger it and vanish from the record — in the
test lens, 11 ions flown produced 6 `terminate` calls, and nothing reported the
loss.

`segment.other_actions` fires every time step for every ion, so keeping the
latest state per `ion_number` and writing it in `terminate_run` captures all of
them. `terminate` is then useful only as a flag for "this one hit an electrode".

Readable in `other_actions`: `ion_number`, `ion_time_of_flight`, `ion_px_mm`,
`ion_py_mm`, `ion_pz_mm`, `ion_vx_mm`, `ion_vy_mm`, `ion_vz_mm`, `ion_mass`,
`ion_charge`. Kinetic energy via `simion.speed_to_ke(speed, ion_mass)`.
`ion_splat` is **not** readable in `terminate` — reading it raises
"Read access denied to ion_splat in this segment".

## File formats

- `.gem` — text geometry source. Readable and generatable.
- `.fly2` — text particle definitions (Lua-like `particles { standard_beam {...} }`).
  Generatable.
- `.fly`, `.iob`, `.pa*` — **binary**. Never hand-edit.

`.iob` stores the PA filename in a 12-byte space-padded field and the array
dimensions as nearby int32s. That is why a workbench cannot be retargeted at a
differently-sized array without the GUI.

## simion.wb from Lua

`simion.wb` is C userdata, not a table: `pairs()` over it raises
"bad argument #1 to 'pairs' (table expected, got userdata)". `simion.wb.load`
and `simion.wb.save` exist as functions, but there is no `new` or
`add_instance`, and `load` accepts only `.iob` files — hence no headless
workbench creation. Probe fields with `pcall` rather than enumerating.
