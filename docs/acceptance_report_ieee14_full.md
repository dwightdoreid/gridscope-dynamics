# GridScope Dynamics acceptance — IEEE 14-Bus

126 of 126 checks passed in 1.3 min (full set).

| Id | Area | Check | Result | Observed |
|---|---|---|---|---|
| A0 | Install | Dynawo and the C++ tool chain are found; GridScope protection models are compiled or compile | PASS | Dynawo /tmp/dynawo_dl/dynawo; 4 models compiled |
| A1 | Case | Load IEEE 14-bus: 14 buses, 20 branches, 5 generators, 259 MW load, no validation issues | PASS | 14 buses, 16 lines, 4 transformers, 5 generators, 259.0 MW |
| A2 | Case | Save/open round trip keeps machine models, events (incl. disabled), protection and study settings | PASS | identical after save → open |
| B1 | Load flow | AC load flow matches MATPOWER case14: total generation 272.39 MW, losses 13.393 MW | PASS | 5 iterations, gen 272.39 MW, losses 13.393 MW |
| B2 | Load flow | Bus voltages and angles match MATPOWER case14 (bus 14: 1.036 pu, −16.03°) | PASS | bus 14 1.0355 pu -16.03°; bus 3 -12.73° |
| B3 | Load flow | Generator results table: outputs add up to total generation; slack G1 = 232.39 MW | PASS | sum 272.39 MW, G1 232.39 MW; limit flags: G1 Q < Qmin |
| B4 | Load flow | Changing a generator schedule moves the slack: G2 +20 MW → G1 about −21 MW (−20 MW plus the change in losses) | PASS | G1 changed by -21.03 MW |
| B5 | Load flow | DC load flow converges; flows balance the 259 MW load | PASS | 20 branch flows; angles from -17.19° |
| B6 | Contingency | DC N-1: 20 outages; 7-8 islands bus 8; the rest give thermal violations (line 1-2 and 1-5 at 168 %) | PASS | {'Violation': 19, 'Islanding': 1} |
| B7 | Contingency | AC N-1: 20 outages solved (one non-converged case flagged, not crashed) | PASS | {'Violation': 18, 'Did not converge': 1, 'Islanding': 1} |
| B8 | OPF | DC OPF: optimal, Σ Pg = 259 MW (lossless), uniform LMP (no congestion), cost 5314.16 | PASS | cost 5314.16, LMP 21.036 |
| B9 | Short circuit | IEC 60909 max, 3-phase at all buses: largest Ik″ 41.78 kA; ip = κ·√2·Ik″ at every bus | PASS | max Ik″ 41.78 kA, max ip 108.53 kA, max 1650 MVA |
| B10 | Short circuit | IEC min case gives lower currents than max at every bus | PASS | min/max ratio 0.831–0.863 |
| B11 | Short circuit | SLG, LL and LLG faults and the ANSI method run at all buses; LL ≈ 0.866 × 3-phase | PASS | slg max 51.80 kA; ll max 36.19 kA; llg max 50.04 kA; ANSI ok |
| C1 | Simulation | Flat start, 10 s: no drift (ΔP < 0.5 MW, ΔV < 0.001 pu); initial P equals the load flow | PASS | ΔP 0.004 MW, ΔV 0.00000 pu, P(0) vs LF 0.002 MW |
| C2 | Simulation | Bolted fault at bus 4 for 100 ms: bus 4 voltage ≈ 0 during the fault; system stable | PASS | V4 0.001 pu during fault; Stable: max rotor-angle separation 74°, TSI 66% |
| C3 | Simulation | Fault with reactance (Value = 0.05 pu): retained voltage > 0, smaller swing than bolted | PASS | V4 0.39 pu; swing 71° vs 74° bolted |
| C4 | Simulation | Fault at bus 4 cleared by tripping line 4-5 after 100 ms: stable | PASS | Stable: max rotor-angle separation 75°, TSI 65% |
| C5 | Simulation | Line trip alone (2-3), and one end only (side ONE): both run; one-end trip leaves the line charging | PASS | both ends: V3 0.9855; side ONE: V3 0.9883 |
| C6 | Simulation | Generator trip G2 (40 MW): frequency dips (nadir ≈ 49.46 Hz), G2 excluded, stable | PASS | f min 49.458 Hz; Stable: max rotor-angle separation 68°, TSI 68% —  |
| C7 | Simulation | Load step +0.2 pu (20 MW) at bus 9: frequency falls and settles below 50 Hz | PASS | f min 49.926 Hz, final 49.926 Hz |
| C8 | Simulation | Load step ΔQ at bus 9: voltage at bus 9 falls | PASS | ΔV9 -0.0039 pu |
| C9 | Simulation | Generator ΔP setpoint +0.1 pu on G2: G2 output rises by about 10 MW | PASS | ΔP G2 9.84 MW |
| C10 | Simulation | AVR setpoint step +0.02 pu on G2: G2 reactive output rises, bus 2 voltage rises | PASS | ΔQ G2 17.7 Mvar, ΔV2 0.0080 pu |
| C11 | Simulation | Line fault at 30 % of line 4-5 (line opens when it clears): stable, fault point reported | PASS | Stable: max rotor-angle separation 75°, TSI 65% |
| C12 | Simulation | Islanding: tripping transformer 7-8 islands bus 8; G8 excluded from the verdict | PASS | Stable: max rotor-angle separation 68°, TSI 68% — bus(es) 8 islanded; generator(s) G8 disc |
| C13 | Simulation | Long fault (0.5 s at bus 2) loses synchronism → UNSTABLE | PASS | Loss of synchronism: rotor-angle separation reached 3401° (> 180°) at t = 5.00 s |
| C14 | Simulation | Disabled events (On unticked) are kept in the table but not simulated | PASS | 1 of 2 events run; both saved |
| C15 | Simulation | Invalid events are rejected with a message (unknown bus, line fault at 0 %, branch fault on a transformer) | PASS | 3 messages |
| C16 | Simulation | IDA solver: flat start and a load step run to the end | PASS | Stable: max rotor-angle separation 68°, TSI 68% |
| C17 | Simulation | Study settings: 60 Hz nominal frequency is used in the frequency results | PASS | f min 59.424 Hz |
| D1 | CCT | Critical clearing time, bus 2, no trip: bracket confirmed by independent runs either side | PASS | Critical clearing time between 388 and 395 ms. |
| D2 | CCT | Critical clearing time, bus 2 cleared by tripping line 2-4 | PASS | Critical clearing time between 380 and 388 ms. |
| D3 | Screening | Dynamic N-1 screening of all 20 branches (100 ms fault at the from end + trip) | PASS | 20 contingencies; columns: branch, fault_bus, verdict, max_angle_sep, TSI, min_post_voltage |
| E1.01 | Library model | GeneratorSynchronousFourWindings: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 78°, TSI 64% |
| E1.02 | Library model | GeneratorSynchronousFourWindingsGovSteam1St4bPss2b: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 83°, TSI 63% |
| E1.03 | Library model | GeneratorSynchronousFourWindingsGoverPropVRPropInt: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E1.04 | Library model | GeneratorSynchronousFourWindingsPmConstSt5bPss2b: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 75°, TSI 65% |
| E1.05 | Library model | GeneratorSynchronousFourWindingsPmConstSt6bPss3b: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E1.06 | Library model | GeneratorSynchronousFourWindingsPmConstSt6cPss6c: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E1.07 | Library model | GeneratorSynchronousFourWindingsPmConstSt9cPss2c: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E1.08 | Library model | GeneratorSynchronousFourWindingsPmConstVRNordic: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E1.09 | Library model | GeneratorSynchronousFourWindingsProportionalRegulations: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 75°, TSI 65% |
| E1.10 | Library model | GeneratorSynchronousFourWindingsTGov1SexsPss2a: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E1.11 | Library model | GeneratorSynchronousFourWindingsVRKundur: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 74°, TSI 66% |
| E1.12 | Library model | GeneratorSynchronousFourWindingsVRKundurPssKundur: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 74°, TSI 66% |
| E1.13 | Library model | GeneratorSynchronousThreeWindings: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 82°, TSI 63% |
| E1.14 | Library model | GeneratorSynchronousThreeWindingsGoverNordicVRNordic: flat start and fault + trip | PASS | flat ΔP 0.03 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E1.15 | Library model | GeneratorSynchronousThreeWindingsGoverPropVRPropInt: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 88°, TSI 61% |
| E1.16 | Library model | GeneratorSynchronousThreeWindingsPmConstVRNordic: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E1.17 | Library model | GeneratorSynchronousThreeWindingsProportionalRegulations: flat start and fault + trip | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E2.00 | Composed model | GAST + IEEET1 + PSS2A (any governor × AVR × PSS): flat start and fault | PASS | GsSync4W_Gast_IEEET1_Pss2a: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E2.G01 | Composed model | Governor Proportional (simplified) + SEXS | PASS | GsSync4W_GoverProportional_SEXS_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.G02 | Composed model | Governor GAST (gas turbine) + SEXS | PASS | GsSync4W_Gast_SEXS_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.G03 | Composed model | Governor GOVCT2 (gas turbine) + SEXS | PASS | GsSync4W_GovCt2_SEXS_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.G04 | Composed model | Governor GOVSTEAM1 (IEEE steam) + SEXS | PASS | GsSync4W_GovSteam1_SEXS_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.G05 | Composed model | Governor HYGOV (hydro) + SEXS | PASS | GsSync4W_HyGov_SEXS_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.G06 | Composed model | Governor IEEEG1 (IEEE steam) + SEXS | PASS | GsSync4W_IEEEG1_SEXS_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.G07 | Composed model | Governor IEEEG2 (IEEE hydro) + SEXS | PASS | GsSync4W_IEEEG2_SEXS_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 83°, TSI 63% |
| E2.G08 | Composed model | Governor Nordic governor + SEXS | PASS | GsSync4W_GoverNordic_SEXS_None: flat ΔP 0.02 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.G09 | Composed model | Governor TGOV1 (steam) + SEXS | PASS | GsSync4W_TGov1_SEXS_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.A01 | Composed model | TGOV1 + AVR Proportional (simplified) | PASS | GsSync4W_TGov1_VRProportional_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.A02 | Composed model | TGOV1 + AVR AC1C (IEEE) | PASS | GsSync4W_TGov1_Ac1c_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, UNSTABLE with its example parameters: Loss of synchronism: rotor-angle separation reached 16360° ( |
| E2.A03 | Composed model | TGOV1 + AVR BBSEX1 (static) | PASS | GsSync4W_TGov1_BbSex1_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.A04 | Composed model | TGOV1 + AVR DC1A (IEEE) | PASS | GsSync4W_TGov1_Dc1a_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E2.A05 | Composed model | TGOV1 + AVR DC1C (IEEE) | PASS | GsSync4W_TGov1_Dc1c_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E2.A06 | Composed model | TGOV1 + AVR IEEET1 (IEEE DC) | PASS | GsSync4W_TGov1_IEEET1_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E2.A07 | Composed model | TGOV1 + AVR IEEEX2 (IEEE DC) | PASS | GsSync4W_TGov1_IEEEX2_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E2.A08 | Composed model | TGOV1 + AVR IEEEX2A (IEEE DC) | PASS | GsSync4W_TGov1_IEEX2A_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, UNSTABLE with its example parameters: Loss of synchronism: rotor-angle separation reached 4572° (> |
| E2.A09 | Composed model | TGOV1 + AVR Kundur AVR | PASS | GsSync4W_TGov1_VRKundur_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 74°, TSI 66% |
| E2.A10 | Composed model | TGOV1 + AVR Nordic AVR | PASS | GsSync4W_TGov1_VRNordic_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.A11 | Composed model | TGOV1 + AVR PI (simplified) | PASS | GsSync4W_TGov1_VRProportionalIntegral_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.A12 | Composed model | TGOV1 + AVR SCRX (bus-fed static) | PASS | GsSync4W_TGov1_SCRX_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E2.A13 | Composed model | TGOV1 + AVR SEXS (simplified exciter) | PASS | GsSync4W_TGov1_SEXS_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.A14 | Composed model | TGOV1 + AVR ST4C (IEEE) | PASS | GsSync4W_TGov1_St4c_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.A15 | Composed model | TGOV1 + AVR ST5B (IEEE) | PASS | GsSync4W_TGov1_St5b_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 75°, TSI 65% |
| E2.A16 | Composed model | TGOV1 + AVR ST5C (IEEE) | PASS | GsSync4W_TGov1_St5c_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 75°, TSI 65% |
| E2.A17 | Composed model | TGOV1 + AVR ST6B (IEEE) | PASS | GsSync4W_TGov1_St6b_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E2.A18 | Composed model | TGOV1 + AVR ST6C (IEEE) | PASS | GsSync4W_TGov1_St6c_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 77°, TSI 65% |
| E2.A19 | Composed model | TGOV1 + AVR ST9C (IEEE) | PASS | GsSync4W_TGov1_St9c_None: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.P01 | Composed model | TGOV1 + SEXS + PSS Kundur PSS | PASS | GsSync4W_TGov1_SEXS_PssKundur: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.P02 | Composed model | TGOV1 + SEXS + PSS PSS1A (power input) | PASS | GsSync4W_TGov1_SEXS_Pss1aPGen: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.P03 | Composed model | TGOV1 + SEXS + PSS PSS1A (speed input) | PASS | GsSync4W_TGov1_SEXS_Pss1aOmega: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.P04 | Composed model | TGOV1 + SEXS + PSS PSS2A (IEEE) | PASS | GeneratorSynchronousFourWindingsTGov1SexsPss2a: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.P05 | Composed model | TGOV1 + SEXS + PSS PSS2B (IEEE) | PASS | GsSync4W_TGov1_SEXS_Pss2b: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.P06 | Composed model | TGOV1 + SEXS + PSS PSS2C (IEEE) | PASS | GsSync4W_TGov1_SEXS_Pss2c: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.P07 | Composed model | TGOV1 + SEXS + PSS PSS3B (IEEE) | PASS | GsSync4W_TGov1_SEXS_Pss3b: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.P08 | Composed model | TGOV1 + SEXS + PSS PSS3C (IEEE) | PASS | GsSync4W_TGov1_SEXS_Pss3c: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E2.P09 | Composed model | TGOV1 + SEXS + PSS PSS6C (IEEE) | PASS | GsSync4W_TGov1_SEXS_Pss6c: flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 76°, TSI 65% |
| E3.01 | Converter source | Solar PV: PhotovoltaicsWeccCurrentSourceB at G2 — starts at 40 MW, rides through a fault | PASS | P(0) 40.00 MW, flat ΔP 0.08 MW; fault: stable |
| E3.02 | Converter source | Wind (type 4): WTG4AWeccCurrentSource at G2 — starts at 40 MW, rides through a fault | PASS | P(0) 40.00 MW, flat ΔP 0.08 MW; fault: stable |
| E3.03 | Converter source | Wind (type 4): WTG4BWeccCurrentSource at G2 — starts at 40 MW, rides through a fault | PASS | P(0) 40.00 MW, flat ΔP 0.08 MW; fault: stable |
| E3.04 | Converter source | Wind (type 4): WT4AWeccCurrentSource at G2 — starts at 40 MW, rides through a fault | PASS | P(0) 40.00 MW, flat ΔP 0.08 MW; fault: stable |
| E3.05 | Converter source | Wind (type 4): WT4BWeccCurrentSource at G2 — starts at 40 MW, rides through a fault | PASS | P(0) 40.00 MW, flat ΔP 0.08 MW; fault: stable |
| E3.06 | Converter source | Battery storage: BESScbWeccCurrentSource at G2 — starts at 40 MW, rides through a fault | PASS | P(0) 40.00 MW, flat ΔP 0.00 MW; fault: stable |
| E3.07 | Converter source | Battery storage: BESScbWeccCurrentSourceNoPlantControl at G2 — starts at 40 MW, rides through a fault | PASS | P(0) 40.00 MW, flat ΔP 0.00 MW; fault: stable |
| E3.08 | Converter source | Grid-forming converter: GridFormingConverterMatchingControl at G2 — starts at 40 MW, rides through a fault | PASS | P(0) 39.99 MW, flat ΔP 0.01 MW; fault: stable |
| E3.09 | Converter source | Grid-forming converter: GridFormingConverterDispatchableVirtualOscillatorControl at G2 — starts at 40 MW, rides through a fault | PASS | P(0) 39.99 MW, flat ΔP 0.03 MW; fault: stable |
| E4.01 | Load model | All loads LoadAlphaBeta: flat start and fault | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 74°, TSI 66% |
| E4.02 | Load model | All loads LoadAlphaBetaRestorative: flat start and fault | PASS | flat ΔP 0.20 MW ΔV 0.0002 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 75°, TSI 65% |
| E4.03 | Load model | All loads LoadAlphaBetaRestorativeLimitsRecalc: flat start and fault | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 75°, TSI 65% |
| E4.04 | Load model | All loads LoadAlphaBetaTwoMotorFifthOrder: flat start and fault | PASS | flat ΔP 0.00 MW ΔV 0.0000 pu; fault + trip runs to the end, STABLE: Stable: max rotor-angle separation 75°, TSI 66% |
| E5 | Load model | Per-bus load model override reaches Dynawo (bus 9 restorative, rest default) | PASS | ['LoadAlphaBeta', 'LoadAlphaBetaRestorative'] |
| E6 | Parameters | Edited parameter reaches Dynawo: halving TGOV1 droop R halves the frequency drop | PASS | Δf 0.030 Hz → 0.014 Hz (ratio 0.49) |
| E7 | Parameters | PSS adds damping: Kundur AVR + PSS swings decay faster than without PSS | PASS | late swing 0.47° → 0.12° with PSS |
| E8 | Parameters | Set-point steps reach every machine type: library (proportional), protected, and composed GAST/IEEET1 | PASS | library: ΔV2 +0.0080 pu, ΔP +9.9 MW; protected: ΔV2 +0.0080 pu, ΔP +9.9 MW; composed: ΔV2 +0.0187 pu, ΔP +11.4 MW |
| E9 | Parameters | A library model that cannot take set-point steps (TGov1/SEXS/PSS2A) says so instead of ignoring them silently | PASS | AVR setpoint step on G2 not applied: pypowsybl sends set-point steps only to Dynawo's proportional-regulation  |
| F1 | Protection | No protection set: the network model is unchanged (no protection notes, no operations) | PASS | no operations |
| F2 | Protection | Generator under-voltage: 2 s fault at bus 3 trips G3 at 2.5 s (U < 0.7 for 1.5 s) | PASS | Generator G3 under-voltage tripped @ 2.5s |
| F3 | Protection | Generator over-voltage: AVR step +0.1 pu on G6 raises it to 1.135 pu and trips it (U > 1.12 for 1 s) | PASS | Generator G6 over-voltage tripped @ 3.03s |
| F4 | Protection | Generator over/under speed: losing G1 (232 MW) trips G2 on under-frequency (f < 49.5 Hz for 0.5 s) | PASS | Generator G2 over/under speed tripped @ 1.69s |
| F5 | Protection | Generator loss of synchronism: 0.5 s fault at bus 2 — slipping machine(s) tripped | PASS | Generator G3 loss of synchronism tripped @ 1.71s; Generator G6 loss of synchronism tripped @ 1.77s; Generator G1 loss of synchronism tripped @ 1.79s; Generator G8 loss of synchronism tripped @ 1.79s → Stable: max rotor-angle separation 0°, TSI 100% —  |
| F6 | Protection | Distance: 0.5 s busbar fault at bus 4 cleared in zone 2 by the remote ends at 1.36 s | PASS | Line 2-4 distance (end 1) opened @ 1.36s; Line 3-4 distance (end 1) opened @ 1.36s; Line 4-5 distance (end 2) opened @ 1.36s |
| F7 | Protection | Distance zones on a line fault (Side NONE): at 10 % end 1 zone 1 @1.06 s, end 2 zone 2 @1.36 s; at 50 % both zone 1 | PASS | 10 %: Line 4-5 distance (end 1) opened @ 1.06s; Line 4-5 distance (end 2) opened @ 1.36s / 50 %: Line 4-5 distance (end 1) opened @ 1.06s; Line 4-5 distance (end 2) opened @ 1.06s |
| F8 | Protection | Line overcurrent: line 1-2 (122 % loaded) with pick-up 110 % trips at its 1 s delay | PASS | Line 1-2 overcurrent opened @ 1.01s |
| F9 | Protection | Transformer overcurrent: 5-6 (70 % loaded) with pick-up 50 % trips at its 2 s delay | PASS | Transformer 5-6 overcurrent opened @ 2s |
| F10 | Protection | No pick-up when loading is below the setting (all transformers at 150 %): no operation, no rating note | PASS | no operations; ratings used |
| F11 | Protection | Missing rating: pick-up falls back to pre-disturbance loading, with a note | PASS | Transformer 5-6: no rating (Rate MVA) — overcurrent pick-up taken as 150 % of the pre-disturbance lo |
| F12 | Protection | Under-frequency load shedding: losing G2 sheds in steps at the loads (f1 49.9, f2 49.8, f3 49.7 Hz) | PASS | 11 operations, first: Load at bus 2 10 % shed @ 1.69 s |
| F13 | Protection | Under-voltage load shedding: 1.5 s fault at bus 9 sheds load at buses near the fault | PASS | Load at bus 4 load shedding 20 % shed @ 2.01s; Load at bus 5 load shedding 20 % shed @ 2.01s; Load at bus 6 load shedding 20 % shed @ 2.01s; Load at b |
| F14 | Protection | Line protection + UFLS together: load shedding measures frequency locally and still operates | PASS | 11 operations, first @ 1.84 s |
| F15 | Protection | Line protection with a converter source: run goes ahead without line protection and says why | PASS | Line protection not applied: it needs every branch and bus modelled in Modelica, and converter models (G2) can |
| F16 | Protection | All protection on (every generator, line, transformer, load): 100 ms fault at bus 4 runs, no spurious trips | PASS | Stable: max rotor-angle separation 74°, TSI 66%; operations: none |
| F17 | Protection | All protection on: CCT at bus 1 completes (the protection may isolate the faulted bus) | PASS | Critical clearing time between 178 and 185 ms. |
| F18 | Protection | Protection settings are validated (negative delay rejected) | PASS | Generator G1: t UV s must not be negative. |
| G2 | App | Every page of the app renders, and the load flow, N-1, OPF, short-circuit and simulation run from the UI | PASS | 13 passed in 11.41s |
