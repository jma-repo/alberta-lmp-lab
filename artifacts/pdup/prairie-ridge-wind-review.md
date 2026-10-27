# PDUP model review: Prairie Ridge Wind (synthetic)

SYN-0001 · Stage 2 · 200 MW wind · POI Substation 901S 240 kV (fictional)

**Outcome: Resubmission required** (10 deficiencies, 5 clarifications)

## Summary

The review found that the submitted model does not reach a valid full-output power flow solution. It also identified errors in topology, transformer and line parameters, dynamic settings, and reactive capability. The model shows reactive capability shortfalls and main transformer loading above the rated level at the operating points. Resubmission is required.

_Summary drafted by: local LLM; findings below are rule-based._

## Deficiencies (must be corrected)

| # | Rule | Element | Finding | Required action |
|---|---|---|---|---|
| 1 | PF-01 | Prairie Ridge Wind (synthetic) | The AC power flow does not reach a valid solution at full output with the data as submitted. It solves to a valid operating point once GSU 2 kv_lv is set to 0.69. *Root cause isolated by re-solving with each suspected correction on its own.* | Correct the model so the case solves from a flat start. |
| 2 | PF-02 | Prairie Ridge Wind (synthetic) | At 197 MW the plant delivers +23 / -104 Mvar at the POI; 0.95 power factor needs +/-65 Mvar. | Add reactive capability (inverter MVA, capacitor or STATCOM) or justify the shortfall. |
| 3 | TOP-02 | GSU 2 | LV winding is 690 kV but bus G2 is 0.69 kV. *The winding voltage looks like it was entered in volts: 690 -> 0.69 kV.* | Correct the winding or bus voltage. |
| 4 | XF-01 | Main transformer | Impedance 5.56% on 180 MVA is outside 6-15% for a main transformer. *The value looks like it is on the 100 MVA system base: 5.56% x 180/100 = 10.0% on the transformer's own base, which is typical.* | Confirm the test-report impedance and the MVA base it is stated on. |
| 5 | LINE-01 | Collector 2 | X = 1.428 ohm/km, outside 0.08-0.25 ohm/km for 34.5 kV cable. *Likely ohms typed into the p.u. field: read that way it is 0.120 ohm/km, a normal value (0.09074 p.u. on the study base).* | Confirm X for the conductor and length, in p.u. on 100 MVA. |
| 6 | DYN-04 | WTG group 1 REECA1, WTG group 2 REECA1 | Imax = 0.9 p.u. cannot carry rated P (0.900) with the required Q (0.297): needs 0.948. | Raise Imax to the converter's current rating or justify the limit. |
| 7 | DYN-05 | WTG group 1 REPCA1, WTG group 2 REPCA1 | Ddn = 5 gives a 20% droop, outside 2-5%. | Set the droop to the plant's frequency-response setting (for 5%, Ddn = 20). |
| 8 | TOP-03 | Main transformer | The YNd1 main transformer has a delta low-voltage winding and no grounding transformer is modelled on MV. | Add the collector grounding transformer (impedance and connection) or confirm the grounding method. |
| 9 | LINE-05 | Gen-tie | Winter rating 400 MVA is below the summer rating 450 MVA; cooler winter air should allow more. *The summer and winter values appear to be swapped.* | Confirm the seasonal ratings. |
| 10 | TOP-01 | COL3 | Bus COL3 (34.5 kV) is not connected to the POI. | Connect the bus or remove it from the case. |

## Clarifications requested

| # | Rule | Element | Finding | Required action |
|---|---|---|---|---|
| 1 | GEN-03 | WTG group 1, WTG group 2 | Reactive range -30 to 30 Mvar is below +/-33 Mvar (0.33 x Pmax) at the terminals. | Provide the capability curve at full output. |
| 2 | DYN-03 | WTG group 1 REGCA1, WTG group 2 REGCA1 | Tg = 0.005 s is shorter than 4 integration steps (0.0167 s). | Set Tg to 0 to bypass it, or to at least 0.017 s. |
| 3 | PF-03 | Prairie Ridge Wind (synthetic) | At the required operating points: Main transformer loaded 112% (export), Main transformer loaded 112% (unity), Main transformer loaded 116% (absorb). | Review voltage control settings and equipment ratings. |
| 4 | SC-01 | Prairie Ridge Wind (synthetic) | Short-circuit ratio 2.8 (560 MVA / 200 MW) is below 3: a weak connection point. | Provide a validated EMT (PSCAD) model; an EMT study will be required. |
| 5 | ML-01 | Collector 1 | Each value passes its range check, but the combination is more unusual than every one of the 600 accepted 34.5 kV cable models: X high (+3.5 robust sd), R low (-3.0 robust sd), B low (-2.8 robust sd). | Confirm the conductor and cable data sheet values together. |

## Study results

- AC power flow: **no valid solution as submitted**; results below use the probable corrections and are provisional until the market participant confirms them.
- Reactive capability at the POI: +23.1 / -103.7 Mvar at 197.0 MW against +/-64.7 Mvar required.
- Short-circuit ratio at the POI: 2.8; collector fault level 11.6 kA (switchgear 25 kA).
- Most sensitive input for the POI reactive margin: Inverter reactive limit (-10% / +10%: -47.5 to -36.4 Mvar).

_Synthetic project and data, produced by an automated first-pass review; engineering judgement and AESO's PDUP checklist govern._
