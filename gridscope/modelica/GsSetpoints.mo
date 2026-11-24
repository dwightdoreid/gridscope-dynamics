within Dynawo.Electrical.Controls.Machines;

model GsSetpoints "GridScope: steps on a machine's mechanical power reference and voltage reference (the 'Generator ΔP setpoint' and 'AVR setpoint step' events)"
  /* pypowsybl sends these events only to models it knows as controllable, which a composed machine is not.
     The steps are parameters here instead. The power reference feeds the governor's PmRefPu input. The
     voltage step is applied where the AVR measures the stator voltage: the AVR sees UsPu - deltaUsRefPu,
     which acts on its error exactly as raising the reference by deltaUsRefPu. */
  parameter Types.ActivePowerPu Pm0Pu "Initial mechanical power in pu (base PNomTurb), from the machine's initialisation";
  parameter Real PmRefScale = 1 "The governor's reference input starts at PmRefScale * Pm0Pu (TGOV1: its droop R)";
  parameter Types.Time tPmStep = 1e9 "Time of the mechanical power reference step in s";
  parameter Types.ActivePowerPu deltaPmRefPu = 0 "Mechanical power reference step in pu (base PNomTurb)";
  parameter Types.Time tUsStep = 1e9 "Time of the voltage reference step in s";
  parameter Types.VoltageModulePu deltaUsRefPu = 0 "Voltage reference step in pu (base UNom)";

  Modelica.Blocks.Interfaces.RealOutput PmRefPu(start = PmRefScale * Pm0Pu) "Mechanical power reference in pu (base PNomTurb)";
  Modelica.Blocks.Interfaces.RealInput UsPu(start = 1) "Measured stator voltage in pu (base UNom)";
  Modelica.Blocks.Interfaces.RealOutput UsMeasPu(start = 1) "Stator voltage passed to the AVR in pu (base UNom)";

protected
  discrete Real dPm(start = 0);
  discrete Real dUs(start = 0);

equation
  when time >= tPmStep then
    dPm = deltaPmRefPu;
  end when;
  when time >= tUsStep then
    dUs = deltaUsRefPu;
  end when;
  PmRefPu = PmRefScale * (Pm0Pu + dPm);
  UsMeasPu = UsPu - dUs;
end GsSetpoints;
