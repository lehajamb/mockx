# SORX hardware — single-board smart order router appliance (MVP)

Custom carrier board around a Toradex Verdin iMX8M Plus SoM. Hardware design only; no boards exist yet and the OS image is out of scope.

## Status: checkpoint 1 (2026-11-16)

Done: requirements, SoM comparison and pick, block diagram, power budget.
Waiting for sign-off before schematics.

| Path | Contents | State |
|---|---|---|
| `docs/requirements.md` | Requirements derived from the MockX feed and SOR examples | Done |
| `docs/som-comparison.md` | SoM comparison and justification | Done |
| `docs/block-diagram.md`, `docs/block-diagram.svg` | Plane-split block diagram, port map | Done |
| `docs/power-budget.md` | Per-rail budget vs. the USB-C PD contract | Done |
| `tools/` | Scripts that generate the diagram and the budget tables | Done |
| `hw/` | KiCad project, schematics PDF, stackup, Gerbers, drill, pick-and-place, assembly drawings | Not started |
| `mech/` | Outline DXF, board STEP, enclosure STEP/STL/3MF, rack ears | Not started |
| `bom/` | BOM CSV with distributor part numbers, second sources, cost roll-up | Not started |
| `docs/` (rest) | Thermal estimate, bring-up checklist, manufacturing test plan, ERC/DRC waivers, stock check | Not started |

## Status (2026-11-16)

Not ready for fabrication. Schematic (ERC clean), BOM, enclosure and documents are complete. The PCB layout is exported but has unconnected items: see `docs/verification-report.md` and `HANDOFF.md`.
