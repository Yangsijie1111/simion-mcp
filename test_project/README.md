# Test fixture

`lens.gem` is the whole distributable fixture: a three-element einzel lens with
cylindrical symmetry, named electrodes, and a 91 × 20 grid.

The binary half of the fixture — `lens.iob` (workbench), `lens.fly` (particle
definition) and the refined `lens.pa*` arrays — is **not** in this repository.
The arrays are regenerable, and the workbench used during development was
derived from a file in the SIMION distribution, which is not ours to
redistribute. Rebuild it yourself before running `validate.py`.

## Rebuilding

1. **Arrays** — headless, from the geometry in this directory:

   ```bash
   simion.exe --nogui --noprompt gem2pa lens.gem lens.pa#
   simion.exe --nogui --noprompt refine lens.pa#
   ```

   This produces `lens.pa0` plus `lens.pa1`–`lens.pa3`.

2. **Workbench** — GUI only, since SIMION cannot create an `.iob` headlessly.
   Open SIMION, create a new workbench, place `lens.pa0` as its single
   instance, and save it as `lens.iob` in this directory.

3. **Particles** — define a beam in the workbench's Particles view and save it
   with the workbench. `validate.py` does not reference the particle file
   directly; it comes in through the `.iob`.

## About the numbers in validate.py

Assertions that depend only on the geometry and the tool contracts (electrode
names, array production, ion capture, overwrite safety, sweep sizes) hold for
any rebuild.

Assertions carrying absolute physical values — the 0.853 mm focused spot, the
3.162 mm unenergised spot, R² = 0.977, and the ion count — were measured with
the original beam. A rebuilt fixture with a different beam definition will
shift them, so treat those as reference figures rather than as a contract.
`EXIT_PLANE_MM` and `FOCUS_FACTORS` at the top of the script are the knobs to
adjust.
