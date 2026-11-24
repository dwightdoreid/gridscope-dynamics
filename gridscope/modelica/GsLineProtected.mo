within Dynawo.Electrical.Lines;

model GsLineProtected "GridScope: AC line (PI model) with Dynawo distance protection at both ends and a definite-time overcurrent relay"
  /* Distance: Dynawo's two-zone quadrilateral relay at each end, measuring that end's voltage and power.
     Overcurrent: trips when the larger end current stays above IMaxPu for tOC.
     A relay trip opens the line (both ends) through its third switch-off signal. Switching events act on
     the branch state: 1 open, 2 closed, 3 only end 1 closed, 4 only end 2 closed (the open-ended line
     stays energised from the closed end, as in the network model). */
  import Modelica.Constants;
  import Modelica.ComplexMath;
  /* PI line: the equations of Dynawo 1.7.0's Line, which cannot be extended here because its 'state'
     variable clashes with the state connector pypowsybl drives for trip events. Switch-off signals:
     1 node disconnection, 2 switching events (state <> 2), 3 protection. */
  extends Dynawo.Electrical.Controls.Basics.SwitchOff.SwitchOffLogic(NbSwitchOffSignals = 3);
  Dynawo.Connectors.ACPower terminal1 "Connector used to connect the line to the grid (side 1)";
  Dynawo.Connectors.ACPower terminal2 "Connector used to connect the line to the grid (side 2)";
  parameter Types.PerUnit RPu "Resistance in pu (base SnRef)";
  parameter Types.PerUnit XPu "Reactance in pu (base SnRef)";
  parameter Types.PerUnit GPu "Half-conductance in pu (base SnRef)";
  parameter Types.PerUnit BPu "Half-susceptance in pu (base SnRef)";
  Types.ActivePowerPu P1Pu "Active power on side 1 in pu (base SnRef) (receptor convention)";
  Types.ReactivePowerPu Q1Pu "Reactive power on side 1 in pu (base SnRef) (receptor convention)";
  Types.ActivePowerPu P2Pu "Active power on side 2 in pu (base SnRef) (receptor convention)";
  Types.ReactivePowerPu Q2Pu "Reactive power on side 2 in pu (base SnRef) (receptor convention)";

  parameter Boolean DistanceOn = false "Distance protection in service";
  parameter Boolean Distance1On = true "Distance relay at end 1 present (false for the inner end of a line split at a fault point)";
  parameter Boolean Distance2On = true "Distance relay at end 2 present";
  parameter Types.PerUnit Z1RPu = 0.01 "Zone 1 resistive reach in pu (base UNom, SnRef)";
  parameter Types.PerUnit Z1XPu = 0.01 "Zone 1 reactive reach in pu (base UNom, SnRef)";
  parameter Types.Time tZ1 = 0 "Zone 1 time delay in s";
  parameter Types.PerUnit Z2RPu = 0.02 "Zone 2 resistive reach in pu (base UNom, SnRef)";
  parameter Types.PerUnit Z2XPu = 0.02 "Zone 2 reactive reach in pu (base UNom, SnRef)";
  parameter Types.Time tZ2 = 0.3 "Zone 2 time delay in s";
  parameter Types.Time CircuitBreakerTime = 0.06 "Circuit-breaker opening time in s";
  parameter Boolean OCOn = false "Overcurrent protection in service";
  parameter Types.CurrentModulePu IMaxPu = 10 "Overcurrent pick-up in pu (base UNom, SnRef)";
  parameter Types.Time tOC = 1 "Overcurrent time delay in s";
  parameter Types.PerUnit GEnd1Pu = 0 "Conductance to ground at end 1 in pu (base SnRef): a line split at a fault point keeps the point defined once both sections are open";
  parameter Types.PerUnit GEnd2Pu = 0 "Conductance to ground at end 2 in pu (base SnRef)";
  parameter Types.Time tMeasure = 0.001 "Relays see a neutral impedance until this time (initialisation) in s";

  Dynawo.Electrical.Controls.Protections.DistanceProtectionLineTwoZones distance1(LineSide = 1, tZone = {tZ1, tZ2}, RPu = if DistanceOn and Distance1On then {Z1RPu, Z2RPu} else {-1000, -1000}, XPu = if DistanceOn and Distance1On then {Z1XPu, Z2XPu} else {-1000, -1000}, TrippingZone = {true, true}, CircuitBreakerTime = CircuitBreakerTime);
  Dynawo.Electrical.Controls.Protections.DistanceProtectionLineTwoZones distance2(LineSide = 2, tZone = {tZ1, tZ2}, RPu = if DistanceOn and Distance2On then {Z1RPu, Z2RPu} else {-1000, -1000}, XPu = if DistanceOn and Distance2On then {Z1XPu, Z2XPu} else {-1000, -1000}, TrippingZone = {true, true}, CircuitBreakerTime = CircuitBreakerTime);
  Real I2MonitoredPu(start = 0) "Square of the larger of the two end currents in pu (base UNom, SnRef)";
  Boolean ocTripped(start = false) "Overcurrent relay has operated";
  discrete Types.Time tOCReached(start = Constants.inf) "Time the overcurrent pick-up was exceeded";
  Boolean protectionTripped(start = false) "Line opened by its protection";
  Integer tripCause(start = 0) "0 none, 1 distance at end 1, 2 distance at end 2, 3 overcurrent";
  input Integer state_value(start = 2) "Branch state set by switching events (pypowsybl connects it as line_state_value): 1 open, 2 closed, 3/4 only end 1/2 closed";
  Integer state(start = 2) "Connection state reported back to the network (2 = closed, 1 = open)";
  Types.ComplexCurrentPu i1Pu(re(start = 0), im(start = 0)) "Current into the line at end 1 (without the end conductance)";
  Types.ComplexCurrentPu i2Pu(re(start = 0), im(start = 0)) "Current into the line at end 2 (without the end conductance)";

protected
  parameter Types.ComplexImpedancePu ZPu(re = RPu, im = XPu) "Line impedance";
  parameter Types.ComplexAdmittancePu YPu(re = GPu, im = BPu) "Line half-admittance";

equation
  if running.value and state_value == 3 then
    // only end 1 closed: the line is energised from end 1 and open at end 2
    i1Pu = YPu * terminal1.V + YPu * terminal1.V / (Complex(1) + ZPu * YPu);
    i2Pu = Complex(0);
  elseif running.value and state_value == 4 then
    // only end 2 closed
    i2Pu = YPu * terminal2.V + YPu * terminal2.V / (Complex(1) + ZPu * YPu);
    i1Pu = Complex(0);
  elseif running.value then
    ZPu * (i2Pu - YPu * terminal2.V) = terminal2.V - terminal1.V;
    ZPu * (i1Pu - YPu * terminal1.V) = terminal1.V - terminal2.V;
  else
    i1Pu = Complex(0);
    i2Pu = Complex(0);
  end if;
  terminal1.i = i1Pu + Complex(GEnd1Pu) * terminal1.V;
  terminal2.i = i2Pu + Complex(GEnd2Pu) * terminal2.V;
  P1Pu = ComplexMath.real(terminal1.V * ComplexMath.conj(terminal1.i));
  Q1Pu = ComplexMath.imag(terminal1.V * ComplexMath.conj(terminal1.i));
  P2Pu = ComplexMath.real(terminal2.V * ComplexMath.conj(terminal2.i));
  Q2Pu = ComplexMath.imag(terminal2.V * ComplexMath.conj(terminal2.i));
  switchOffSignal2.value = state_value == 1;
  state = if not running.value then 1 elseif state_value == 3 or state_value == 4 then state_value else 2;
  I2MonitoredPu = max(terminal1.i.re ^ 2 + terminal1.i.im ^ 2, terminal2.i.re ^ 2 + terminal2.i.im ^ 2);
  // measurements (a neutral far-away impedance during initialisation and once the line is open)
  distance1.UMonitoredPu = if time < tMeasure or protectionTripped or not running.value or state_value == 4 then 1 else sqrt(terminal1.V.re ^ 2 + terminal1.V.im ^ 2 + 1e-12);
  distance1.PMonitoredPu = if time < tMeasure or protectionTripped or not running.value or state_value == 4 then 1 else P1Pu + 1e-9;
  distance1.QMonitoredPu = if time < tMeasure or protectionTripped or not running.value or state_value == 4 then 0 else Q1Pu;
  distance2.UMonitoredPu = if time < tMeasure or protectionTripped or not running.value or state_value == 3 then 1 else sqrt(terminal2.V.re ^ 2 + terminal2.V.im ^ 2 + 1e-12);
  distance2.PMonitoredPu = if time < tMeasure or protectionTripped or not running.value or state_value == 3 then 1 else P2Pu + 1e-9;
  distance2.QMonitoredPu = if time < tMeasure or protectionTripped or not running.value or state_value == 3 then 0 else Q2Pu;

  when time > tMeasure and I2MonitoredPu > IMaxPu ^ 2 and OCOn and not pre(ocTripped) then
    tOCReached = time;
  elsewhen I2MonitoredPu <= IMaxPu ^ 2 and pre(tOCReached) <> Constants.inf and not pre(ocTripped) then
    tOCReached = Constants.inf;
  end when;
  when time - tOCReached >= tOC then
    ocTripped = true;
  end when;

  protectionTripped = distance1.tripped[1] or distance1.tripped[2] or distance2.tripped[1] or distance2.tripped[2] or ocTripped;
  tripCause = if distance1.tripped[1] or distance1.tripped[2] then 1 elseif distance2.tripped[1] or distance2.tripped[2] then 2 elseif ocTripped then 3 else 0;
  switchOffSignal3.value = protectionTripped;
end GsLineProtected;
