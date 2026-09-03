# Worked tuning example

Focusing the three-element einzel lens in `simion-mcp/test_project`. Every
number below is a real result from that workbench, including the parts that did
not go as predicted.

## 1. Inspect

```
simion_inspect_workbench(iob_path=".../lens.iob")
```

```
electrodes: 1 entrance_barrel, 2 focus_ring, 3 exit_barrel
pa_define:  91,20,1, cylindrical,, electrostatic
workbench_program: absent
```

The names tell you which electrode does the work: the outer barrels hold the
beam potential, the centre ring focuses. So electrode 2 is the tuning knob.

## 2. Find the beam before choosing a measurement zone

```
simion_fly(iob_path=..., voltages={"2": 0})
```

Ion records come back with `x_mm ≈ 90` — the array is 91 grid units, so the
beam exits at x ≈ 90 mm. The exit plane therefore sits at `x_min = 85`, not at
some assumed value.

Getting this wrong is the most common failure: a zone beyond the array returns
`transmission = 0` and no spot size, which looks like a broken lens but is a
broken measurement.

## 3. Baseline

```
simion_fly(iob_path=..., voltages={"2": 0}, x_min=85)
```

```
transmission: 1.0    rms_radius_mm: 3.162
```

All 11 ions arrive, spread over a 3.2 mm spot. That is the number to beat.

## 4. Find the range the beam survives

A coarse scan of electrode 2 before committing to a design:

| V2 | transmission | rms_radius_mm |
|---|---|---|
| 0 | 1.0 | 3.162 |
| 100 | 1.0 | 2.724 |
| 200 | 1.0 | 0.489 |
| 250 | 1.0 | 7.394 |
| 300 | 0.0 | — |

Two things worth noticing. The focus minimum is near 200 V and it is *sharp* —
0.49 mm at 200 V, 7.39 mm only 50 V later. And above ~280 V the ring reflects
the beam entirely.

So the design must stay below that cliff. Fitting a surface across it would be
fitting through a discontinuity.

## 5. Sweep

```
simion_sweep(
  iob_path=...,
  factors=[[2, 120, 240], [3, -60, 60]],
  design="grid", levels=3, x_min=85,
)
```

Nine runs, all with `transmission = 1.0`, spot sizes spanning 0.621–7.306 mm.
Full transmission across the whole design region is what makes the response
fittable.

## 6. Fit

```
simion_fit_response_surface(runs=..., metric="rms_radius_mm", goal="min",
                            factors=[[2, 120, 240], [3, -60, 60]])
```

```
r_squared: 0.977   fit_quality: good
recommended_voltages: {"2": 174.0, "3": -60.0}
predicted_value: 0.069
factors_at_boundary: ["3"]
```

## 7. Verify — and read the gap

```
simion_fly(iob_path=..., voltages={"2": 174, "3": -60}, x_min=85)
```

```
rms_radius_mm: 0.853
```

Predicted 0.069 mm, achieved 0.853 mm. The model was optimistic by an order of
magnitude, and the honest result to report is **0.853 mm**.

This is not a defect. A quadratic surface cannot represent a minimum as sharp
as this lens's, so it extrapolates past what the optics can deliver. What the
fit did get right is the *region* — 174 V is close to the true optimum near
200 V, and 0.853 mm is a 3.7× improvement on the 3.162 mm baseline.

`factors_at_boundary: ["3"]` is the second signal: electrode 3 was pinned at
the edge of its range, so its optimum lies outside what was swept.

## 8. Second pass

Both signals point to the same next move — narrow around the optimum and widen
where it hit the wall:

```
simion_sweep(factors=[[2, 170, 220], [3, -150, -30]], design="grid",
             levels=5, x_min=85)
```

Refitting over the narrower range lets the quadratic describe the minimum
properly. Iterating this way is the method SIMIONtuneR uses; a single pass
locates the region, and successive passes close in on it.

Stop when the verified value stops improving — not when the predicted one
looks good.
