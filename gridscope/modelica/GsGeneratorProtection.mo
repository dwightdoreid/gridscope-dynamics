within Dynawo.Electrical.Controls.Machines.Protections;

model GsGeneratorProtection "GridScope generator protection relay: over/under speed, under/over voltage and loss of synchronism (Dynawo protection models combined)"
  /* Each function is Dynawo's own protection model; a disabled function gets an unreachable threshold.
     Any function that operates trips the generator through its third switch-off signal. */
  parameter Boolean SpeedOn = false "Over/under speed protection in service";
  parameter Types.AngularVelocityPu OmegaMinPu = 0.95 "Under-speed threshold in pu (base omegaNom)";
  parameter Types.AngularVelocityPu OmegaMaxPu = 1.05 "Over-speed threshold in pu (base omegaNom)";
  parameter Types.Time tSpeed = 1 "Over/under speed time delay in s";
  parameter Boolean UVOn = false "Under-voltage protection in service";
  parameter Types.VoltageModulePu UMinPu = 0.7 "Under-voltage threshold in pu (base UNom)";
  parameter Types.Time tUV = 1.5 "Under-voltage time delay in s";
  parameter Boolean OVOn = false "Over-voltage protection in service";
  parameter Types.VoltageModulePu UMaxPu = 1.2 "Over-voltage threshold in pu (base UNom)";
  parameter Types.Time tOV = 1 "Over-voltage time delay in s";
  parameter Boolean LOSOn = false "Loss-of-synchronism (pole-slip) protection in service";
  parameter Types.Angle ThetaMax = 2.62 "Internal angle above which the machine is out of step in rad";
  parameter Types.Time tLOS = 0 "Loss-of-synchronism time delay in s";

  input Types.AngularVelocityPu omegaMonitoredPu(start = 1) "Rotor speed in pu (base omegaNom)";
  input Types.VoltageModulePu UMonitoredPu(start = 1) "Terminal voltage in pu (base UNom)";
  input Types.Angle thetaMonitored(start = 0) "Internal angle in rad";
  Dynawo.Connectors.BPin switchOffSignal(value(start = false)) "Trip order for the generator";
  Integer tripCause(start = 0) "0 none, 1 speed, 2 under-voltage, 3 over-voltage, 4 loss of synchronism";

  SpeedProtection speed(OmegaMinPu = if SpeedOn then OmegaMinPu else -1000, OmegaMaxPu = if SpeedOn then OmegaMaxPu else 1000, tLagAction = tSpeed);
  UVA uva(UMinPu = if UVOn then UMinPu else -1000, tLagAction = tUV);
  OVA ova(UMaxPu = if OVOn then UMaxPu else 1000, tLagAction = tOV);
  LossOfSynchronismProtection los(ThetaMax = if LOSOn then ThetaMax else 1000, tLagAction = tLOS);

equation
  speed.omegaMonitoredPu = omegaMonitoredPu;
  uva.UMonitoredPu = UMonitoredPu;
  ova.UMonitoredPu = UMonitoredPu;
  los.thetaMonitored = thetaMonitored;
  switchOffSignal.value = speed.switchOffSignal.value or uva.switchOffSignal.value or ova.switchOffSignal.value or los.switchOffSignal.value;
  tripCause = if speed.switchOffSignal.value then 1 elseif uva.switchOffSignal.value then 2 elseif ova.switchOffSignal.value then 3 elseif los.switchOffSignal.value then 4 else 0;
end GsGeneratorProtection;
