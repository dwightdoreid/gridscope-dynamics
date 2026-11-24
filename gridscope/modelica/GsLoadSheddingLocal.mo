within Dynawo.Electrical.Loads;

model GsLoadSheddingLocal "GridScope: GsLoadShedding measuring frequency locally (for an all-Modelica network, where no system frequency is available)"
  /* P = PRef (1 + deltaP) (1 - shed) (U / U0)^alpha, Q likewise with beta.
     shed = load share disconnected by the UFLS steps (frequency measured at the load bus: 1 + d(voltage angle)/dt / omegaNom) and the UVLS step. */
  import Modelica.Constants;
  extends BaseClasses.BaseLoad;
  extends AdditionalIcons.Load;

  parameter Real alpha "Active load sensitivity to voltage";
  parameter Real beta "Reactive load sensitivity to voltage";
  parameter Boolean UFLSOn = false "Under-frequency load shedding in service";
  parameter Types.AngularVelocityPu omegaThreshold1Pu = 0.98 "UFLS step 1 frequency threshold in pu (base omegaNom)";
  parameter Types.AngularVelocityPu omegaThreshold2Pu = 0.975 "UFLS step 2 frequency threshold in pu (base omegaNom)";
  parameter Types.AngularVelocityPu omegaThreshold3Pu = 0.97 "UFLS step 3 frequency threshold in pu (base omegaNom)";
  parameter Real UFLSStep1 = 0.1 "Share of the load shed by UFLS step 1";
  parameter Real UFLSStep2 = 0.1 "Share of the load shed by UFLS step 2";
  parameter Real UFLSStep3 = 0.1 "Share of the load shed by UFLS step 3";
  parameter Types.Time tUFLS = 0.1 "UFLS time delay (relay + breaker) in s";
  parameter Boolean UVLSOn = false "Under-voltage load shedding in service";
  parameter Types.VoltageModulePu UMinPu = 0.85 "UVLS voltage threshold in pu (base UNom)";
  parameter Real UVLSStep = 0.2 "Share of the load shed by UVLS";
  parameter Types.Time tUVLS = 1 "UVLS time delay in s";

  parameter Types.Time tFilter = 0.05 "Time constant of the voltage filter used to measure the frequency in s";
  parameter Types.Time tOmega = 0.1 "Time constant of the frequency measurement filter in s (rides through angle jumps)";
  Real VfRe(start = u0Pu.re) "Filtered voltage, real part";
  Real VfIm(start = u0Pu.im) "Filtered voltage, imaginary part";
  Real omegaRawPu(start = 1) "Unfiltered local frequency in pu (base omegaNom)";
  Real omegaPu(start = 1) "Measured local frequency in pu (base omegaNom)";
  Dynawo.Electrical.Controls.Protections.UFLS3Steps ufls(omegaThresholdPu = if UFLSOn then {omegaThreshold1Pu, omegaThreshold2Pu, omegaThreshold3Pu} else {-1, -1, -1}, UFLSStep = {UFLSStep1, UFLSStep2, UFLSStep3}, tLagAction = tUFLS);
  Boolean uvlsActivated(start = false) "UVLS has operated";
  discrete Types.Time tUReached(start = Constants.inf) "Time the voltage fell below the UVLS threshold";
  Real uvlsShed(start = 0) "Share shed by UVLS (smoothed)";
  Real shed(start = 0) "Total share of the load shed";

equation
  der(VfRe) * tFilter = terminal.V.re - VfRe;
  der(VfIm) * tFilter = terminal.V.im - VfIm;
  // d(angle of Vf)/dt = Im(conj(Vf) dVf/dt) / |Vf|^2
  omegaRawPu = 1 + (VfRe * (terminal.V.im - VfIm) - VfIm * (terminal.V.re - VfRe)) / tFilter / (VfRe ^ 2 + VfIm ^ 2 + 1e-9) / SystemBase.omegaNom;
  der(omegaPu) * tOmega = omegaRawPu - omegaPu;
  ufls.omegaMonitoredPu = omegaPu;
  when time > 0.001 and UPu.value <= UMinPu and UVLSOn and running.value and not pre(uvlsActivated) then
    tUReached = time;
  elsewhen UPu.value > UMinPu and pre(tUReached) <> Constants.inf and not pre(uvlsActivated) then
    tUReached = Constants.inf;
  end when;
  when time - tUReached >= tUVLS then
    uvlsActivated = true;
  end when;
  der(uvlsShed) * 0.01 = (if uvlsActivated then UVLSStep else 0) - uvlsShed;
  shed = min(1, uvlsShed - ufls.deltaPQfiltered);
  if (running.value) then
    PPu = PRefPu * (1 + deltaP) * (1 - shed) * ((ComplexMath.'abs'(terminal.V) / ComplexMath.'abs'(u0Pu)) ^ alpha);
    QPu = QRefPu * (1 + deltaQ) * (1 - shed) * ((ComplexMath.'abs'(terminal.V) / ComplexMath.'abs'(u0Pu)) ^ beta);
  else
    terminal.i = Complex(0);
  end if;
end GsLoadSheddingLocal;
