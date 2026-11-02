#!/usr/bin/env python3
"""ZUCKY CENTER v5 -- price-timed dispatch edition.
v5 changes: price-timed battery dispatch valued on the ILLUSTRATIVE price signal (energy model
unchanged); booster always lifts to supply + HX approach (v3/v4 lift-target inconsistency fixed);
test counting no longer re-runs the suite inside the doc renderer; new doc section 21.
Previous headers follow.

ZUCKY CENTER v3 -- climate-sited edition (fuses the SubArcticCoolingSimulator idea, patched,
into v2: continuous PUE(T) curves per cooling type over monthly/hourly ILLUSTRATIVE profiles,
site/climate layer, cold-water sinks, seasonal heat sales, climate scenarios).
Previous header: ZUCKY CENTER v2 -- underground AI data center ledger (extends underground_energy.py).

Adds: split battery cold sides (cold_charge_c / cold_discharge_c), ORC discharge,
capped rock-warming horizon, FRACTION_OF_CARNOT sweep, and a DataCenter composer
(cooling type, heat customer, waste-heat battery, outage-sized LAES with cold reuse,
optional molten-salt tier, flexible compute, underground site ledger lines) that
emits the first-law table from EnergyFlow accounting.
v2 FUSED EDITION: code and the v2 design document in one standard-library file.
Run:  python3 zucky_center.py            (baseline vs evolved + table)
      python3 zucky_center.py doc        (full design document, plain text, live numbers)
      python3 zucky_center.py doc --md   (same document as Markdown)
      python3 zucky_center.py --test     (all tests, incl. document checks)
      python3 zucky_center.py --json     (numbers used by the design document)

Original header follows.
Underground Energy -- fused prototype (sCO2 generator + Carnot battery + LAES).

PROTOTYPE FOR EXPLORATION, NOT A DESIGN. Standard library only.
Run:  python3 underground_energy.py          (side-by-side comparison + examples)
      python3 underground_energy.py --test   (all tests)

Fuses sco2_generator.py and carnot_battery.py (logic, names, flags kept) with a
rebuilt liquid-air energy storage (LAES) model. One shared core: geothermal
gradient, Carnot efficiency, sCO2 critical-point flag, STANDING.

Every number marked ILLUSTRATIVE is an assumption for exploration,
not a measured value. The ledger reports; it does not rule.
"""
import math
import sys
from dataclasses import dataclass, field

STANDING = False  # the ledger reports; it does not rule


# ======================= shared core =======================
def geothermal_temp_c(surface_temp_c, depth_meters, gradient_c_per_km=25.0):
    """ONE shared rock temperature: surface + depth x gradient (C/km)."""
    return surface_temp_c + depth_meters / 1000.0 * gradient_c_per_km


def carnot_efficiency(hot_temp_k, cold_temp_k):
    """ONE shared Carnot limit 1 - Tc/Th (0 if not a valid engine)."""
    if hot_temp_k <= cold_temp_k or cold_temp_k <= 0:
        return 0.0
    return 1.0 - cold_temp_k / hot_temp_k


def rte_guard(rte, system):
    """Applied to EVERY system: >100% round trip means impossible inputs."""
    if rte > 1.0:
        raise ValueError(f"{system}: RTE {rte*100:.1f}% exceeds 100%: inputs are thermodynamically impossible")
    return rte


# ======================= sCO2 generator =======================
KELVIN = 273.15
CO2_CRITICAL_C = 31.0          # CO2 critical temperature (physical constant, ~30.98 C)
CRITICAL_MARGIN_C = 32.0       # below this the compressor inlet sits near/below critical
DEGRADE_ABOVE_C = 40.0         # ILLUSTRATIVE: efficiency falls off noticeably above here
SEVERE_ABOVE_C = 45.0          # ILLUSTRATIVE: strong degradation
FRACTION_OF_CARNOT = 0.48      # ILLUSTRATIVE: compact sCO2 Brayton ~45-50% of Carnot
PARASITIC_BASE = 0.02          # ILLUSTRATIVE: 2% of gross for pumps/fans at surface
PARASITIC_PER_KM = 0.03        # ILLUSTRATIVE: +3% of gross per km of lift/circulation
ROCK_WARMING_C_PER_YEAR = 1.5  # ILLUSTRATIVE: rock sink warming per year of operation
ROCK_WARMING_HORIZON_YEARS = 10.0  # ILLUSTRATIVE: linear warming model only trusted this long
SINK_STORE_MIN_GAP_C = 50.0    # ILLUSTRATIVE: flag when sink comes within this of the hot side
ORC_FRACTION_OF_CARNOT = 0.55  # ILLUSTRATIVE: low-temperature ORC ~50-60% of Carnot
GRADIENT_RANGE = (15.0, 60.0)  # C/km, typical continental range


@dataclass
class SimulationResult:
    sink: str
    depth_meters: float
    rock_temp_c: float
    source_temp_c: float
    cold_side_c: float
    carnot_eff: float
    sco2_eff: float
    heat_input_mw_th: float
    gross_mw_e: float
    parasitic_mw_e: float
    net_mw_e: float
    flags: list = field(default_factory=list)


class UndergroundSCO2Generator:
    def __init__(self, depth_meters, source_temp_c, surface_temp_c,
                 sink="surface", approach_c=10.0, gradient_c_per_km=25.0,
                 heat_input_mw_th=10.0, years_operating=0.0,
                 fraction_of_carnot=None,
                 warming_horizon_years=ROCK_WARMING_HORIZON_YEARS):
        if depth_meters < 0:
            raise ValueError("depth_meters cannot be negative")
        lo, hi = GRADIENT_RANGE
        if not lo <= gradient_c_per_km <= hi:
            raise ValueError(f"gradient must be {lo}-{hi} C/km")
        if sink not in ("surface", "rock"):
            raise ValueError("sink must be 'surface' or 'rock'")
        if approach_c < 0:
            raise ValueError("approach_c cannot be negative")
        if heat_input_mw_th <= 0:
            raise ValueError("heat_input_mw_th must be positive")
        if years_operating < 0:
            raise ValueError("years_operating cannot be negative")
        self.depth_meters = depth_meters
        self.source_temp_c = source_temp_c
        self.surface_temp_c = surface_temp_c
        self.sink = sink
        self.approach_c = approach_c
        self.gradient_c_per_km = gradient_c_per_km
        self.heat_input_mw_th = heat_input_mw_th
        self.years_operating = years_operating
        self.fraction_of_carnot = fraction_of_carnot
        self.warming_horizon_years = warming_horizon_years
        if self.source_temp_c <= self.cold_side_c():
            raise ValueError("hot side must be above cold side")

    def calculate_geothermal_temp(self):
        """Rock temp = surface + depth x gradient (gradient is a parameter)."""
        return geothermal_temp_c(self.surface_temp_c, self.depth_meters, self.gradient_c_per_km)

    def cold_side_c(self):
        """Compressor inlet / heat-rejection temperature for the chosen sink."""
        if self.sink == "surface":
            return self.surface_temp_c + self.approach_c
        # ILLUSTRATIVE linear warming, capped at the valid horizon (beyond it the
        # unbounded line would eventually invert hot and cold sides)
        warming = ROCK_WARMING_C_PER_YEAR * min(self.years_operating, self.warming_horizon_years)
        return self.calculate_geothermal_temp() + warming + self.approach_c

    @staticmethod
    def estimate_carnot_efficiency(hot_temp_k, cold_temp_k):
        """Theoretical maximum thermodynamic efficiency."""
        return carnot_efficiency(hot_temp_k, cold_temp_k)

    def simulate(self):
        rock = self.calculate_geothermal_temp()
        cold = self.cold_side_c()
        carnot = self.estimate_carnot_efficiency(self.source_temp_c + KELVIN, cold + KELVIN)
        frac = FRACTION_OF_CARNOT if self.fraction_of_carnot is None else self.fraction_of_carnot
        if not 0 < frac <= 1:
            raise ValueError("fraction_of_carnot must be in (0, 1]")
        eff = carnot * frac
        gross = self.heat_input_mw_th * eff
        parasitic_frac = PARASITIC_BASE + PARASITIC_PER_KM * self.depth_meters / 1000.0
        parasitic = min(gross, gross * parasitic_frac)
        flags = []
        flags.append(critical_point_flag(cold))
        if self.sink == "rock" and self.years_operating > self.warming_horizon_years:
            flags.append(f"[HORIZON] {self.years_operating:g} years exceeds the "
                         f"{self.warming_horizon_years:g}-year valid window of the linear warming "
                         "model (ILLUSTRATIVE); warming held at the cap, re-model before trusting.")
        flags += sink_gap_flags(self.source_temp_c, cold)
        return SimulationResult(self.sink, self.depth_meters, rock, self.source_temp_c,
                                cold, carnot, eff, self.heat_input_mw_th, gross,
                                parasitic, gross - parasitic, flags)


def report_sco2(r):
    lines = [
        f"=== UNDERGROUND sCO2 GENERATOR -- sink: {r.sink} ===",
        f"Subterranean Depth:        {r.depth_meters:.0f} m",
        f"Surrounding Rock Temp:     {r.rock_temp_c:.2f} C",
        f"Heat Source Temp:          {r.source_temp_c:.1f} C",
        f"Cold Side (comp. inlet):   {r.cold_side_c:.2f} C",
        f"Carnot Limit:              {r.carnot_eff*100:.2f} %",
        f"Est. sCO2 Efficiency:      {r.sco2_eff*100:.2f} %  (ILLUSTRATIVE 0.48 x Carnot)",
        f"Heat Input:                {r.heat_input_mw_th:.2f} MW_th",
        f"Gross Electric:            {r.gross_mw_e:.3f} MW_e",
        f"Parasitic (pump/lift):     {r.parasitic_mw_e:.3f} MW_e  (ILLUSTRATIVE)",
        f"Net Electric:              {r.net_mw_e:.3f} MW_e",
    ] + r.flags
    return "\n".join(lines)


def side_by_side(depth=800, source=550, surface=15, **kw):
    out = []
    for sink in ("surface", "rock"):
        out.append(report_sco2(UndergroundSCO2Generator(depth, source, surface, sink=sink, **kw).simulate()))
    out.append("Assumptions marked ILLUSTRATIVE are not measured data. "
               "The ledger reports; it does not rule.")
    return "\n\n".join(out)




def sink_gap_flags(hot_c, cold_c):
    """Flag when the sink approaches the store/source temperature."""
    gap = hot_c - cold_c
    if gap < SINK_STORE_MIN_GAP_C:
        return [f"[SINK NEAR STORE] Sink {cold_c:.1f} C is only {gap:.1f} C below the hot side "
                f"{hot_c:.1f} C (< {SINK_STORE_MIN_GAP_C:.0f} C, ILLUSTRATIVE); output collapses."]
    return []


def critical_point_flag(cold):
    """ONE shared sCO2 compressor-inlet flag."""
    if cold < CRITICAL_MARGIN_C:
        return (f"[CRITICAL POINT] Compressor inlet {cold:.1f} C is near/below the "
                     f"CO2 critical point ({CO2_CRITICAL_C} C); real-fluid behavior "
                     "dominates, the simple factor is unreliable here.")
    elif cold > SEVERE_ABOVE_C:
        return (f"[WARNING] Compressor inlet {cold:.1f} C is far above critical; "
                     "compressor work rises sharply, expect efficiency well below estimate.")
    elif cold > DEGRADE_ABOVE_C:
        return (f"[CAUTION] Compressor inlet {cold:.1f} C is above ~{DEGRADE_ABOVE_C:.0f} C; "
                     "efficiency begins to degrade.")
    else:
        return (f"[OK] Compressor inlet {cold:.1f} C sits in the near-critical sweet spot.")


# ======================= Carnot battery =======================
HEAT_PUMP_FRACTION = 0.55   # ILLUSTRATIVE: real heat pumps reach ~50-60% of Carnot COP
LOSS_K_PER_DAY_PER_C = 2e-5  # ILLUSTRATIVE: decay rate k (1/day) per C of store-rock gap

# ILLUSTRATIVE max storage temperatures (C) per medium, plus notes.
MEDIA = {
    "hot_rock":          {"max_c": 600.0, "note": "packed rock / crushed-rock bed"},
    "molten_salt_tank":  {"max_c": 565.0, "note": "solar-salt tanks, freezes ~220 C"},
    "hot_water_aquifer": {"max_c": 150.0, "note": "pressurized aquifer storage (ATES/HT-ATES)"},
    "salt_cavern":       {"max_c": 80.0,
                          "note": "UNSUITABLE for hot storage: salt creeps and dissolves "
                                  "when heated; caverns suit gas, air or brine storage"},
}


def carnot_cop(hot_k, cold_k):
    """Heating COP limit T_h/(T_h-T_c)."""
    if hot_k <= cold_k or cold_k <= 0:
        raise ValueError("hot side must be above cold side (Kelvin > 0)")
    return hot_k / (hot_k - cold_k)


def check_consistency(cop, turbine_eff):
    """COP x eta <= 1 is required at ANY pair of temperatures, because
    COP <= T_h/(T_h-T_c) = 1/eta_carnot and eta <= eta_carnot.
    Returns (ok, product, message)."""
    product = cop * turbine_eff
    if product > 1.0:
        return (False, product,
                f"INCONSISTENT: COP {cop} x efficiency {turbine_eff} = {product:.3f} > 1; "
                "no pair of temperatures allows both (round trip would exceed 100%).")
    return True, product, f"consistent: COP x eta = {product:.3f} <= 1"


def implied_store_temp_c(cop, cold_c):
    """Storage temperature at which a PERFECT heat pump reaches this COP."""
    if cop <= 1:
        raise ValueError("COP must exceed 1 to imply a temperature")
    tc = cold_c + KELVIN
    return tc * cop / (cop - 1) - KELVIN


@dataclass
class BatteryResult:
    medium: str
    storage_temp_c: float
    cold_temp_c: float
    rock_temp_c: float
    capacity_mwh_th: float
    carnot_cop: float
    cop: float
    carnot_eff: float
    discharge_eff: float
    electricity_in_mwh: float
    charge_power_mw: float
    decay_k_per_day: float
    retained_mwh_th: float
    loss_mwh_th: float
    electricity_out_mwh: float
    discharge_power_mw: float
    rte: float
    flags: list = field(default_factory=list)
    cold_charge_c: float = None
    cold_discharge_c: float = None
    discharge_cycle: str = "sco2"
    heat_lifted_mwh_th: float = 0.0      # drawn from the cold (source) side during charge
    discharge_rejected_mwh_th: float = 0.0


class UndergroundCarnotBattery:
    def __init__(self, storage_capacity_mwh, storage_temp_c, cold_temp_c=15.0,
                 medium="hot_rock", rock_temp_c=None,
                 heat_pump_fraction=HEAT_PUMP_FRACTION,
                 loss_k_per_day_per_c=LOSS_K_PER_DAY_PER_C,
                 cold_charge_c=None, cold_discharge_c=None,
                 discharge_cycle="sco2", fraction_of_carnot=None):
        """cold_temp_c is kept for compatibility: it is used for whichever of
        cold_charge_c (heat-pump source) / cold_discharge_c (engine sink) is not given."""
        cold_charge_c = cold_temp_c if cold_charge_c is None else cold_charge_c
        cold_discharge_c = cold_temp_c if cold_discharge_c is None else cold_discharge_c
        if discharge_cycle not in ("sco2", "orc"):
            raise ValueError("discharge_cycle must be 'sco2' or 'orc'")
        if storage_temp_c <= max(cold_charge_c, cold_discharge_c):
            raise ValueError("storage must be hotter than both cold sides")
        if storage_capacity_mwh <= 0:
            raise ValueError("storage_capacity_mwh must be positive")
        if medium not in MEDIA:
            raise ValueError(f"medium must be one of {sorted(MEDIA)}")
        if storage_temp_c > MEDIA[medium]["max_c"]:
            raise ValueError(f"{medium} limit is {MEDIA[medium]['max_c']} C (ILLUSTRATIVE)")
        if storage_temp_c <= cold_temp_c:
            raise ValueError("storage must be hotter than the cold side")
        if not 0 < heat_pump_fraction <= 1:
            raise ValueError("heat_pump_fraction must be in (0, 1]")
        if loss_k_per_day_per_c < 0:
            raise ValueError("loss rate cannot be negative")
        self.capacity_mwh = storage_capacity_mwh
        self.storage_temp_c = storage_temp_c
        self.cold_temp_c = cold_temp_c
        self.medium = medium
        self.cold_charge_c = cold_charge_c
        self.cold_discharge_c = cold_discharge_c
        self.discharge_cycle = discharge_cycle
        self.fraction_of_carnot = fraction_of_carnot
        self.rock_temp_c = cold_discharge_c if rock_temp_c is None else rock_temp_c
        self.hp_fraction = heat_pump_fraction
        self.loss_coeff = loss_k_per_day_per_c

    def decay_k(self):
        """k (1/day) scales with the store-to-rock temperature gap (ILLUSTRATIVE)."""
        return self.loss_coeff * max(0.0, self.storage_temp_c - self.rock_temp_c)

    def retained(self, idle_hrs):
        """E = E0 * exp(-k t)."""
        return self.capacity_mwh * math.exp(-self.decay_k() * idle_hrs / 24.0)

    def simulate_cycle(self, charge_duration_hrs, idle_duration_hrs, discharge_duration_hrs):
        if charge_duration_hrs <= 0 or discharge_duration_hrs <= 0:
            raise ValueError("charge and discharge durations must be positive")
        if idle_duration_hrs < 0:
            raise ValueError("idle duration cannot be negative")
        flags = []
        hot_k, cold_k = self.storage_temp_c + KELVIN, self.cold_charge_c + KELVIN
        ccop = carnot_cop(hot_k, cold_k)
        cop = ccop * self.hp_fraction
        if cop < 1.0:
            flags.append(f"[CHARGE] Heat-pump COP {cop:.2f} < 1 at this lift; "
                         "resistive heating (COP 1) is used instead.")
            cop = 1.0
        if self.medium == "salt_cavern":
            flags.append("[UNSUITABLE] Salt caverns creep and dissolve when heated; "
                         "use for gas/air/brine storage, not hot thermal storage.")
        e_in = self.capacity_mwh / cop
        retained = self.retained(idle_duration_hrs)
        if self.discharge_cycle == "sco2":
            # Discharge through the shared sCO2 model (depth 0, cold side = sink, no approach).
            gen = UndergroundSCO2Generator(0, self.storage_temp_c, self.cold_discharge_c,
                                           sink="surface", approach_c=0.0,
                                           heat_input_mw_th=retained / discharge_duration_hrs,
                                           fraction_of_carnot=self.fraction_of_carnot)
            g = gen.simulate()
            flags += g.flags
            carnot_d, eff_d = g.carnot_eff, g.sco2_eff
        else:
            frac = ORC_FRACTION_OF_CARNOT if self.fraction_of_carnot is None else self.fraction_of_carnot
            carnot_d = carnot_efficiency(self.storage_temp_c + KELVIN, self.cold_discharge_c + KELVIN)
            eff_d = carnot_d * frac
            flags.append(f"[ORC] Discharge via organic Rankine cycle at {frac} x Carnot (ILLUSTRATIVE).")
            flags += sink_gap_flags(self.storage_temp_c, self.cold_discharge_c)
        e_out = retained * eff_d
        rte = e_out / e_in
        if rte > 1.0:
            raise ValueError(f"RTE {rte*100:.1f}% exceeds 100%: inputs are thermodynamically impossible")
        if rte < 0.4:
            flags.append(f"[NOTE] RTE {rte*100:.1f}% is below the ~40-60% often cited for Carnot batteries.")
        return BatteryResult(self.medium, self.storage_temp_c, self.cold_temp_c, self.rock_temp_c,
                             self.capacity_mwh, ccop, cop, carnot_d, eff_d,
                             e_in, e_in / charge_duration_hrs, self.decay_k(), retained,
                             self.capacity_mwh - retained, e_out,
                             e_out / discharge_duration_hrs, rte, flags,
                             self.cold_charge_c, self.cold_discharge_c, self.discharge_cycle,
                             self.capacity_mwh - e_in, retained - e_out)


def report_battery(r):
    lines = [
        f"=== UNDERGROUND CARNOT BATTERY -- medium: {r.medium} ===",
        f"Storage / Cold / Rock:     {r.storage_temp_c:.0f} / {r.cold_temp_c:.0f} / {r.rock_temp_c:.0f} C",
        f"Capacity:                  {r.capacity_mwh_th:.2f} MWh_th",
        f"Carnot COP / used COP:     {r.carnot_cop:.2f} / {r.cop:.2f}  (ILLUSTRATIVE {HEAT_PUMP_FRACTION} x Carnot)",
        f"[CHARGE] Electricity in:   {r.electricity_in_mwh:.2f} MWh_e at {r.charge_power_mw:.2f} MW",
        f"[IDLE] Decay k:            {r.decay_k_per_day*100:.3f} %/day (ILLUSTRATIVE)",
        f"[IDLE] Retained heat:      {r.retained_mwh_th:.2f} MWh_th (loss {r.loss_mwh_th:.2f})",
        f"Cold charge / discharge:   {r.cold_charge_c:.0f} / {r.cold_discharge_c:.0f} C",
        f"Carnot / {r.discharge_cycle} efficiency:  {r.carnot_eff*100:.2f} / {r.discharge_eff*100:.2f} %  (ILLUSTRATIVE "
        f"{ORC_FRACTION_OF_CARNOT if r.discharge_cycle == 'orc' else FRACTION_OF_CARNOT} x Carnot)",
        f"[DISCHARGE] Electricity:   {r.electricity_out_mwh:.2f} MWh_e at {r.discharge_power_mw:.2f} MW",
        f"Round-Trip Efficiency:     {r.rte*100:.2f} %",
    ] + r.flags
    return "\n".join(lines)


def example_battery():
    out = []
    ok, prod, msg = check_consistency(3.5, 0.52)
    out.append("Original inputs (COP 3.5, turbine 0.52): " + msg +
               f" A perfect heat pump hits COP 3.5 only up to ~{implied_store_temp_c(3.5, 15):.0f} C, "
               f"where Carnot efficiency is ~{UndergroundSCO2Generator.estimate_carnot_efficiency(implied_store_temp_c(3.5, 15)+KELVIN, 15+KELVIN)*100:.0f}%.")
    for medium, temp in (("molten_salt_tank", 565), ("hot_rock", 400), ("hot_water_aquifer", 150)):
        b = UndergroundCarnotBattery(50.0, temp, 15.0, medium=medium, rock_temp_c=35.0)
        out.append(report_battery(b.simulate_cycle(6, 12, 6)))
    out.append("Assumptions marked ILLUSTRATIVE are not measured data. "
               "The ledger reports; it does not rule.")
    return "\n\n".join(out)




# ======================= LAES (rebuilt) =======================
LAES_MIN_WORK_KWH_PER_KG = 0.20      # ~theoretical minimum liquefaction work (approx.)
LIQUEFACTION_KWH_PER_KG = 0.35       # ILLUSTRATIVE: with cold recycle
LIQUEFACTION_NO_RECYCLE_KWH_PER_KG = 0.50  # ILLUSTRATIVE: without cold recycle
DISCHARGE_KWH_PER_KG = 0.15          # ILLUSTRATIVE: pump + ambient reheat + expansion (~0.12-0.2)
HOT_STORE_BOOST_KWH_PER_KG = 0.05    # ILLUSTRATIVE: extra expansion work from a full hot store at 450 C
HOT_STORE_REF_C = 450.0              # reference hot-store temperature for the boost
LIQUID_AIR_LATENT_KJ_PER_KG = 199.0  # approximation (N2 ~199, O2 ~213; air is a mixture)
LIQUID_AIR_TEMP_C = -194.0           # approx. normal boiling point of liquid air
LIQUID_AIR_DENSITY = 875.0           # kg/m3, approximation
TANK_U_W_PER_M2K = 0.02              # ILLUSTRATIVE: vacuum/perlite-insulated large tank
LAES_PUMP_PER_KM = 0.03              # ILLUSTRATIVE: +3% of output per km of lift/circulation
LAES_GOOD_RTE = 0.50                 # real LAES ~50-60% with good cold recycle; pilots lower


@dataclass
class LAESResult:
    depth_meters: float
    rock_temp_c: float
    electricity_in_mwh: float
    liquefaction_kwh_per_kg: float
    mass_kg: float
    tank_area_m2: float
    u_w_per_m2k: float
    boil_off_kg_per_day: float
    boil_off_kg: float
    retained_kg: float
    discharge_kwh_per_kg: float
    hot_store_boost_kwh_per_kg: float
    gross_out_mwh: float
    pump_mwh: float
    electricity_out_mwh: float
    rte: float
    flags: list = field(default_factory=list)


class UndergroundLAESSystem:
    """capacity_mwh means ELECTRICITY IN (MWh_e). Efficiency is applied once:
    via kWh/kg in (liquefaction) and kWh/kg out (discharge)."""

    def __init__(self, capacity_mwh, depth_meters=0.0, storage_days=1.0,
                 surface_temp_c=15.0, gradient_c_per_km=25.0,
                 liquefaction_kwh_per_kg=None, cold_recycle=True,
                 discharge_kwh_per_kg=DISCHARGE_KWH_PER_KG,
                 hot_store_temp_c=None, hot_store_medium="hot_rock",
                 cavern="tank", tank_area_m2=None, u_w_per_m2k=TANK_U_W_PER_M2K,
                 cryo_temp_c=LIQUID_AIR_TEMP_C):
        if capacity_mwh <= 0:
            raise ValueError("capacity_mwh (electricity in) must be positive")
        if depth_meters < 0 or storage_days < 0:
            raise ValueError("depth and storage_days cannot be negative")
        if liquefaction_kwh_per_kg is None:
            liquefaction_kwh_per_kg = (LIQUEFACTION_KWH_PER_KG if cold_recycle
                                       else LIQUEFACTION_NO_RECYCLE_KWH_PER_KG)
        if liquefaction_kwh_per_kg < LAES_MIN_WORK_KWH_PER_KG:
            raise ValueError(f"liquefaction work {liquefaction_kwh_per_kg} kWh/kg is below the "
                             f"~{LAES_MIN_WORK_KWH_PER_KG} kWh/kg theoretical minimum")
        if discharge_kwh_per_kg <= 0:
            raise ValueError("discharge_kwh_per_kg must be positive")
        if u_w_per_m2k < 0 or (tank_area_m2 is not None and tank_area_m2 <= 0):
            raise ValueError("U cannot be negative; tank area must be positive")
        if cavern not in ("tank", "salt", "hard_rock"):
            raise ValueError("cavern must be 'tank', 'salt' or 'hard_rock'")
        self.hot_store = None
        if hot_store_temp_c is not None:
            # reuse the battery's hot-store code (media limits, exponential decay)
            self.hot_store = UndergroundCarnotBattery(1.0, hot_store_temp_c, surface_temp_c,
                                                      medium=hot_store_medium)
        self.capacity_mwh = capacity_mwh
        self.depth_meters = depth_meters
        self.storage_days = storage_days
        self.surface_temp_c = surface_temp_c
        self.gradient = gradient_c_per_km
        self.w_liq = liquefaction_kwh_per_kg
        self.cold_recycle = cold_recycle
        self.w_dis = discharge_kwh_per_kg
        self.cavern = cavern
        self.u = u_w_per_m2k
        self.cryo_temp_c = cryo_temp_c
        self.tank_area_override = tank_area_m2

    def rock_temp_c(self):
        return geothermal_temp_c(self.surface_temp_c, self.depth_meters, self.gradient)

    def mass_kg(self):
        return self.capacity_mwh * 1000.0 / self.w_liq

    def tank_area_m2(self):
        """Given area, else a single sphere holding the liquid (approximation)."""
        if self.tank_area_override is not None:
            return self.tank_area_override
        v = self.mass_kg() / LIQUID_AIR_DENSITY
        r = (3 * v / (4 * math.pi)) ** (1 / 3)
        return 4 * math.pi * r * r

    def boil_off_kg_per_day(self):
        """Q = U A dT (W) -> kg/day via latent heat (~199 kJ/kg, approximation)."""
        gap = max(0.0, self.rock_temp_c() - self.cryo_temp_c)
        q_w = self.u * self.tank_area_m2() * gap
        return q_w * 86400.0 / (LIQUID_AIR_LATENT_KJ_PER_KG * 1000.0)

    def hot_store_boost(self):
        if self.hot_store is None:
            return 0.0
        hs = self.hot_store
        scale = (carnot_efficiency(hs.storage_temp_c + KELVIN, self.surface_temp_c + KELVIN)
                 / carnot_efficiency(HOT_STORE_REF_C + KELVIN, self.surface_temp_c + KELVIN))
        kept = hs.retained(self.storage_days * 24.0) / hs.capacity_mwh
        return HOT_STORE_BOOST_KWH_PER_KG * scale * kept

    def simulate(self):
        flags = []
        m = self.mass_kg()
        rate = self.boil_off_kg_per_day()
        lost = min(m, rate * self.storage_days)   # physical cap: cannot lose more than stored
        kept = m - lost
        boost = self.hot_store_boost()
        w_out = self.w_dis + boost
        gross = kept * w_out / 1000.0
        pump = gross * LAES_PUMP_PER_KM * self.depth_meters / 1000.0
        pump = min(gross, pump)
        e_out = gross - pump
        rte = rte_guard(e_out / self.capacity_mwh, "LAES")
        if self.depth_meters > 0:
            flags.append(f"[DEPTH] {self.depth_meters:.0f} m adds lift/pumping cost "
                         f"({pump:.2f} MWh, ILLUSTRATIVE) and warmer rock ({self.rock_temp_c():.1f} C) "
                         "that raises boil-off; liquid air tanks gain little from depth.")
        if self.cavern == "salt" and self.hot_store is not None and self.hot_store.storage_temp_c > MEDIA["salt_cavern"]["max_c"]:
            flags.append(f"[UNSUITABLE] Salt cavern with a {self.hot_store.storage_temp_c:.0f} C hot store: "
                         "salt creeps and dissolves when heated.")
        if not self.cold_recycle:
            flags.append("[NOTE] No cold recycle: liquefaction work is much higher.")
        if lost >= m:
            flags.append("[BOIL-OFF] Entire inventory boiled off.")
        if rte < LAES_GOOD_RTE:
            flags.append(f"[NOTE] RTE {rte*100:.1f}% is below the ~50-60% real LAES reaches "
                         "with good cold recycle (pilots have been lower).")
        return LAESResult(self.depth_meters, self.rock_temp_c(), self.capacity_mwh, self.w_liq, m,
                          self.tank_area_m2(), self.u, rate, lost, kept, self.w_dis, boost,
                          gross, pump, e_out, rte, flags)


def report_laes(r):
    lines = [
        "=== UNDERGROUND LAES (liquid air) ===",
        f"Depth / Rock Temp:         {r.depth_meters:.0f} m / {r.rock_temp_c:.1f} C",
        f"[CHARGE] Electricity in:   {r.electricity_in_mwh:.2f} MWh_e at {r.liquefaction_kwh_per_kg} kWh/kg (ILLUSTRATIVE)",
        f"Liquid air made:           {r.mass_kg/1000:.1f} t",
        f"Tank area / U:             {r.tank_area_m2:.0f} m2 / {r.u_w_per_m2k} W/m2K (ILLUSTRATIVE)",
        f"[BOIL-OFF] Rate / total:   {r.boil_off_kg_per_day/1000:.2f} t/day / {r.boil_off_kg/1000:.2f} t",
        f"Discharge work:            {r.discharge_kwh_per_kg} + {r.hot_store_boost_kwh_per_kg:.3f} hot-store kWh/kg (ILLUSTRATIVE)",
        f"[DISCHARGE] Electricity:   {r.electricity_out_mwh:.2f} MWh_e (pump {r.pump_mwh:.2f})",
        f"Round-Trip Efficiency:     {r.rte*100:.2f} %",
    ] + r.flags
    return "\n".join(lines)


# ======================= comparison =======================
def compare(electricity_in_mwh=50.0, storage_hours=12.0, depth=600.0, surface=15.0):
    """Common scenario. The sCO2 generator is not storage: it is shown converting
    the same 50 MWh as WASTE HEAT (MWh_th), so its number is heat-to-power."""
    days = storage_hours / 24.0
    rock = geothermal_temp_c(surface, depth)
    g = UndergroundSCO2Generator(depth, 550, surface, heat_input_mw_th=electricity_in_mwh / storage_hours).simulate()
    gen_out = g.net_mw_e * storage_hours
    probe = UndergroundCarnotBattery(1.0, 565, surface, medium="molten_salt_tank", rock_temp_c=rock).simulate_cycle(6, 0, 6)
    bat = UndergroundCarnotBattery(electricity_in_mwh * probe.cop, 565, surface, medium="molten_salt_tank",
                                   rock_temp_c=rock).simulate_cycle(6, storage_hours, 6)
    la = UndergroundLAESSystem(electricity_in_mwh, depth, days, surface, hot_store_temp_c=450).simulate()
    rows = [("", "sCO2 gen (heat)", "Carnot battery", "LAES"),
            ("Input (MWh)", f"{electricity_in_mwh:.1f} th", f"{bat.electricity_in_mwh:.1f} e", f"{la.electricity_in_mwh:.1f} e"),
            ("Storage loss", "n/a", f"{bat.loss_mwh_th:.2f} MWh_th", f"{la.boil_off_kg/1000:.2f} t air"),
            ("Output (MWh_e)", f"{gen_out:.2f}", f"{bat.electricity_out_mwh:.2f}", f"{la.electricity_out_mwh:.2f}"),
            ("Efficiency", f"{gen_out/electricity_in_mwh*100:.1f}% h->e", f"{bat.rte*100:.1f}% RTE", f"{la.rte*100:.1f}% RTE"),
            ("Flags", str(len(g.flags)), str(len(bat.flags)), str(len(la.flags)))]
    out = [f"=== SIDE BY SIDE: {electricity_in_mwh:.0f} MWh in, {storage_hours:g} h storage, {depth:.0f} m, rock {rock:.1f} C ==="]
    out += [f"{a:<16}{b:>18}{c:>18}{d:>18}" for a, b, c, d in rows]
    out.append("PROTOTYPE FOR EXPLORATION, NOT A DESIGN. ILLUSTRATIVE numbers are not measured data. "
               "The ledger reports; it does not rule.")
    return "\n".join(out)


def example():
    parts = [compare(), compare(storage_hours=72.0), side_by_side(), example_battery()]
    parts.append(report_laes(UndergroundLAESSystem(100.0, 600, 3, hot_store_temp_c=450, cavern="salt").simulate()))
    return "\n\n".join(parts)


# ======================= sensitivity sweep =======================
SWEEP_FRACTIONS = (0.40, 0.48, 0.55)


def fraction_sweep(fractions=SWEEP_FRACTIONS, depth=800, source=550, surface=15, rock=35.0):
    """FRACTION_OF_CARNOT sensitivity: sCO2 heat-to-power and the molten-salt RTE."""
    rows = []
    for f in fractions:
        g = UndergroundSCO2Generator(depth, source, surface, fraction_of_carnot=f).simulate()
        b = UndergroundCarnotBattery(100.0, 565, 25, medium="molten_salt_tank", rock_temp_c=rock,
                                     fraction_of_carnot=f).simulate_cycle(6, 12, 6)
        rows.append({"fraction": f, "sco2_eff": g.sco2_eff, "sco2_net_mw_e_per_10mw": g.net_mw_e,
                     "molten_salt_rte": b.rte})
    return rows


def report_sweep(rows):
    out = ["=== FRACTION_OF_CARNOT SWEEP (ILLUSTRATIVE factors) ===",
           f"{'fraction':>9}{'sCO2 eff':>11}{'net MW/10MW_th':>16}{'salt RTE':>10}"]
    out += [f"{r['fraction']:>9.2f}{r['sco2_eff']*100:>10.1f}%{r['sco2_net_mw_e_per_10mw']:>16.2f}"
            f"{r['molten_salt_rte']*100:>9.1f}%" for r in rows]
    return "\n".join(out)


# ======================= DataCenter composer =======================
# Cooling types: (heat-capture temperature C, default PUE, share of facility heat
# captured in the liquid loop at that temperature). ALL ILLUSTRATIVE.
COOLING = {
    "air":            {"temp_c": 40.0, "pue": 1.15, "capture": 1.00},  # as in the v1 PDF
    "direct_to_chip": {"temp_c": 60.0, "pue": 1.10, "capture": 0.80},
    "immersion":      {"temp_c": 65.0, "pue": 1.05, "capture": 0.95},
}
RESIDUAL_AIR_C = 35.0             # ILLUSTRATIVE: uncaptured heat leaves as ~35 C air
HX_APPROACH_C = 5.0               # ILLUSTRATIVE: direct heat-exchange approach to a heat customer
CHILLER_COP = 5.0                 # ILLUSTRATIVE: electricity avoided per unit of cold delivered
# LAES cold reuse (ILLUSTRATIVE): cold not already returned to the liquefier is used to
# pre-cool the halls; facility waste heat reheats the air before expansion.
COLD_TO_HALLS_KWH_PER_KG = 0.04   # ILLUSTRATIVE (~0.11 kWh/kg total cold to ambient, partly recycled)
WASTE_REHEAT_BOOST_KWH_PER_KG = 0.02   # ILLUSTRATIVE extra expansion work at 60 C reheat
WASTE_REHEAT_REF_C = 60.0
WASTE_REHEAT_CONVERSION = 0.10    # ILLUSTRATIVE: work gained per unit of waste heat used (< Carnot 60->15 = 13.5%)
LAES_EFFECTIVE_RTE_BOUND = 0.70   # ILLUSTRATIVE ceiling on electricity+cooling credit per electricity in
# Flexible compute (ILLUSTRATIVE): shiftable IT share, max loading in absorbing hours
FLEX_HEADROOM = 1.20
# ILLUSTRATIVE 24 h price signal (relative units, index 1.0 = average)
DEFAULT_PRICES = [0.6] * 6 + [0.9] * 4 + [1.0] * 6 + [1.8] * 4 + [1.1] * 4
# Underground site ledger lines. ILLUSTRATIVE parasitic loads or flags -- NOT costs.
GROUNDWATER_INFLOW_L_S = 50.0     # ILLUSTRATIVE; site-specific, can be 10x either way
PUMP_EFF = 0.70                   # ILLUSTRATIVE
VENT_SHARE_OF_IT = 0.005          # ILLUSTRATIVE ventilation + smoke-control fans
HUMIDITY_MW = 0.10                # ILLUSTRATIVE dehumidification
ROCK_MONITOR_MW = 0.02            # ILLUSTRATIVE instrumentation


@dataclass
class EnergyFlow:
    """One first-law column. All values MWh; in must equal out."""
    name: str
    electric_in: float = 0.0
    thermal_in: float = 0.0
    heat_from_cold: float = 0.0
    electric_out: float = 0.0
    thermal_out: float = 0.0
    note: str = ""

    def total_in(self):
        return self.electric_in + self.thermal_in + self.heat_from_cold

    def total_out(self):
        return self.electric_out + self.thermal_out

    def assert_balanced(self, tol=1e-6):
        # Guards the ARITHMETIC (coding slips, sign errors), not the physics: several
        # composer columns define thermal_out as the balancing residual, so this cannot
        # fail there by construction. The physics check is the grid == heat-to-environment
        # identity tested in _tests_zucky.
        vals = (self.electric_in, self.thermal_in, self.heat_from_cold, self.electric_out, self.thermal_out)
        if min(vals) < -tol:
            raise AssertionError(f"{self.name}: negative flow {vals}")
        if abs(self.total_in() - self.total_out()) > tol * max(1.0, self.total_in()):
            raise AssertionError(f"{self.name}: first law violated in {self.total_in()} != out {self.total_out()}")
        return True


@dataclass
class HeatCustomer:
    """District heating (or similar) customer. ILLUSTRATIVE numbers in scenarios."""
    demand_mw_th: float
    supply_temp_c: float
    availability: float = 1.0   # fraction of the year the demand exists (seasonal)

    def __post_init__(self):
        if self.demand_mw_th < 0 or not 0 <= self.availability <= 1:
            raise ValueError("demand must be >= 0 and availability in [0, 1]")


def flex_shift(load_mw, prices, flexible_share, headroom=FLEX_HEADROOM):
    """Shift the flexible share of load out of the most expensive hours into the
    cheapest ones. Energy is conserved; loading capped at headroom x base."""
    if not 0 <= flexible_share <= 1:
        raise ValueError("flexible_share must be in [0, 1]")
    n = len(prices)
    base = [load_mw] * n
    order = sorted(range(n), key=lambda h: prices[h])
    mean = sum(prices) / n
    expensive = [h for h in reversed(order) if prices[h] > mean]
    cheap = [h for h in order if prices[h] < mean]
    new = list(base)
    to_move = 0.0
    for h in expensive:
        cut = load_mw * flexible_share
        new[h] -= cut; to_move += cut
    room = load_mw * (headroom - 1.0)
    for h in cheap:
        add = min(room, to_move)
        new[h] += add; to_move -= add
    for h in expensive[::-1]:          # anything that did not fit goes back (least expensive first)
        if to_move <= 0: break
        back = min(to_move, base[h] - new[h])
        new[h] += back; to_move -= back
    shifted = sum(max(0.0, b - x) for b, x in zip(base, new))
    return {"hourly_mw": new, "shifted_mwh": shifted,
            "cost_index_base": sum(b * p for b, p in zip(base, prices)),
            "cost_index_flex": sum(x * p for x, p in zip(new, prices))}


def dispatch_windows(prices, charge_hours, discharge_hours):
    """Cheapest hours to charge, priciest non-charge hours to discharge.

    ILLUSTRATIVE price signal (relative units, like flex_shift). The ENERGY model is
    untouched: this only re-values the same MWh at the signal. Not money."""
    n = len(prices)
    if not (0 < charge_hours and 0 < discharge_hours and charge_hours + discharge_hours <= n):
        raise ValueError("charge + discharge hours must be positive and fit in the price signal")
    order = sorted(range(n), key=lambda h: prices[h])
    charge = order[:charge_hours]
    discharge = [h for h in reversed(order) if h not in charge][:discharge_hours]
    return {"charge_hours": sorted(charge), "discharge_hours": sorted(discharge)}


def price_spread(prices, e_in_mwh, e_out_mwh, charge_hours=12, discharge_hours=6):
    """ILLUSTRATIVE price-weighted value of one storage cycle on a relative price signal.

    charge_cost_index = e_in valued at the mean of the cheapest charge_hours;
    discharge_value_index = e_out valued at the mean of the priciest discharge_hours.
    value_ratio < 1 means the cycle destroys price-weighted value even before losses
    are counted twice: e_in/e_out already include the RTE. Flat signal -> ratio == RTE."""
    w = dispatch_windows(prices, charge_hours, discharge_hours)
    pc = sum(prices[h] for h in w["charge_hours"]) / charge_hours
    pd = sum(prices[h] for h in w["discharge_hours"]) / discharge_hours
    cost, value = e_in_mwh * pc, e_out_mwh * pd
    return {"charge_hours": w["charge_hours"], "discharge_hours": w["discharge_hours"],
            "charge_price": pc, "discharge_price": pd,
            "charge_cost_index": cost, "discharge_value_index": value,
            "value_ratio": value / cost if cost > 0 else 0.0}


def laes_backup(backup_mw, outage_hours, depth=0.0, cold_reuse=False, reheat_c=40.0, surface=15.0):
    """Size LAES from an outage-duration input. Returns dict incl. sized electricity in."""
    if backup_mw <= 0 or outage_hours <= 0:
        raise ValueError("backup_mw and outage_hours must be positive")
    need = backup_mw * outage_hours
    boost = 0.0
    if cold_reuse:
        scale = max(0.0, min(1.0, (reheat_c - surface) / (WASTE_REHEAT_REF_C - surface)))
        boost = WASTE_REHEAT_BOOST_KWH_PER_KG * scale
    e_in = need / 0.4
    for _ in range(6):   # boil-off depends weakly on size: iterate
        sysm = UndergroundLAESSystem(e_in, depth, 0.0, surface,
                                     discharge_kwh_per_kg=DISCHARGE_KWH_PER_KG + boost)
        r = sysm.simulate()
        e_in *= need / r.electricity_out_mwh
    r = UndergroundLAESSystem(e_in, depth, 0.0, surface,
                              discharge_kwh_per_kg=DISCHARGE_KWH_PER_KG + boost).simulate()
    standby = UndergroundLAESSystem(e_in, depth, 1.0, surface).simulate()
    cold_mwh = r.mass_kg * COLD_TO_HALLS_KWH_PER_KG / 1000.0 if cold_reuse else 0.0
    reheat_mwh_th = r.mass_kg * boost / 1000.0 / WASTE_REHEAT_CONVERSION if cold_reuse else 0.0
    effective = (r.electricity_out_mwh + cold_mwh / CHILLER_COP) / e_in
    flags = list(r.flags)
    if effective > LAES_EFFECTIVE_RTE_BOUND:
        raise ValueError(f"LAES effective RTE {effective:.3f} above ILLUSTRATIVE bound {LAES_EFFECTIVE_RTE_BOUND}")
    rte_guard(effective, "LAES (with cold credit)")
    rte_guard(r.rte, "LAES")
    if cold_reuse:
        flags.append(f"[COLD REUSE] {cold_mwh:.0f} MWh of cold pre-cools the halls during discharge; "
                     f"{reheat_mwh_th:.0f} MWh_th of {reheat_c:.0f} C waste heat reheats air (ILLUSTRATIVE).")
    return {"backup_mw": backup_mw, "outage_hours": outage_hours, "need_mwh_e": need,
            "electricity_in_mwh": e_in, "electricity_out_mwh": r.electricity_out_mwh, "rte": r.rte,
            "effective_rte": effective, "mass_t": r.mass_kg / 1000.0,
            "volume_m3": r.mass_kg / LIQUID_AIR_DENSITY, "boil_off_t_per_day": standby.boil_off_kg_per_day / 1000.0,
            "makeup_mwh_e_per_day": standby.boil_off_kg_per_day * standby.liquefaction_kwh_per_kg / 1000.0,
            "cold_to_halls_mwh": cold_mwh, "reheat_mwh_th": reheat_mwh_th, "boost_kwh_per_kg": boost,
            "flags": flags}


def site_ledger(it_load_mw, depth_m, groundwater_l_s=GROUNDWATER_INFLOW_L_S):
    """Underground site items: ILLUSTRATIVE parasitic MW or flags. Not costs."""
    gw_mw = groundwater_l_s * 9.81 * depth_m / PUMP_EFF / 1e6   # rho*g*h*Q/eta, Q in m3/s * 1000 kg
    return [
        ("Ventilation / smoke control", it_load_mw * VENT_SHARE_OF_IT,
         "ILLUSTRATIVE fan load; fire-zone compartments and smoke extraction need design"),
        ("Egress (staff escape, refuge)", 0.0,
         "FLAG: shafts/ramps and refuge chambers drive layout; no energy number given"),
        ("Groundwater pumping", gw_mw,
         f"ILLUSTRATIVE {groundwater_l_s:g} L/s lifted {depth_m:.0f} m at {PUMP_EFF} pump efficiency"),
        ("Humidity control", HUMIDITY_MW, "ILLUSTRATIVE dehumidification; wet rock is the default"),
        ("Rock stability", ROCK_MONITOR_MW,
         "ILLUSTRATIVE monitoring load; FLAG: span limits and support set by geotechnical survey"),
    ]


@dataclass
class DataCenterResult:
    name: str
    cooling: str
    flows: dict
    numbers: dict
    flags: list


class DataCenter:
    def __init__(self, name="ZUCKY CENTER", it_load_mw=50.0, pue=None, cooling="air",
                 heat_customer=None, battery_hours=12.0, battery_store_c=150.0,
                 battery_medium="hot_water_aquifer", discharge_cycle="orc",
                 discharge_sink_c=15.0, rock_temp_c=35.0, battery_idle_hrs=12.0,
                 laes_backup_mw=None, outage_hours=24.0, laes_cold_reuse=False,
                 molten_salt_mwh_in=0.0, flexible_share=0.0, prices=None,
                 hall_depth_m=800.0, groundwater_l_s=GROUNDWATER_INFLOW_L_S, site_underground=True):
        if cooling not in COOLING:
            raise ValueError(f"cooling must be one of {sorted(COOLING)}")
        if it_load_mw <= 0:
            raise ValueError("it_load_mw must be positive")
        self.name, self.it, self.cooling = name, it_load_mw, cooling
        self.pue = COOLING[cooling]["pue"] if pue is None else pue
        if self.pue < 1.0:
            raise ValueError("PUE cannot be below 1")
        self.customer = heat_customer
        self.battery_hours, self.battery_store_c = battery_hours, battery_store_c
        self.battery_medium, self.discharge_cycle = battery_medium, discharge_cycle
        self.discharge_sink_c, self.rock_temp_c, self.idle = discharge_sink_c, rock_temp_c, battery_idle_hrs
        self.laes_mw = it_load_mw if laes_backup_mw is None else laes_backup_mw
        self.outage_hours, self.cold_reuse = outage_hours, laes_cold_reuse
        self.salt_in, self.flex = molten_salt_mwh_in, flexible_share
        self.prices = DEFAULT_PRICES if prices is None else prices
        self.depth, self.gw = hall_depth_m, groundwater_l_s
        self.underground = site_underground
        # v5: booster always lifts to supply + HX approach (a real heat exchanger needs the margin).

    def simulate(self):
        flags = []
        cool = COOLING[self.cooling]
        t_hot = cool["temp_c"]
        site = site_ledger(self.it, self.depth, self.gw) if self.underground else []
        site_mw = sum(mw for _, mw, _ in site)
        facility_mw = self.it * self.pue + site_mw
        day_e = facility_mw * 24.0                       # all becomes heat
        hot_stream_mw = facility_mw * cool["capture"]
        hot_day = hot_stream_mw * 24.0
        air_day = day_e - hot_day
        # --- heat customer (annual-average day) ---
        sold = hp_e = drawn_for_sale = 0.0
        if self.customer and self.customer.demand_mw_th > 0:
            c = self.customer
            deliver_mw = c.demand_mw_th
            if t_hot >= c.supply_temp_c + HX_APPROACH_C:
                cop_c = None
                draw_mw = min(deliver_mw, hot_stream_mw)
                deliver_mw = draw_mw
                elec_mw = 0.0
            else:
                lift_to = c.supply_temp_c + HX_APPROACH_C
                cop_c = carnot_cop(lift_to + KELVIN, t_hot + KELVIN) * HEAT_PUMP_FRACTION
                cop_c = max(cop_c, 1.0)
                draw_mw = min(deliver_mw * (1 - 1 / cop_c), hot_stream_mw)
                deliver_mw = draw_mw / (1 - 1 / cop_c) if cop_c > 1 else 0.0
                elec_mw = deliver_mw / cop_c if cop_c else 0.0
                flags.append(f"[HEAT SALES] {t_hot:.0f} C loop is below {c.supply_temp_c:.0f} C supply: "
                             f"heat pump COP {cop_c:.2f} (ILLUSTRATIVE {HEAT_PUMP_FRACTION} x Carnot).")
            hrs = 24.0 * c.availability
            sold, drawn_for_sale, hp_e = deliver_mw * hrs, draw_mw * hrs, elec_mw * hrs
        # --- waste-heat battery: stores battery_hours of the remaining hot stream ---
        remaining_mw = hot_stream_mw - drawn_for_sale / 24.0
        cap = remaining_mw * self.battery_hours
        bat = UndergroundCarnotBattery(cap, self.battery_store_c, medium=self.battery_medium,
                                       cold_charge_c=t_hot, cold_discharge_c=self.discharge_sink_c,
                                       rock_temp_c=self.rock_temp_c,
                                       discharge_cycle=self.discharge_cycle).simulate_cycle(
                                           self.battery_hours, self.idle, 6)
        rte_guard(bat.rte, "waste-heat battery")
        lifted = bat.heat_lifted_mwh_th
        if lifted > hot_day - drawn_for_sale + 1e-9:
            raise ValueError("battery lifts more heat than the hot stream provides")
        flags += bat.flags
        # --- LAES backup sized from outage duration ---
        laes = laes_backup(self.laes_mw, self.outage_hours, 0.0, self.cold_reuse, reheat_c=t_hot)
        flags += laes["flags"]
        # --- molten salt tier, per 100 MWh_e in (and actual if enabled) ---
        def salt(e_in):
            probe = UndergroundCarnotBattery(1.0, 565, 25, medium="molten_salt_tank",
                                             rock_temp_c=self.rock_temp_c).simulate_cycle(6, 0, 6)
            return UndergroundCarnotBattery(e_in * probe.cop, 565, 25, medium="molten_salt_tank",
                                            rock_temp_c=self.rock_temp_c).simulate_cycle(6, 12, 6)
        s100 = salt(100.0)
        # --- flexible compute ---
        fx = flex_shift(self.it * self.pue, self.prices, self.flex)
        # --- EnergyFlow columns ---
        rejected_from_stream = day_e - lifted - drawn_for_sale
        flows = {
            "battery": EnergyFlow("Waste-heat battery (cycle)", bat.electricity_in_mwh, 0.0, lifted,
                                  bat.electricity_out_mwh, bat.loss_mwh_th + bat.discharge_rejected_mwh_th,
                                  f"{bat.rte*100:.0f}% RTE"),
            "laes": EnergyFlow("LAES (per backup event)", laes["electricity_in_mwh"], laes["reheat_mwh_th"],
                               laes["cold_to_halls_mwh"], laes["electricity_out_mwh"],
                               laes["electricity_in_mwh"] + laes["reheat_mwh_th"] + laes["cold_to_halls_mwh"]
                               - laes["electricity_out_mwh"], f"{laes['rte']*100:.0f}% RTE"),
            "salt": EnergyFlow("Molten salt (per 100 MWh_e in)", s100.electricity_in_mwh, 0.0, 0.0,
                               s100.electricity_out_mwh, s100.electricity_in_mwh - s100.electricity_out_mwh,
                               f"{s100.rte*100:.0f}% RTE"),
            "dc": EnergyFlow("Data center (per day)", day_e, 0.0, 0.0, 0.0,
                             lifted + drawn_for_sale + rejected_from_stream,
                             f"{lifted:.0f} lifted, {drawn_for_sale:.0f} sold-side, {rejected_from_stream:.0f} rejected"),
        }
        if self.customer and sold > 0:
            flows["heat"] = EnergyFlow("Heat sales (per day)", hp_e, drawn_for_sale, 0.0, 0.0, sold, "")
        for f in flows.values():
            f.assert_balanced()
        salt_day = salt(self.salt_in) if self.salt_in > 0 else None
        grid = day_e + bat.electricity_in_mwh + hp_e + laes["makeup_mwh_e_per_day"] - bat.electricity_out_mwh
        if salt_day:
            grid += salt_day.electricity_in_mwh - salt_day.electricity_out_mwh
        useful = bat.electricity_out_mwh + sold + (salt_day.electricity_out_mwh if salt_day else 0.0)
        heat_env = (rejected_from_stream + flows["battery"].thermal_out
                    + laes["makeup_mwh_e_per_day"]
                    + (salt_day.electricity_in_mwh - salt_day.electricity_out_mwh if salt_day else 0.0))
        numbers = {
            "it_load_mw": self.it, "pue": self.pue, "cooling_temp_c": t_hot,
            "site_parasitic_mw": site_mw, "facility_mw": facility_mw, "facility_mwh_day": day_e,
            "hot_stream_mwh_day": hot_day, "residual_air_mwh_day": air_day,
            "heat_sold_mwh_day": sold, "heat_boosted": cop_c is not None if (self.customer and self.customer.demand_mw_th > 0) else False,
            "booster_cop": (cop_c if (self.customer and self.customer.demand_mw_th > 0) else None), "heat_drawn_for_sale_mwh_day": drawn_for_sale, "heat_pump_e_for_sale": hp_e,
            "battery_capacity_mwh_th": cap, "battery_cop": bat.cop, "battery_carnot_cop": bat.carnot_cop,
            "battery_e_in": bat.electricity_in_mwh, "battery_lifted": lifted,
            "battery_e_out": bat.electricity_out_mwh, "battery_rte": bat.rte, "battery_discharge_eff": bat.discharge_eff,
            "battery_retained": bat.retained_mwh_th, "battery_decay_pct_day": bat.decay_k_per_day * 100,
            "battery_net_loss": bat.electricity_in_mwh - bat.electricity_out_mwh,
            "battery_value": price_spread(self.prices, bat.electricity_in_mwh, bat.electricity_out_mwh),
            "waste_rejected_mwh_day": rejected_from_stream,
            "heat_to_environment_mwh_day": heat_env,
            "useful_output_mwh_day": useful, "net_grid_mwh_day": grid,
            "laes": {k: v for k, v in laes.items() if k != "flags"},
            "salt100_out": s100.electricity_out_mwh, "salt100_rte": s100.rte,
            "salt_carnot": s100.carnot_eff, "salt_eff": s100.discharge_eff,
            "flex_shifted_mwh": fx["shifted_mwh"], "flex_cost_index_base": fx["cost_index_base"],
            "flex_cost_index_flex": fx["cost_index_flex"],
            "site": [(n, mw, note) for n, mw, note in site],
        }
        return DataCenterResult(self.name, self.cooling, flows, numbers, flags)


def first_law_table(res):
    cols = [k for k in ("battery", "laes", "salt", "heat", "dc") if k in res.flows]
    fl = [res.flows[k] for k in cols]
    w = 20
    out = [f"=== FIRST-LAW TABLE: {res.name} ({res.cooling}) -- MWh, in = out asserted per column ==="]
    out.append(f"{'':<16}" + "".join(f"{f.name[:w-1]:>{w}}" for f in fl))
    for label, attr in (("Electric in", "electric_in"), ("Thermal in", "thermal_in"),
                        ("Heat from cold", "heat_from_cold"), ("Electric out", "electric_out"),
                        ("Thermal out", "thermal_out")):
        out.append(f"{label:<16}" + "".join(f"{getattr(f, attr):>{w}.1f}" for f in fl))
    out.append(f"{'In - out':<16}" + "".join(f"{f.total_in()-f.total_out():>{w}.1e}" for f in fl))
    out.append(f"{'Note':<16}" + "".join(f"{f.note[:w-1]:>{w}}" for f in fl))
    return "\n".join(out)


def baseline_center():
    """The v1 PDF: air cooling at 40 C, ORC discharge, no heat customer, 24 h LAES."""
    return DataCenter("ZUCKY v1 baseline", cooling="air", discharge_cycle="orc", outage_hours=24.0)


def evolved_center():
    """Evolved: direct-to-chip 60 C, heat sold to district heating (ILLUSTRATIVE 20 MW_th
    at 55 C, 50% of the year), 30% flexible compute, LAES sized for an ILLUSTRATIVE 8 h
    outage with cold reuse."""
    return DataCenter("ZUCKY v2 evolved", cooling="direct_to_chip", discharge_cycle="orc",
                      heat_customer=HeatCustomer(20.0, 55.0, 0.5), flexible_share=0.30,
                      outage_hours=8.0, laes_cold_reuse=True)


def compare_centers():
    b, e = baseline_center().simulate(), evolved_center().simulate()
    keys = [("Useful output (e + heat sold)", "useful_output_mwh_day"),
            ("  battery electricity out", "battery_e_out"), ("  heat sold", "heat_sold_mwh_day"),
            ("Net grid purchases", "net_grid_mwh_day"), ("Facility waste heat rejected", "waste_rejected_mwh_day"),
            ("All heat to environment", "heat_to_environment_mwh_day"),
            ("Battery RTE", "battery_rte"), ("Flexible MWh shifted", "flex_shifted_mwh")]
    out = ["=== BASELINE vs EVOLVED (MWh per annual-average day unless noted) ===",
           f"{'':<32}{'baseline':>14}{'evolved':>14}"]
    for label, k in keys:
        fmt = (lambda v: f"{v*100:.1f}%") if k == "battery_rte" else (lambda v: f"{v:.0f}")
        out.append(f"{label:<32}{fmt(b.numbers[k]):>14}{fmt(e.numbers[k]):>14}")
    out.append(f"{'LAES electricity in (sized)':<32}{b.numbers['laes']['electricity_in_mwh']:>14.0f}"
               f"{e.numbers['laes']['electricity_in_mwh']:>14.0f}")
    out.append("No law broken: every column balances, every RTE <= 100%. Economics: not claimed here.")
    return "\n".join(out), b, e


def scenario_numbers():
    _, b, e = compare_centers()
    return {"baseline": b.numbers, "evolved": e.numbers,
            "flows": {n: {k: {a: getattr(f, a) for a in ("electric_in", "thermal_in", "heat_from_cold",
                                                          "electric_out", "thermal_out")}
                          for k, f in r.flows.items()} for n, r in (("baseline", b), ("evolved", e))},
            "flags": {"baseline": b.flags, "evolved": e.flags},
            "sweep": fraction_sweep(),
            "cooling": COOLING, "climate": climate_numbers(), "v4": v4_numbers()}


def example_center():
    txt, b, e = compare_centers()
    return "\n\n".join([txt, first_law_table(b), first_law_table(e), report_sweep(fraction_sweep()),
                        "PROTOTYPE FOR EXPLORATION, NOT A DESIGN. ILLUSTRATIVE numbers are not measured data. "
                        "The ledger reports; it does not rule."])


# ======================= tests =======================
def _tests_sco2(check, raises):
    G = UndergroundSCO2Generator
    c = G.estimate_carnot_efficiency
    check("carnot 600/300 = 0.5", abs(c(600, 300) - 0.5) < 1e-12)
    check("carnot equal = 0", c(300, 300) == 0.0)
    check("carnot inverted = 0", c(300, 600) == 0.0)
    check("carnot in (0,1)", 0 < c(823.15, 298.15) < 1)
    check("negative depth rejected", raises(lambda: G(-1, 550, 15)))
    check("low gradient rejected", raises(lambda: G(800, 550, 15, gradient_c_per_km=5)))
    check("high gradient rejected", raises(lambda: G(800, 550, 15, gradient_c_per_km=90)))
    check("bad sink rejected", raises(lambda: G(800, 550, 15, sink="ocean")))
    check("hot below cold rejected", raises(lambda: G(800, 20, 15)))
    check("zero heat rejected", raises(lambda: G(800, 550, 15, heat_input_mw_th=0)))
    check("negative approach rejected", raises(lambda: G(800, 550, 15, approach_c=-1)))
    g = G(800, 550, 15)
    check("rock temp 800m = 35", abs(g.calculate_geothermal_temp() - 35.0) < 1e-9)
    check("gradient param used", abs(G(1000, 550, 15, gradient_c_per_km=50).calculate_geothermal_temp() - 65) < 1e-9)
    check("surface sink = 25", abs(g.cold_side_c() - 25.0) < 1e-9)
    check("rock sink = 45", abs(G(800, 550, 15, sink="rock").cold_side_c() - 45.0) < 1e-9)
    check("rock warms with years", G(800, 550, 15, sink="rock", years_operating=5).cold_side_c() > 45)
    check("surface ignores years", G(800, 550, 15, years_operating=5).cold_side_c() == 25)
    s, r = g.simulate(), G(800, 550, 15, sink="rock").simulate()
    check("surface beats rock", s.sco2_eff > r.sco2_eff)
    check("surface flags critical", any("CRITICAL" in f for f in s.flags))
    check("rock 45C flags caution", any("CAUTION" in f for f in r.flags))
    check("rock deep flags warning", any("WARNING" in f for f in G(1500, 550, 15, sink="rock").simulate().flags))
    check("ok band", any("[OK]" in f for f in G(800, 550, 25, approach_c=10).simulate().flags))
    for res in (s, r):
        check("net <= gross", res.net_mw_e <= res.gross_mw_e)
        check("net >= 0", res.net_mw_e >= 0)
        check("eff = 0.48 carnot", abs(res.sco2_eff - 0.48 * res.carnot_eff) < 1e-12)
    check("parasitic grows with depth", G(2000, 550, 15).simulate().parasitic_mw_e > G(100, 550, 15).simulate().parasitic_mw_e)
    check("800m carnot ~0.638", abs(s.carnot_eff - 0.6378) < 0.001)
    check("800m eff 25-35%", 0.25 < s.sco2_eff < 0.35)
    check("800m net 2.5-3.5 MW", 2.5 < s.net_mw_e < 3.5)
    check("returns dataclass", isinstance(s, SimulationResult))
    check("STANDING is False", STANDING is False)
    check("report has net", "Net Electric" in report_sco2(s))


def _tests_battery(check, raises):
    B = UndergroundCarnotBattery
    eta_c = UndergroundSCO2Generator.estimate_carnot_efficiency
    # thermodynamic bound
    for th, tc in ((838.15, 288.15), (423.15, 288.15), (600, 300)):
        check(f"carnot COP x carnot eta = 1 ({th})", abs(carnot_cop(th, tc) * eta_c(th, tc) - 1) < 1e-12)
    check("carnot COP 600/300 = 2", abs(carnot_cop(600, 300) - 2) < 1e-12)
    check("carnot COP inverted rejected", raises(lambda: carnot_cop(300, 600)))
    ok, prod, msg = check_consistency(3.5, 0.52)
    check("original combo flagged", not ok and "INCONSISTENT" in msg)
    check("original product 1.82", abs(prod - 1.82) < 1e-9)
    check("realistic combo ok", check_consistency(1.7, 0.15)[0])
    t = implied_store_temp_c(3.5, 15)
    check("implied temp ~130 C", 125 < t < 135)
    check("implied carnot eff < 0.52", eta_c(t + KELVIN, 15 + KELVIN) < 0.52)
    check("original naive RTE ~181%", abs((50 - 50 * 0.01 * 0.5) * 0.52 / (50 / 3.5) - 1.811) < 0.001)
    # guard
    perfect = B(50, 400, 15, heat_pump_fraction=1.0, loss_k_per_day_per_c=0)
    try:
        # patch the shared model to an impossible >1 fraction: guard must fire
        orig_sim = UndergroundSCO2Generator.simulate
        def bad(self):
            r = orig_sim(self); r.sco2_eff = r.carnot_eff * 1.5; return r
        UndergroundSCO2Generator.simulate = bad
        check("guard raises RTE > 100%", raises(lambda: perfect.simulate_cycle(6, 0, 6)))
    finally:
        UndergroundSCO2Generator.simulate = orig_sim
    rp = perfect.simulate_cycle(6, 0, 6)
    check("perfect HP + 0.48 turbine RTE = 0.48", abs(rp.rte - 0.48) < 1e-9)
    # decay
    b = B(50, 565, 15, medium="molten_salt_tank", rock_temp_c=35)
    check("k scales with gap", abs(b.decay_k() - 2e-5 * 530) < 1e-12)
    check("no idle no loss", abs(b.retained(0) - 50) < 1e-12)
    check("exp decay 1 day", abs(b.retained(24) - 50 * math.exp(-b.decay_k())) < 1e-12)
    check("decay multiplicative", abs(b.retained(48) / 50 - (b.retained(24) / 50) ** 2) < 1e-12)
    check("hotter store loses faster", B(50, 500, 15, rock_temp_c=35).decay_k() > B(50, 200, 15, rock_temp_c=35).decay_k())
    check("store at rock temp: no loss", B(50, 35, 15, rock_temp_c=35).decay_k() == 0)
    check("longer idle lower RTE", b.simulate_cycle(6, 240, 6).rte < b.simulate_cycle(6, 12, 6).rte)
    # media
    check("bad medium rejected", raises(lambda: B(50, 300, 15, medium="volcano")))
    check("aquifer max enforced", raises(lambda: B(50, 200, 15, medium="hot_water_aquifer")))
    check("salt tank max enforced", raises(lambda: B(50, 600, 15, medium="molten_salt_tank")))
    check("hot rock 600 ok", B(50, 600, 15, medium="hot_rock").storage_temp_c == 600)
    sc = B(50, 70, 15, medium="salt_cavern").simulate_cycle(6, 12, 6)
    check("salt cavern warns", any("UNSUITABLE" in f for f in sc.flags))
    check("other media no unsuitable", not any("UNSUITABLE" in f for f in b.simulate_cycle(6, 12, 6).flags))
    check("store below cold rejected", raises(lambda: B(50, 10, 15)))
    check("zero capacity rejected", raises(lambda: B(0, 300, 15)))
    # power from durations
    r = b.simulate_cycle(6, 12, 4)
    check("charge MW = MWh/h", abs(r.charge_power_mw - r.electricity_in_mwh / 6) < 1e-12)
    check("discharge MW = MWh/h", abs(r.discharge_power_mw - r.electricity_out_mwh / 4) < 1e-12)
    check("zero charge hrs rejected", raises(lambda: b.simulate_cycle(0, 12, 6)))
    check("negative idle rejected", raises(lambda: b.simulate_cycle(6, -1, 6)))
    # COP floor
    check("high lift falls back to COP 1", r.cop == 1.0 and any("resistive" in f for f in r.flags))
    aq = B(50, 150, 15, medium="hot_water_aquifer").simulate_cycle(6, 12, 6)
    check("aquifer uses heat pump COP > 1", aq.cop > 1)
    check("COP = 0.55 x carnot", abs(aq.cop - 0.55 * aq.carnot_cop) < 1e-12)
    # link to sco2_generator
    check("discharge eff = shared 0.48 x carnot", abs(r.discharge_eff - FRACTION_OF_CARNOT * r.carnot_eff) < 1e-12)
    check("carnot matches generator", abs(r.carnot_eff - eta_c(565 + KELVIN, 15 + KELVIN)) < 1e-12)
    check("critical flag passed through", any("CRITICAL POINT" in f for f in r.flags))
    check("warm cold side gets generator caution",
          any("CAUTION" in f for f in B(50, 400, 42).simulate_cycle(6, 0, 6).flags))
    # all RTE <= 1 across sweep
    sweep_ok = all(B(50, t, 15, medium="hot_rock", heat_pump_fraction=f).simulate_cycle(6, 0, 6).rte <= 1
                   for t in range(30, 601, 30) for f in (0.3, 0.55, 1.0))
    check("RTE <= 100% across sweep", sweep_ok)
    check("returns dataclass", isinstance(r, BatteryResult))
    check("STANDING is False", STANDING is False)
    check("report has RTE", "Round-Trip" in report_battery(r))


def _tests_laes(check, raises):
    L = UndergroundLAESSystem
    # min-work guard
    check("liquefaction below 0.2 rejected", raises(lambda: L(50, liquefaction_kwh_per_kg=0.19)))
    check("liquefaction 0.2 accepted", L(50, liquefaction_kwh_per_kg=0.2).w_liq == 0.2)
    check("default with recycle 0.35", L(50).w_liq == 0.35)
    check("no recycle 0.5", L(50, cold_recycle=False).w_liq == 0.5)
    check("no recycle lowers RTE", L(50, cold_recycle=False).simulate().rte < L(50).simulate().rte)
    check("bad inputs rejected", raises(lambda: L(0)) and raises(lambda: L(50, depth_meters=-1))
          and raises(lambda: L(50, u_w_per_m2k=-1)) and raises(lambda: L(50, cavern="moon")))
    # mass balance
    r = L(50, 0, 2).simulate()
    check("mass = MWh*1000/w", abs(r.mass_kg - 50000 / 0.35) < 1e-6)
    check("retained + lost = mass", abs(r.retained_kg + r.boil_off_kg - r.mass_kg) < 1e-6)
    check("boil-off = rate x days", abs(r.boil_off_kg - r.boil_off_kg_per_day * 2) < 1e-6)
    huge = L(50, 0, 10000, u_w_per_m2k=50).simulate()
    check("physical cap: lost <= stored", huge.boil_off_kg == huge.mass_kg and huge.retained_kg == 0)
    check("full boil-off flagged, zero out", huge.electricity_out_mwh == 0 and any("Entire" in f for f in huge.flags))
    # boil-off physics
    base = L(50, 0, 1, tank_area_m2=1000)
    check("boil-off linear in U", abs(L(50, 0, 1, tank_area_m2=1000, u_w_per_m2k=0.04).boil_off_kg_per_day()
                                     - 2 * base.boil_off_kg_per_day()) < 1e-9)
    check("boil-off linear in area", abs(L(50, 0, 1, tank_area_m2=2000).boil_off_kg_per_day()
                                        - 2 * base.boil_off_kg_per_day()) < 1e-9)
    gap0 = 15 - LIQUID_AIR_TEMP_C
    check("boil-off formula UA dT / L", abs(base.boil_off_kg_per_day() - 0.02 * 1000 * gap0 * 86400 / 199000) < 1e-9)
    check("boil-off grows with gap (depth)", L(50, 2000, 1, tank_area_m2=1000).boil_off_kg_per_day() > base.boil_off_kg_per_day())
    check("rock temp from shared gradient", L(50, 600).rock_temp_c() == geothermal_temp_c(15, 600))
    check("zero U no loss", L(50, 0, 5, u_w_per_m2k=0).simulate().boil_off_kg == 0)
    # no double efficiency
    r0 = L(50, 0, 0).simulate()
    check("out = mass x w_dis (applied once)", abs(r0.electricity_out_mwh - r0.mass_kg * 0.15 / 1000) < 1e-9)
    check("RTE = w_dis / w_liq with no losses", abs(r0.rte - 0.15 / 0.35) < 1e-12)
    check("RTE = out / in", abs(r0.rte - r0.electricity_out_mwh / 50) < 1e-12)
    # hot store reuse
    h = L(50, 0, 0, hot_store_temp_c=450).simulate()
    check("hot store boost at ref = 0.05", abs(h.hot_store_boost_kwh_per_kg - 0.05) < 1e-12)
    check("hot store raises RTE", h.rte > r0.rte)
    check("hot store decays with time", L(50, 0, 30, hot_store_temp_c=450).hot_store_boost() < 0.05)
    check("hot store uses battery media limits", raises(lambda: L(50, hot_store_temp_c=200, hot_store_medium="hot_water_aquifer")))
    # RTE guard
    check("discharge > liquefaction raises", raises(lambda: L(50, 0, 0, liquefaction_kwh_per_kg=0.2, discharge_kwh_per_kg=0.25).simulate()))
    check("rte_guard shared", raises(lambda: rte_guard(1.01, "x")) and rte_guard(1.0, "x") == 1.0)
    # depth / salt warnings
    d = L(50, 600, 1).simulate()
    check("depth flag", any("[DEPTH]" in f for f in d.flags))
    check("no depth flag at surface", not any("[DEPTH]" in f for f in r0.flags))
    check("depth adds pump cost", d.pump_mwh > 0 and r.pump_mwh == 0)
    check("salt cavern + 450 C hot store unsuitable",
          any("UNSUITABLE" in f for f in L(50, 600, 1, hot_store_temp_c=450, cavern="salt").simulate().flags))
    check("salt cavern without hot store ok", not any("UNSUITABLE" in f for f in L(50, 600, 1, cavern="salt").simulate().flags))
    # notes, not verdicts
    check("below 50% note", any("50-60%" in f for f in r0.flags))
    check("no note at good RTE", not any("50-60%" in f for f in h.flags))
    check("no 'viable' verdicts", "viable" not in report_laes(d).lower() and "viable" not in compare().lower())
    # original naive 58% not used
    naive = (1 - (-196 + KELVIN) / (450 + KELVIN)) * 0.65
    check("naive formula ~58%", abs(naive - 0.5806) < 0.001)
    orig = L(100, 600, 3, hot_store_temp_c=450, cavern="salt").simulate()
    check("original example not 58%", abs(orig.rte - naive) > 0.01)
    check("returns dataclass", isinstance(orig, LAESResult))
    check("compare has three systems", all(k in compare() for k in ("sCO2", "Carnot", "LAES")))


def _tests_zucky(check, raises):
    B, G = UndergroundCarnotBattery, UndergroundSCO2Generator
    # split temperatures
    old = B(684, 150, 15, medium="hot_water_aquifer", rock_temp_c=35).simulate_cycle(12, 12, 6)
    same = B(684, 150, 15, medium="hot_water_aquifer", rock_temp_c=35, cold_charge_c=15, cold_discharge_c=15).simulate_cycle(12, 12, 6)
    check("old cold_temp_c still works = explicit split", abs(old.rte - same.rte) < 1e-12)
    sp = B(684, 150, medium="hot_water_aquifer", rock_temp_c=35, cold_charge_c=40, cold_discharge_c=15,
           discharge_cycle="orc").simulate_cycle(12, 12, 6)
    check("split: COP uses 40 C source (3.85)", abs(sp.carnot_cop - 423.15 / 110) < 1e-9)
    check("split: used COP 2.12", abs(sp.cop - 0.55 * 423.15 / 110) < 1e-9 and 2.1 < sp.cop < 2.13)
    check("split: discharge uses 15 C sink", abs(sp.carnot_eff - (1 - 288.15 / 423.15)) < 1e-12)
    check("split: PDF RTE ~37%", 0.36 < sp.rte < 0.38)
    check("split: PDF 323 MWh_e in", abs(sp.electricity_in_mwh - 684 / sp.cop) < 1e-9 and 320 < sp.electricity_in_mwh < 325)
    check("split: lifted = cap - e_in (~361)", abs(sp.heat_lifted_mwh_th - (684 - sp.electricity_in_mwh)) < 1e-9)
    check("split fields reported", sp.cold_charge_c == 40 and sp.cold_discharge_c == 15)
    check("store below either cold side rejected", raises(lambda: B(50, 100, cold_charge_c=120, cold_discharge_c=15)))
    check("partial override keeps cold_temp_c for the other", B(50, 150, 20, medium="hot_water_aquifer", cold_charge_c=40).cold_discharge_c == 20)
    # ORC
    check("ORC eff = 0.55 x carnot", abs(sp.discharge_eff - ORC_FRACTION_OF_CARNOT * sp.carnot_eff) < 1e-12)
    check("ORC flag", any("[ORC]" in f for f in sp.flags))
    check("ORC has no CO2 critical flag", not any("CRITICAL POINT" in f for f in sp.flags))
    check("ORC separate from sCO2 0.48", ORC_FRACTION_OF_CARNOT != FRACTION_OF_CARNOT)
    check("bad cycle rejected", raises(lambda: B(50, 150, 15, medium="hot_water_aquifer", discharge_cycle="stirling")))
    check("ORC beats sCO2 at same temps", sp.discharge_eff > B(684, 150, medium="hot_water_aquifer", cold_charge_c=40,
          cold_discharge_c=15).simulate_cycle(12, 12, 6).discharge_eff)
    # warming cap
    w10 = G(800, 550, 15, sink="rock", years_operating=10).cold_side_c()
    w50 = G(800, 550, 15, sink="rock", years_operating=50)
    check("warming capped at horizon", abs(w50.cold_side_c() - w10) < 1e-12 and abs(w10 - 60) < 1e-9)
    check("horizon flag beyond window", any("[HORIZON]" in f for f in w50.simulate().flags))
    check("no horizon flag inside window", not any("[HORIZON]" in f for f in G(800, 550, 15, sink="rock", years_operating=5).simulate().flags))
    check("no inversion at 1000 years", G(800, 550, 15, sink="rock", years_operating=1000).simulate().net_mw_e > 0)
    check("sink near store flagged", any("SINK NEAR STORE" in f for f in G(800, 90, 15, sink="rock", years_operating=10).simulate().flags))
    check("sink far from store not flagged", not any("SINK NEAR STORE" in f for f in G(800, 550, 15).simulate().flags))
    # sweep
    sw = fraction_sweep()
    check("sweep has 0.40/0.48/0.55", [r["fraction"] for r in sw] == [0.40, 0.48, 0.55])
    check("sweep monotonic", sw[0]["sco2_eff"] < sw[1]["sco2_eff"] < sw[2]["sco2_eff"])
    check("sweep 0.48 = default model", abs(sw[1]["sco2_eff"] - G(800, 550, 15).simulate().sco2_eff) < 1e-12)
    check("sweep does not mutate global", FRACTION_OF_CARNOT == 0.48)
    check("bad fraction rejected", raises(lambda: G(800, 550, 15, fraction_of_carnot=1.2).simulate()))
    # composer energy balance
    b, e = baseline_center().simulate(), evolved_center().simulate()
    for res in (b, e):
        for k, f in res.flows.items():
            check(f"{res.name} {k} column in = out", abs(f.total_in() - f.total_out()) < 1e-6 and f.assert_balanced())
    bad = EnergyFlow("bad", electric_in=10, electric_out=11)
    check("unbalanced column raises", raises_assert(bad.assert_balanced))
    n = b.numbers
    check("dc: lifted + sold + rejected = facility heat",
          abs(n["battery_lifted"] + n["heat_drawn_for_sale_mwh_day"] + n["waste_rejected_mwh_day"] - n["facility_mwh_day"]) < 1e-6)
    check("baseline lifted < stored (not '684 stored')", n["battery_lifted"] < n["battery_capacity_mwh_th"])
    check("baseline rejected > half the stream", n["waste_rejected_mwh_day"] > n["facility_mwh_day"] / 2)
    check("baseline battery RTE ~37%", 0.36 < n["battery_rte"] < 0.38)
    check("baseline grid heat = all heat (no sales)", abs(n["heat_to_environment_mwh_day"] - n["net_grid_mwh_day"]) < 1e-6)
    check("evolved: environment = grid - heat sold",
          abs(e.numbers["heat_to_environment_mwh_day"] - (e.numbers["net_grid_mwh_day"] - e.numbers["heat_sold_mwh_day"])) < 1e-6)
    check("table emitted from flows", "In - out" in first_law_table(b) and "Data center" in first_law_table(b))
    # RTE guard in composer
    check("composer rte_guard on battery", b.numbers["battery_rte"] <= 1 and e.numbers["battery_rte"] <= 1)
    check("LAES effective RTE <= bound", e.numbers["laes"]["effective_rte"] <= LAES_EFFECTIVE_RTE_BOUND)
    # heat sales
    check("baseline sells no heat", n["heat_sold_mwh_day"] == 0)
    check("evolved sells 20 MW x 12 h", abs(e.numbers["heat_sold_mwh_day"] - 240) < 1e-6)
    check("60 C loop feeds 55 C directly (no heat pump)", e.numbers["heat_pump_e_for_sale"] == 0)
    hp = DataCenter(cooling="air", heat_customer=HeatCustomer(20, 70, 1.0)).simulate()
    check("40 C loop to 70 C needs heat pump", hp.numbers["heat_pump_e_for_sale"] > 0 and any("HEAT SALES" in f for f in hp.flags))
    check("heat pump sale balanced", hp.flows["heat"].assert_balanced())
    check("bad availability rejected", raises(lambda: HeatCustomer(10, 60, 1.5)))
    check("evolved useful output > baseline", e.numbers["useful_output_mwh_day"] > n["useful_output_mwh_day"])
    # flexible load
    fx = flex_shift(50, DEFAULT_PRICES, 0.3)
    check("flex conserves energy", abs(sum(fx["hourly_mw"]) - 50 * 24) < 1e-9)
    check("flex lowers cost index", fx["cost_index_flex"] < fx["cost_index_base"])
    check("flex respects headroom", max(fx["hourly_mw"]) <= 50 * FLEX_HEADROOM + 1e-9)
    check("zero flex no shift", flex_shift(50, DEFAULT_PRICES, 0)["shifted_mwh"] == 0)
    check("flat prices no shift", flex_shift(50, [1.0] * 24, 0.5)["shifted_mwh"] == 0)
    check("bad flex share rejected", raises(lambda: flex_shift(50, DEFAULT_PRICES, 1.5)))
    # outage-based LAES sizing
    l24, l8 = laes_backup(50, 24), laes_backup(50, 8)
    check("LAES delivers outage MWh", abs(l24["electricity_out_mwh"] - 1200) < 1e-6 and abs(l8["electricity_out_mwh"] - 400) < 1e-6)
    check("LAES 24 h ~2800 MWh_e in (PDF)", 2750 < l24["electricity_in_mwh"] < 2850)
    check("shorter outage smaller store", l8["electricity_in_mwh"] < l24["electricity_in_mwh"] / 2.9)
    check("bad outage rejected", raises(lambda: laes_backup(50, 0)))
    # cold reuse bounds
    lc = laes_backup(50, 8, cold_reuse=True, reheat_c=60)
    check("cold reuse raises RTE", lc["rte"] > l8["rte"])
    check("cold reuse effective >= plain", lc["effective_rte"] > lc["rte"])
    check("cold reuse within bound and <= 100%", lc["effective_rte"] <= LAES_EFFECTIVE_RTE_BOUND <= 1)
    check("cold reuse reheat colder gives less", laes_backup(50, 8, cold_reuse=True, reheat_c=40)["rte"] < lc["rte"])
    check("reheat at ambient no boost", laes_backup(50, 8, cold_reuse=True, reheat_c=15)["boost_kwh_per_kg"] == 0)
    check("waste reheat conversion below Carnot 60->15", WASTE_REHEAT_CONVERSION < carnot_efficiency(333.15, 288.15))
    # site items + wording
    site = dict((nm, mw) for nm, mw, _ in site_ledger(50, 800))
    check("site has five items", len(site) == 5)
    check("groundwater = rho g h Q / eta", abs(site["Groundwater pumping"] - 50 * 9.81 * 800 / 0.7 / 1e6) < 1e-12)
    check("deeper halls pump more", dict((a, m) for a, m, _ in site_ledger(50, 150))["Groundwater pumping"] < site["Groundwater pumping"])
    txt = example_center()
    check("no 'viable' verdicts", "viable" not in txt.lower())
    check("'No law broken' language", "No law broken" in txt)
    check("cooling types ILLUSTRATIVE set", set(COOLING) == {"air", "direct_to_chip", "immersion"})
    check("bad cooling rejected", raises(lambda: DataCenter(cooling="fans")))


def _tests_v5(check, raises):
    # price-timed dispatch windows on the ILLUSTRATIVE signal:
    # six 0.6 (h0-5), four 0.9 (h6-9), six 1.0 (h10-15), four 1.1 (h20-23), four 1.8 (h16-19)
    w = dispatch_windows(DEFAULT_PRICES, 12, 6)
    check("charge = 12 cheapest hours", w["charge_hours"] == list(range(12)))
    check("discharge = 6 priciest non-charge hours", w["discharge_hours"] == [16, 17, 18, 19, 22, 23])
    check("windows do not overlap", not set(w["charge_hours"]) & set(w["discharge_hours"]))
    check("bad windows rejected", raises(lambda: dispatch_windows(DEFAULT_PRICES, 20, 6))
          and raises(lambda: dispatch_windows(DEFAULT_PRICES, 0, 6)))
    ps = price_spread(DEFAULT_PRICES, 323.0, 120.0)
    check("flat signal: value ratio == RTE", abs(price_spread([1.0] * 24, 323.0, 120.0)["value_ratio"] - 120.0 / 323.0) < 1e-12)
    check("value ratio = value / cost", abs(ps["value_ratio"] - ps["discharge_value_index"] / ps["charge_cost_index"]) < 1e-12)
    check("default spread: charge cheaper than discharge", ps["charge_price"] < ps["discharge_price"])
    b = baseline_center().simulate()
    bv = b.numbers["battery_value"]
    check("composer carries battery_value",
          abs(bv["charge_cost_index"] - price_spread(DEFAULT_PRICES, b.numbers["battery_e_in"],
                                                     b.numbers["battery_e_out"])["charge_cost_index"]) < 1e-9)
    check("baseline battery value_ratio in (0,1): arbitrage alone does not carry it", 0 < bv["value_ratio"] < 1)
    # booster fix: composer COP must equal booster_cop(loop, supply), i.e. lift to supply + approach
    hp = DataCenter(cooling="air", heat_customer=HeatCustomer(20, 70, 1.0)).simulate()
    check("composer booster COP == booster_cop(loop, supply)", abs(hp.numbers["booster_cop"] - booster_cop(40.0, 70.0)) < 1e-12)
    check("booster lift includes the HX approach", abs(booster_cop(40, 55)
          - max(1.0, HEAT_PUMP_FRACTION * carnot_cop(55 + HX_APPROACH_C + KELVIN, 40 + KELVIN))) < 1e-12)
    import json
    check("--json serializes (tuple keys stringified)",
          "subarctic_hybrid/direct_to_chip" in json.dumps(_jsonable(scenario_numbers()), default=str))


def raises_assert(fn):
    try: fn(); return False
    except AssertionError: return True


def run_tests():
    passed = failed = 0
    def check(name, cond):
        nonlocal passed, failed
        if cond: passed += 1
        else:
            failed += 1; print(f"FAIL: {name}")
    def raises(fn):
        try: fn(); return False
        except ValueError: return True
    for name, fn in (("sCO2", _tests_sco2), ("battery", _tests_battery), ("LAES", _tests_laes), ("zucky", _tests_zucky),
                     ("climate v3", _tests_climate), ("v4 supply", _tests_v4),
                     ("v5 dispatch", _tests_v5),
                     ("design doc", _tests_doc)):
        p0, f0 = passed, failed
        fn(check, raises)
        print(f"  {name}: {passed-p0} passed, {failed-f0} failed")
    check("critical flag shared", "CRITICAL POINT" in critical_point_flag(25))
    print(f"{passed} passed, {failed} failed")
    return failed == 0


# ======================= v3: climate / site layer =======================
# Fused from the SubArcticCoolingSimulator idea, patched. Locations are GENERIC; every
# profile below is ILLUSTRATIVE, not measured climate data for any real site.
MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
HOURS_PER_YEAR = 8760.0
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
BASELINE_FLAT_PUE = 1.40      # ILLUSTRATIVE: the original's flat warm-climate PUE, kept only as a parameter
HEATING_BASE_C = 18.0         # ILLUSTRATIVE heating-degree base temperature
HEAT_BASE_MW = 2.0            # ILLUSTRATIVE year-round district heat (hot water) demand
HEAT_MW_PER_K = 1.2           # ILLUSTRATIVE extra demand per C below HEATING_BASE_C
HEAT_NETWORK_MAX_MW = 30.0    # ILLUSTRATIVE network capacity cap
HEAT_SUPPLY_C = 55.0          # ILLUSTRATIVE low-temperature network supply (as v2)
WATER_SINKS = {               # ILLUSTRATIVE cold-water sinks: (annual mean C, seasonal amplitude C, coldest month idx)
    "fjord_water": (6.0, 2.0, 2),   # deep-intake sea water lags the air (coldest ~Mar)
    "lake_water": (5.0, 3.0, 2),    # deep lake intake (ILLUSTRATIVE)
}
SINK_DEPTH_M = 150.0          # ILLUSTRATIVE depth of the rock heat-sink loop (uses geothermal_temp_c)

# Continuous ILLUSTRATIVE PUE(T_sink) curves: overhead = floor in free cooling, smooth ramp
# to mech between free_c and full_c, then a gentle mechanical-assist slope above full_c.
# Hotter liquid loops keep free cooling to much warmer sinks.
# For liquid cooling only the captured share (COOLING[...]["capture"]) rides the liquid curve;
# the residual air share still follows the air curve.
PUE_CURVES = {
    "air":            {"floor": 0.06, "free_c": 12.0, "full_c": 28.0, "mech": 0.30, "slope": 0.006},
    "direct_to_chip": {"floor": 0.03, "free_c": 30.0, "full_c": 45.0, "mech": 0.12, "slope": 0.004},
    "immersion":      {"floor": 0.02, "free_c": 36.0, "full_c": 50.0, "mech": 0.08, "slope": 0.003},
}


def validate_hours(hours, total=HOURS_PER_YEAR):
    """Season/month hours must be non-negative and sum to a year (8760 h)."""
    hours = list(hours)
    if any(h < 0 for h in hours):
        raise ValueError("hours cannot be negative")
    if abs(sum(hours) - total) > 1e-6:
        raise ValueError(f"hours sum to {sum(hours):g}, must be {total:g}")
    return hours


def month_hours():
    return validate_hours([d * 24.0 for d in MONTH_DAYS])


def sinusoid_months(mean_c, amplitude_c, coldest_month=0):
    """12 monthly means from an annual mean and seasonal amplitude (ILLUSTRATIVE)."""
    if amplitude_c < 0:
        raise ValueError("amplitude must be >= 0")
    return [mean_c - amplitude_c * math.cos(2 * math.pi * (m - coldest_month) / 12) for m in range(12)]


@dataclass
class ClimateProfile:
    name: str
    monthly_c: list
    diurnal_amp_c: float = 4.0   # ILLUSTRATIVE half-swing of the day/night cycle
    note: str = "ILLUSTRATIVE generic profile, not measured data for any real site"

    def __post_init__(self):
        if len(self.monthly_c) != 12:
            raise ValueError("monthly_c needs 12 values")
        if self.diurnal_amp_c < 0:
            raise ValueError("diurnal amplitude must be >= 0")

    def hourly(self, m):
        """24 representative hourly temperatures for month m (warmest ~15:00)."""
        t = self.monthly_c[m]
        return [t - self.diurnal_amp_c * math.cos(2 * math.pi * (h - 3) / 24) for h in range(24)]

    def mean(self):
        return sum(t * d for t, d in zip(self.monthly_c, MONTH_DAYS)) / sum(MONTH_DAYS)


PROFILES = {   # ALL ILLUSTRATIVE and generic
    "sub_arctic": ClimateProfile("generic sub-arctic coast", sinusoid_months(1.0, 11.0), 3.0),
    "temperate":  ClimateProfile("generic temperate", sinusoid_months(10.0, 8.0), 4.0),
    "warm":       ClimateProfile("generic warm", sinusoid_months(22.0, 7.0), 6.0),
}


def pue_curve(sink_c, cooling="air"):
    """Continuous, monotone non-decreasing ILLUSTRATIVE PUE vs heat-sink temperature."""
    if cooling not in PUE_CURVES:
        raise ValueError(f"cooling must be one of {sorted(PUE_CURVES)}")
    def overhead(c):
        x = (sink_c - c["free_c"]) / (c["full_c"] - c["free_c"])
        x = max(0.0, min(1.0, x))
        ramp = x * x * (3 - 2 * x)   # smoothstep: continuous, flat at both ends
        return c["floor"] + (c["mech"] - c["floor"]) * ramp + c["slope"] * max(0.0, sink_c - c["full_c"])
    if cooling == "air":
        return 1.0 + overhead(PUE_CURVES["air"])
    cap = COOLING[cooling]["capture"]
    return 1.0 + cap * overhead(PUE_CURVES[cooling]) + (1 - cap) * overhead(PUE_CURVES["air"])


def legacy_step_pue(ambient_c):
    """The ORIGINAL step function, kept only to reproduce and test the original numbers."""
    if ambient_c <= 0.0:
        return 1.04
    if ambient_c <= 15.0:
        return 1.10
    return 1.20


def sink_temps(profile, m, sinks=("ambient_air",), surface_c=None):
    """Hourly effective sink temperatures: the coldest available sink each hour."""
    if not sinks:
        raise ValueError("need at least one sink")
    out = []
    for t_air in profile.hourly(m):
        cands = []
        for s in sinks:
            if s == "ambient_air":
                cands.append(t_air)
            elif s == "rock":
                cands.append(geothermal_temp_c(profile.mean() if surface_c is None else surface_c, SINK_DEPTH_M))
            elif s in WATER_SINKS:
                mu, amp, cold = WATER_SINKS[s]
                cands.append(sinusoid_months(mu, amp, cold)[m])
            else:
                raise ValueError(f"unknown sink {s}; use ambient_air, rock or {sorted(WATER_SINKS)}")
        out.append(min(cands))
    return out


# --- v4: weather-compensated district-heating supply temperature (ALL ILLUSTRATIVE) ---
SUPPLY_SUMMER_C = 55.0        # ILLUSTRATIVE supply at/above SUPPLY_WARM_AMBIENT_C
SUPPLY_WINTER_C = 80.0        # ILLUSTRATIVE supply at/below SUPPLY_COLD_AMBIENT_C
SUPPLY_WARM_AMBIENT_C = 15.0  # ILLUSTRATIVE
SUPPLY_COLD_AMBIENT_C = -15.0 # ILLUSTRATIVE
CAP_SENSITIVITY_MW = (20.0, 30.0, 40.0)   # ILLUSTRATIVE network caps for the sensitivity


def supply_temp_c(ambient_c):
    """ILLUSTRATIVE weather-compensation curve: linear between the two knees, flat outside.
    Non-increasing in ambient and bounded to [SUPPLY_SUMMER_C, SUPPLY_WINTER_C]."""
    x = (SUPPLY_WARM_AMBIENT_C - ambient_c) / (SUPPLY_WARM_AMBIENT_C - SUPPLY_COLD_AMBIENT_C)
    x = max(0.0, min(1.0, x))
    return SUPPLY_SUMMER_C + (SUPPLY_WINTER_C - SUPPLY_SUMMER_C) * x


def booster_cop(loop_c, supply_c, approach_c=None):
    """Booster heat-pump COP (ILLUSTRATIVE HEAT_PUMP_FRACTION x Carnot, floored at 1) lifting the
    loop to supply + approach; None when the loop can feed the network by direct exchange."""
    a = HX_APPROACH_C if approach_c is None else approach_c
    if loop_c >= supply_c + a:
        return None
    return max(1.0, HEAT_PUMP_FRACTION * carnot_cop(supply_c + a + KELVIN, loop_c + KELVIN))


def heat_demand_mw(ambient_c, cap_mw=None):
    """ILLUSTRATIVE district-heating demand: base + degree-driven, capped by the network."""
    return min(HEAT_NETWORK_MAX_MW if cap_mw is None else cap_mw, HEAT_BASE_MW + HEAT_MW_PER_K * max(0.0, HEATING_BASE_C - ambient_c))


def savings_split(it_mwh, facility_mwh, baseline_facility_mwh):
    """Total-energy savings and cooling-OVERHEAD savings are different numbers."""
    over, base_over = facility_mwh - it_mwh, baseline_facility_mwh - it_mwh
    if base_over <= 0:
        raise ValueError("baseline must have positive overhead")
    return {"total_saved_mwh": baseline_facility_mwh - facility_mwh,
            "total_savings_pct": (baseline_facility_mwh - facility_mwh) / baseline_facility_mwh * 100,
            "overhead_saved_mwh": base_over - over,
            "overhead_savings_pct": (base_over - over) / base_over * 100}


@dataclass
class CoolingResult:
    location: str
    cooling: str
    it_mwh: float
    facility_mwh: float
    annual_pue: float
    baseline_label: str
    baseline_facility_mwh: float
    baseline_pue: float
    total_saved_mwh: float
    total_savings_pct: float
    overhead_saved_mwh: float
    overhead_savings_pct: float
    monthly: list
    flags: list = field(default_factory=list)


class SubArcticCoolingSimulator:
    """Patched version of the original. Two-season inputs are still accepted (hours
    validated to 8760) but are expanded to 12 months; a full ClimateProfile is better.
    Baseline: the SAME model on a warm profile (default) or a flat ILLUSTRATIVE PUE."""

    def __init__(self, it_load_mw, location_name="generic sub-arctic site (ILLUSTRATIVE)",
                 winter_temp_c=None, summer_temp_c=None, winter_hours=4380, summer_hours=4380,
                 profile=None, cooling="air", sinks=("ambient_air",),
                 baseline="warm_profile", baseline_pue=BASELINE_FLAT_PUE, step_pue=False):
        if it_load_mw <= 0:
            raise ValueError("it_load_mw must be positive")
        if cooling not in PUE_CURVES:
            raise ValueError(f"cooling must be one of {sorted(PUE_CURVES)}")
        if baseline not in ("warm_profile", "flat"):
            raise ValueError("baseline must be 'warm_profile' or 'flat'")
        self.it, self.location, self.cooling, self.sinks = it_load_mw, location_name, cooling, tuple(sinks)
        self.baseline, self.baseline_pue, self.step = baseline, baseline_pue, step_pue
        self.season_mode = profile is None
        if self.season_mode:
            if winter_temp_c is None or summer_temp_c is None:
                raise ValueError("give a profile or both seasonal temperatures")
            self.season_hours = validate_hours([winter_hours, summer_hours])
            self.winter_c, self.summer_c = winter_temp_c, summer_temp_c
            mean = (winter_temp_c * winter_hours + summer_temp_c * summer_hours) / HOURS_PER_YEAR
            profile = ClimateProfile(location_name, sinusoid_months(mean, abs(summer_temp_c - winter_temp_c) / 2), 0.0)
        self.profile = profile

    def _pue(self, t):
        return legacy_step_pue(t) if self.step else pue_curve(t, self.cooling)

    def _run(self, profile, sinks):
        rows = []
        for m, hrs in enumerate(month_hours()):
            hourly = sink_temps(profile, m, sinks)
            pue = sum(self._pue(t) for t in hourly) / len(hourly)
            rows.append({"month": MONTH_NAMES[m], "hours": hrs, "ambient_c": profile.monthly_c[m],
                         "sink_c": sum(hourly) / len(hourly), "pue": pue,
                         "it_mwh": self.it * hrs, "facility_mwh": self.it * pue * hrs})
        return rows

    def simulate(self):
        flags = ["Location is generic; profiles are ILLUSTRATIVE, not measured climate data."]
        if self.season_mode and self.step:
            # reproduce the original two-season arithmetic exactly (for comparison only)
            w, s = self.season_hours
            fac = self.it * (legacy_step_pue(self.winter_c) * w + legacy_step_pue(self.summer_c) * s)
            monthly = []
            flags.append("ORIGINAL step PUE on two seasonal averages: averages hide warm hours.")
        else:
            monthly = self._run(self.profile, self.sinks)
            fac = sum(r["facility_mwh"] for r in monthly)
        it_mwh = self.it * HOURS_PER_YEAR
        if self.baseline == "flat":
            base_fac, label = it_mwh * self.baseline_pue, f"flat PUE {self.baseline_pue:.2f} (ILLUSTRATIVE)"
        else:
            base_fac = sum(r["facility_mwh"] for r in self._run(PROFILES["warm"], ("ambient_air",)))
            label = f"same model, {PROFILES['warm'].name} profile, ambient air (ILLUSTRATIVE)"
        sv = savings_split(it_mwh, fac, base_fac)
        return CoolingResult(self.location, self.cooling, it_mwh, fac, fac / it_mwh, label, base_fac,
                             base_fac / it_mwh, sv["total_saved_mwh"], sv["total_savings_pct"],
                             sv["overhead_saved_mwh"], sv["overhead_savings_pct"], monthly, flags)


def report_cooling(r):
    out = [f"=== CLIMATE COOLING LEDGER: {r.location} ({r.cooling}) ===",
           f"Annual PUE {r.annual_pue:.3f} vs baseline {r.baseline_pue:.3f} [{r.baseline_label}]",
           f"Facility energy {r.facility_mwh:,.0f} MWh vs baseline {r.baseline_facility_mwh:,.0f} MWh",
           f"Total facility energy reduction: {r.total_savings_pct:.1f}% ({r.total_saved_mwh:,.0f} MWh)",
           f"Cooling-overhead reduction:      {r.overhead_savings_pct:.1f}% ({r.overhead_saved_mwh:,.0f} MWh)"]
    return "\n".join(out + r.flags + ["The ledger reports; it does not rule."])


def original_example_numbers():
    """The original 50 MW example, under its step PUE and flat 1.40 baseline, relabeled correctly."""
    return SubArcticCoolingSimulator(50.0, winter_temp_c=-8.0, summer_temp_c=14.0,
                                     baseline="flat", step_pue=True).simulate()


# --- climate scenarios through the DataCenter composer (monthly representative days) ---
CLIMATE_SCENARIOS = {   # ALL ILLUSTRATIVE
    "v2_temperate_underground": {"label": "v2 evolved (temperate, underground)", "profile": "temperate",
                                 "sinks": ("ambient_air", "rock"), "depth": 800.0, "underground": True},
    "subarctic_surface":        {"label": "sub-arctic surface", "profile": "sub_arctic",
                                 "sinks": ("ambient_air",), "depth": 0.0, "underground": False},
    "subarctic_hybrid":         {"label": "sub-arctic underground hybrid (fjord sink)", "profile": "sub_arctic",
                                 "sinks": ("ambient_air", "fjord_water"), "depth": 300.0, "underground": True},
    "warm_surface":             {"label": "warm surface baseline", "profile": "warm",
                                 "sinks": ("ambient_air",), "depth": 0.0, "underground": False},
}


def annual_center(key, cooling="direct_to_chip", fixed_orc_sink_c=None, seasonal_heat=True,
                  seasonal_supply=True, cap_mw=None):
    """v4: seasonal_supply=True uses the weather-compensated supply curve and a booster heat pump
    lifting to supply + approach; seasonal_supply=False reproduces v3 (fixed HEAT_SUPPLY_C).
    Runs the v2 evolved DataCenter once per month on that month's PUE, sink and heat demand."""
    sc = CLIMATE_SCENARIOS[key]
    prof = PROFILES[sc["profile"]]
    months, tot = [], {}
    for m, hrs in enumerate(month_hours()):
        days = hrs / 24.0
        hourly = sink_temps(prof, m, sc["sinks"])
        pue = sum(pue_curve(t, cooling) for t in hourly) / 24.0
        sink = sum(hourly) / 24.0
        dem = heat_demand_mw(prof.monthly_c[m], cap_mw) if seasonal_heat else 20.0 * 0.5
        sup = supply_temp_c(prof.monthly_c[m]) if seasonal_supply else HEAT_SUPPLY_C
        dc = DataCenter(sc["label"], cooling=cooling, pue=pue, discharge_cycle="orc",
                        discharge_sink_c=sink if fixed_orc_sink_c is None else fixed_orc_sink_c,
                        heat_customer=HeatCustomer(dem, sup, 1.0), flexible_share=0.30,
                        outage_hours=8.0, laes_cold_reuse=True, hall_depth_m=sc["depth"],
                        site_underground=sc["underground"])
        r = dc.simulate()
        n = r.numbers
        row = {"month": MONTH_NAMES[m], "days": days, "hours": hrs, "ambient_c": prof.monthly_c[m],
               "sink_c": sink, "pue": pue, "heat_demand_mw": dem, "supply_c": sup,
               "loop_c": n["cooling_temp_c"], "boosted": bool(n["heat_boosted"]), "booster_cop": n["booster_cop"],
               "boost_e_mwh": n["heat_pump_e_for_sale"] * days,
               "boosted_heat_mwh": (n["heat_sold_mwh_day"] * days) if n["heat_boosted"] else 0.0,
               "heat_flow_in_mwh": ((r.flows["heat"].total_in() * days) if "heat" in r.flows else 0.0),
               "heat_flow_elec_mwh": ((r.flows["heat"].electric_in * days) if "heat" in r.flows else 0.0),
               "it_mwh": n["it_load_mw"] * hrs,
               "overhead_mwh": n["it_load_mw"] * (pue - 1) * hrs,
               "site_mwh": n["site_parasitic_mw"] * hrs,
               "facility_mwh": n["facility_mwh_day"] * days,
               "heat_demand_mwh": dem * hrs,
               "heat_sold_mwh": n["heat_sold_mwh_day"] * days,
               "battery_e_out_mwh": n["battery_e_out"] * days,
               "battery_e_in_mwh": n["battery_e_in"] * days,
               "laes_makeup_mwh": n["laes"]["makeup_mwh_e_per_day"] * days,
               "battery_discharge_eff": n["battery_discharge_eff"],
               "useful_mwh": n["useful_output_mwh_day"] * days,
               "grid_mwh": n["net_grid_mwh_day"] * days,
               "heat_env_mwh": n["heat_to_environment_mwh_day"] * days,
               "balanced": all(f.assert_balanced() for f in r.flows.values())}
        months.append(row)
        for k, v in row.items():
            if isinstance(v, float) and k not in ("ambient_c", "sink_c", "pue", "battery_discharge_eff", "heat_demand_mw", "days",
                                                    "supply_c", "loop_c", "booster_cop"):
                tot[k] = tot.get(k, 0.0) + v
    tot["annual_pue"] = tot["facility_mwh"] / tot["it_mwh"]
    tot["cooling_pue"] = (tot["it_mwh"] + tot["overhead_mwh"]) / tot["it_mwh"]
    tot["heat_sold_share_of_demand"] = tot["heat_sold_mwh"] / tot["heat_demand_mwh"]
    tot["boosted_months"] = sum(1 for r in months if r["boosted"])
    return {"key": key, "label": sc["label"], "profile": prof.name, "sinks": list(sc["sinks"]),
            "depth_m": sc["depth"], "cooling": cooling, "months": months, "annual": tot}


def pearson(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0


_CLIMATE_CACHE = {}


def climate_numbers():
    import copy
    if not _CLIMATE_CACHE:
        sc = {k: annual_center(k) for k in CLIMATE_SCENARIOS}
        orc_fixed = {k: annual_center(k, fixed_orc_sink_c=15.0)["annual"]["battery_e_out_mwh"]
                     for k in ("subarctic_surface", "subarctic_hybrid")}
        flat_heat = {k: annual_center(k, seasonal_heat=False)["annual"]["heat_sold_mwh"]
                     for k in ("v2_temperate_underground", "subarctic_hybrid")}
        pue_table = {c: {p: SubArcticCoolingSimulator(50.0, profile=PROFILES[p], cooling=c).simulate().annual_pue
                         for p in PROFILES} for c in PUE_CURVES}
        orig = original_example_numbers()
        patched = SubArcticCoolingSimulator(50.0, winter_temp_c=-8.0, summer_temp_c=14.0, baseline="flat").simulate()
        prof_air = SubArcticCoolingSimulator(50.0, profile=PROFILES["sub_arctic"], cooling="air").simulate()
        cool_vs_heat = {k: {"pue": pearson([r["pue"] for r in v["months"]], [r["heat_demand_mw"] for r in v["months"]]),
                            "ambient": pearson([r["ambient_c"] for r in v["months"]], [r["heat_demand_mw"] for r in v["months"]])}
                        for k, v in sc.items()}
        _CLIMATE_CACHE["d"] = {
            "scenarios": sc, "orc_fixed_sink_15c_battery_e_out": orc_fixed,
            "flat_heat_assumption_heat_sold": flat_heat, "pue_table": pue_table,
            "original_example": {k: getattr(orig, k) for k in ("annual_pue", "baseline_pue", "total_savings_pct",
                                                              "overhead_savings_pct", "total_saved_mwh")},
            "patched_two_season": {k: getattr(patched, k) for k in ("annual_pue", "total_savings_pct", "overhead_savings_pct")},
            "subarctic_air_vs_warm": {k: getattr(prof_air, k) for k in ("annual_pue", "baseline_pue", "total_savings_pct",
                                                                       "overhead_savings_pct", "baseline_label")},
            "pue_heat_correlation": cool_vs_heat}
    return copy.deepcopy(_CLIMATE_CACHE["d"])


_V4_CACHE = {}
V4_COOLINGS = ("air", "direct_to_chip", "immersion")


def v4_numbers():
    """Seasonal supply + booster vs v3 fixed 55 C, per scenario and cooling; cap sensitivity."""
    import copy
    if not _V4_CACHE:
        keep = ("heat_sold_mwh", "boosted_heat_mwh", "boost_e_mwh", "useful_mwh", "grid_mwh",
                "heat_demand_mwh", "boosted_months", "battery_e_out_mwh")
        per = {}
        for k in CLIMATE_SCENARIOS:
            for c in V4_COOLINGS:
                v3 = annual_center(k, cooling=c, seasonal_supply=False)["annual"]
                v4r = annual_center(k, cooling=c)
                v4 = v4r["annual"]
                per[(k, c)] = {"v3": {x: v3.get(x, 0.0) for x in keep}, "v4": {x: v4[x] for x in keep},
                               "months": v4r["months"]}
        caps = {}
        for k in ("subarctic_hybrid", "v2_temperate_underground"):
            caps[k] = {cap: {x: annual_center(k, cap_mw=cap)["annual"][x] for x in keep} for cap in CAP_SENSITIVITY_MW}
        prof = PROFILES["sub_arctic"]
        _V4_CACHE["d"] = {"per": per, "caps": caps,
                          "supply_sub_arctic": [supply_temp_c(t) for t in prof.monthly_c],
                          "capped_months_subarctic": sum(1 for t in prof.monthly_c
                                                         if HEAT_BASE_MW + HEAT_MW_PER_K * max(0.0, HEATING_BASE_C - t) > HEAT_NETWORK_MAX_MW)}
    return copy.deepcopy(_V4_CACHE["d"])


def compare_v4(v=None):
    v = v4_numbers() if v is None else v
    out = ["=== v4 SEASONAL SUPPLY + BOOSTER vs v3 FIXED 55 C (annual MWh; ALL ILLUSTRATIVE) ===",
           f"{'scenario / cooling':<52}{'boosted heat':>13}{'boost e':>9}{'heat sold':>11}{'useful v3':>11}{'useful v4':>11}{'grid v3':>10}{'grid v4':>10}"]
    for (k, c), r in v["per"].items():
        a, b = r["v3"], r["v4"]
        out.append(f"{CLIMATE_SCENARIOS[k]['label'][:38] + ' / ' + c:<52}{b['boosted_heat_mwh']:>13,.0f}{b['boost_e_mwh']:>9,.0f}"
                   f"{b['heat_sold_mwh']:>11,.0f}{a['useful_mwh']:>11,.0f}{b['useful_mwh']:>11,.0f}{a['grid_mwh']:>10,.0f}{b['grid_mwh']:>10,.0f}")
    for k, d in v["caps"].items():
        out.append(f"Cap sensitivity, {CLIMATE_SCENARIOS[k]['label']}: " +
                   ", ".join(f"{cap:g} MW -> {x['heat_sold_mwh']:,.0f} sold" for cap, x in d.items()))
    out.append("No law broken: booster electricity is in grid purchases and the heat column balances.")
    return "\n".join(out)


def _tests_v4(check, raises):
    ts = [x / 2 for x in range(-100, 101)]
    vals = [supply_temp_c(t) for t in ts]
    check("supply curve non-increasing with ambient (rises as it gets colder)", all(b <= a + 1e-12 for a, b in zip(vals, vals[1:])))
    check("supply curve strictly hotter in deep cold than summer", supply_temp_c(-20) > supply_temp_c(20))
    check("supply curve bounded", min(vals) >= SUPPLY_SUMMER_C - 1e-12 and max(vals) <= SUPPLY_WINTER_C + 1e-12)
    check("supply curve hits both bounds", supply_temp_c(-40) == SUPPLY_WINTER_C and supply_temp_c(30) == SUPPLY_SUMMER_C)
    check("supply curve continuous", max(abs(b - a) for a, b in zip(vals, vals[1:])) < 1.0)
    check("booster off when loop >= supply + approach", booster_cop(65, 55) is None and booster_cop(60, 55) is None)
    check("booster on when loop below supply + approach", booster_cop(60, 56) is not None and booster_cop(40, 55) is not None)
    for loop, sup in ((40, 55), (40, 80), (60, 75), (65, 70), (60, 56)):
        cop = booster_cop(loop, sup)
        car = carnot_cop(sup + HX_APPROACH_C + KELVIN, loop + KELVIN)
        check(f"booster COP >= 1 ({loop}->{sup})", cop >= 1.0)
        check(f"booster COP < Carnot ({loop}->{sup})", cop < car)
    check("booster COP falls with bigger lift", booster_cop(60, 80) < booster_cop(60, 60))
    v = v4_numbers()
    for (k, c), r in v["per"].items():
        ms = r["months"]
        check(f"booster only when loop below supply ({k}/{c})",
              all((m["boost_e_mwh"] > 0) == (m["loop_c"] < m["supply_c"] + HX_APPROACH_C) for m in ms if m["heat_sold_mwh"] > 0))
        check(f"per-month heat balance includes boost electricity ({k}/{c})",
              all(abs(m["heat_flow_elec_mwh"] - m["boost_e_mwh"]) < 1e-6 and m["balanced"] for m in ms))
        check(f"boost electricity is in grid purchases ({k}/{c})",
              all(abs(m["grid_mwh"] - (m["facility_mwh"] + m["battery_e_in_mwh"] + m["boost_e_mwh"] + m["laes_makeup_mwh"]
                                       - m["battery_e_out_mwh"])) < 1e-6 * m["grid_mwh"] for m in ms))
    sa = v["per"][("subarctic_hybrid", "direct_to_chip")]["months"]
    check("winter supply hotter than summer (sub-arctic)", sa[0]["supply_c"] > sa[6]["supply_c"])
    check("immersion needs less boost than air (sub-arctic)",
          v["per"][("subarctic_hybrid", "immersion")]["v4"]["boost_e_mwh"] < v["per"][("subarctic_hybrid", "air")]["v4"]["boost_e_mwh"])
    check("warm site dtc needs no boost", v["per"][("warm_surface", "direct_to_chip")]["v4"]["boost_e_mwh"] == 0)
    for k, d in v["caps"].items():
        sold = [d[cap]["heat_sold_mwh"] for cap in CAP_SENSITIVITY_MW]
        boost = [d[cap]["boost_e_mwh"] for cap in CAP_SENSITIVITY_MW]
        check(f"cap sensitivity monotonic heat sold ({k})", all(b >= a - 1e-6 for a, b in zip(sold, sold[1:])))
        check(f"cap sensitivity monotonic boost e ({k})", all(b >= a - 1e-6 for a, b in zip(boost, boost[1:])))
    check("30 MW cap matches default run", abs(v["caps"]["subarctic_hybrid"][30.0]["heat_sold_mwh"]
                                               - v["per"][("subarctic_hybrid", "direct_to_chip")]["v4"]["heat_sold_mwh"]) < 1e-6)
    check("compare_v4 no viable", "viable" not in compare_v4(v).lower())


def compare_climates(c=None):
    c = climate_numbers() if c is None else c
    keys = list(CLIMATE_SCENARIOS)
    rows = [("Annual facility energy (MWh)", "facility_mwh", "{:,.0f}"), ("Annual PUE (incl. site)", "annual_pue", "{:.3f}"),
            ("Heat sold (MWh_th)", "heat_sold_mwh", "{:,.0f}"), ("Battery electricity (MWh_e)", "battery_e_out_mwh", "{:,.0f}"),
            ("Useful output (MWh)", "useful_mwh", "{:,.0f}"), ("Net grid purchases (MWh)", "grid_mwh", "{:,.0f}"),
            ("Heat to environment (MWh)", "heat_env_mwh", "{:,.0f}")]
    out = ["=== CLIMATE SCENARIOS (annual, 12 monthly representative days; ALL ILLUSTRATIVE, generic sites) ===",
           f"{'':<30}" + "".join(f"{CLIMATE_SCENARIOS[k]['label'][:26]:>28}" for k in keys)]
    for lab, k, fmt in rows:
        out.append(f"{lab:<30}" + "".join(f"{fmt.format(c['scenarios'][s]['annual'][k]):>28}" for s in keys))
    out.append("No law broken: every monthly column balances. Economics: not claimed here.")
    return "\n".join(out)


def _tests_climate(check, raises):
    # PUE curve
    for c in PUE_CURVES:
        ts = [x / 2 for x in range(-80, 121)]
        vals = [pue_curve(t, c) for t in ts]
        check(f"PUE curve monotone ({c})", all(b >= a - 1e-12 for a, b in zip(vals, vals[1:])))
        check(f"PUE floor in deep cold ({c})", abs(pue_curve(-30, c) - pue_curve(-60, c)) < 1e-12 and pue_curve(-30, c) == min(vals))
        check(f"PUE >= 1 everywhere ({c})", min(vals) >= 1.0)
        check(f"PUE continuous ({c})", max(abs(b - a) for a, b in zip(vals, vals[1:])) < 0.02)
    check("hotter loop keeps free cooling longer", pue_curve(25, "direct_to_chip") < pue_curve(25, "air"))
    check("immersion <= dtc at 40 C", pue_curve(40, "immersion") <= pue_curve(40, "direct_to_chip"))
    check("bad cooling rejected (curve)", raises(lambda: pue_curve(10, "fans")))
    # labeling: original example
    o = original_example_numbers()
    check("original step PUE annual = 1.07", abs(o.annual_pue - 1.07) < 1e-12)
    check("original total savings ~23.6% (not overhead)", abs(o.total_savings_pct - (1 - 1.07 / 1.40) * 100) < 1e-9)
    check("original overhead savings ~82.5%", abs(o.overhead_savings_pct - (0.40 - 0.07) / 0.40 * 100) < 1e-9)
    check("overhead % != total %", o.overhead_savings_pct > 3 * o.total_savings_pct)
    s = savings_split(100.0, 112.0, 140.0)
    check("1.12 vs 1.40: total 20%", abs(s["total_savings_pct"] - 20.0) < 1e-9)
    check("1.12 vs 1.40: overhead 70%", abs(s["overhead_savings_pct"] - 70.0) < 1e-9)
    check("report labels both", "Total facility energy reduction" in report_cooling(o)
          and "Cooling-overhead reduction" in report_cooling(o) and "reduction in cooling overhead" not in report_cooling(o))
    check("no SUCCESS verdict", "SUCCESS" not in report_cooling(o) and "safely" not in report_cooling(o))
    check("zero-overhead baseline rejected", raises(lambda: savings_split(100, 100, 100)))
    # hours validation
    check("hours 4380+4380 ok", validate_hours([4380, 4380]) == [4380, 4380])
    check("hours not 8760 rejected", raises(lambda: SubArcticCoolingSimulator(50, winter_temp_c=-8, summer_temp_c=14, winter_hours=5000)))
    check("negative hours rejected", raises(lambda: validate_hours([9000, -240])))
    check("month hours sum 8760", abs(sum(month_hours()) - 8760) < 1e-9)
    check("profile needs 12 months", raises(lambda: ClimateProfile("x", [0] * 11)))
    check("needs temps or profile", raises(lambda: SubArcticCoolingSimulator(50)))
    # baseline
    check("flat baseline parameter used", abs(SubArcticCoolingSimulator(50, profile=PROFILES["sub_arctic"], baseline="flat",
                                                                       baseline_pue=1.3).simulate().baseline_pue - 1.3) < 1e-12)
    w = SubArcticCoolingSimulator(50, profile=PROFILES["sub_arctic"]).simulate()
    check("warm-profile baseline from same model", 1.0 < w.baseline_pue < 1.40 and w.annual_pue < w.baseline_pue)
    check("averages hide warm hours (diurnal raises PUE)",
          SubArcticCoolingSimulator(50, profile=ClimateProfile("d", PROFILES["warm"].monthly_c, 6)).simulate().annual_pue >
          SubArcticCoolingSimulator(50, profile=ClimateProfile("n", PROFILES["warm"].monthly_c, 0)).simulate().annual_pue)
    # sinks
    check("fjord caps summer sink", max(sink_temps(PROFILES["sub_arctic"], 6, ("ambient_air", "fjord_water"))) <= 8.0 + 1e-9)
    check("rock sink uses gradient", abs(sink_temps(PROFILES["temperate"], 0, ("rock",))[0]
                                         - geothermal_temp_c(PROFILES["temperate"].mean(), SINK_DEPTH_M)) < 1e-12)
    check("unknown sink rejected", raises(lambda: sink_temps(PROFILES["warm"], 0, ("volcano",))))
    # heat demand seasonal & anti-correlated
    c = climate_numbers()
    for k, r in c["pue_heat_correlation"].items():
        check(f"heat demand anti-correlated with ambient/cooling need ({k})", r["ambient"] < -0.5)
        check(f"heat demand never rises with PUE ({k})", r["pue"] <= 1e-9)
    sa = c["scenarios"]["subarctic_surface"]["months"]
    check("winter heat demand > summer", sa[0]["heat_demand_mw"] > 3 * sa[6]["heat_demand_mw"])
    check("heat demand capped", all(heat_demand_mw(t) <= HEAT_NETWORK_MAX_MW for t in range(-40, 40)))
    # per-month energy balance
    for k, v in c["scenarios"].items():
        ok = all(abs(r["facility_mwh"] - (r["it_mwh"] + r["overhead_mwh"] + r["site_mwh"])) < 1e-6 * r["facility_mwh"]
                 and r["balanced"] and r["heat_sold_mwh"] <= r["facility_mwh"] + 1e-9 for r in v["months"])
        check(f"per-month energy balance ({k})", ok)
        check(f"annual = sum of months ({k})", abs(v["annual"]["grid_mwh"] - sum(r["grid_mwh"] for r in v["months"])) < 1e-6)
    S = c["scenarios"]
    check("cold climate sells more heat", S["subarctic_hybrid"]["annual"]["heat_sold_mwh"] > S["v2_temperate_underground"]["annual"]["heat_sold_mwh"])
    check("sub-arctic PUE < warm PUE", S["subarctic_surface"]["annual"]["cooling_pue"] < S["warm_surface"]["annual"]["cooling_pue"])
    check("winter ORC eff > summer (surface)", sa[0]["battery_discharge_eff"] > sa[6]["battery_discharge_eff"])
    check("cold sink beats fixed 15 C sink", S["subarctic_surface"]["annual"]["battery_e_out_mwh"]
          > c["orc_fixed_sink_15c_battery_e_out"]["subarctic_surface"])
    check("surface has no site parasitics", S["subarctic_surface"]["annual"]["site_mwh"] == 0)
    check("compare table no viable", "viable" not in compare_climates(c).lower())


# ======================= design document v3 (fused) =======================
# The v2 design document lives here. Every number below is computed live from the
# composer (scenario_numbers / baseline_center / evolved_center) and module constants.
# Blocks: ("H", text) heading, ("P", text) paragraph, ("BL", [items]) bullets,
# ("T", rows) table (first row = header).

DOC_TITLE = "ZUCKY CENTER — Thermodynamic Design v5 (price-timed dispatch)"
V1_BATTERY_CHARGE_MWH = 323   # historical v1 PDF figure, quoted for comparison only
V1_STORED_REJECTED_MWH = 684  # historical v1 PDF figure, quoted for comparison only


def doc_blocks(d=None):
    d = scenario_numbers() if d is None else d
    B, E = d["baseline"], d["evolved"]
    FB, FE = d["flows"]["baseline"], d["flows"]["evolved"]
    LB, LE = B["laes"], E["laes"]
    bc, ec = baseline_center(), evolved_center()
    cust = ec.customer
    f0 = lambda v: f"{v:,.0f}"
    f1 = lambda v: f"{v:,.1f}"
    pc = lambda v: f"{v*100:.1f}%"
    g = lambda v: f"{v:g}"
    air, dtc = COOLING["air"], COOLING[ec.cooling]
    carnot_waste = carnot_efficiency(air["temp_c"] + KELVIN, ec.discharge_sink_c + KELVIN)
    rock = geothermal_temp_c(ec.discharge_sink_c, bc.depth)
    shallow = geothermal_temp_c(ec.discharge_sink_c, 150)
    n_tests = _count_tests()
    blocks = []
    H = lambda t: blocks.append(("H", t))
    P = lambda t: blocks.append(("P", t))
    BL = lambda items: blocks.append(("BL", list(items)))
    T = lambda rows: blocks.append(("T", [[str(c) for c in r] for r in rows]))

    P("An underground AI data center. Subterranean Ledger series.")
    C = d["climate"]
    S = {k: v["annual"] for k, v in C["scenarios"].items()}
    SM = {k: v["months"] for k, v in C["scenarios"].items()}
    OX, SA = C["original_example"], C["subarctic_air_vs_warm"]
    P("Basis: this file, zucky_center.py (code and design document fused; extends underground_energy.py: "
      "sCO2 generator + Carnot battery + LAES, with a DataCenter composer). Status: PROTOTYPE NUMBERS. Every value "
      "marked ILLUSTRATIVE is an assumption for exploration, not a measured value. Every number in this document is "
      "computed by the code when `python3 zucky_center.py doc` runs; none is hand-entered (the two v1 figures quoted "
      "for comparison are labelled as such). The ledger reports; it does not rule.")

    V4 = d["v4"]
    vp = V4["per"]
    H("Changes versus v3")
    BL([
        f"District-heating supply temperature is now seasonal: an ILLUSTRATIVE weather-compensated curve from {g(SUPPLY_SUMMER_C)} C "
        f"(ambient ≥ {g(SUPPLY_WARM_AMBIENT_C)} C) to {g(SUPPLY_WINTER_C)} C (ambient ≤ {g(SUPPLY_COLD_AMBIENT_C)} C), linear between; v3 held {g(HEAT_SUPPLY_C)} C all year.",
        f"Booster heat pump: when the loop is below supply + {g(HX_APPROACH_C)} C approach, a booster lifts it (COP {HEAT_PUMP_FRACTION:.2f} x Carnot, floored at 1); "
        "otherwise direct exchange. Its electricity is in net grid purchases and in the heat-sales energy column.",
        f"Network cap sensitivity ({' / '.join(g(x) for x in CAP_SENSITIVITY_MW)} MW, ILLUSTRATIVE) added.",
        "Per-scenario, per-cooling comparison of boosted heat, boost electricity, heat sold, useful output and grid purchases versus v3 (section 20).",
        "New section 20; 'No law broken' moves to 21.",
    ])

    H("Changes versus v4")
    BL([
        "Price-timed battery dispatch (new section 21): the 12 cheapest / 6 priciest hours of the ILLUSTRATIVE price signal "
        "value the battery's charge and discharge; the energy model is unchanged, so this is the spread argument made legible, not economics.",
        "Booster fix: the heat-pump booster always lifts to supply + HX approach now. v4's fixed-supply comparison mode lifted only "
        "to supply, which disagreed with booster_cop() and with real heat-exchanger practice.",
        "Test counting no longer re-runs the suite inside the doc renderer (re-entrancy flag); the shared critical-flag check is counted explicitly.",
        "EnergyFlow.assert_balanced is now commented honestly: it guards the arithmetic, not the physics.",
        "New section 21; 'No law broken' moves to 22.",
    ])

    H("Changes versus v2")
    BL([
        "Fused the SubArcticCoolingSimulator idea as a site/climate layer (class kept, patched).",
        f"Label fix: the original printed total facility-energy savings as '% reduction in cooling overhead'. For its own example "
        f"(step PUE {OX['annual_pue']:.2f} vs flat {OX['baseline_pue']:.2f}) total savings are {OX['total_savings_pct']:.1f}% and overhead savings "
        f"{OX['overhead_savings_pct']:.1f}%; both are now reported, labelled.",
        "Step PUE at 0 / 15 C on two seasonal averages replaced by a continuous ILLUSTRATIVE PUE(T) curve per cooling type, "
        "evaluated hourly over 12 monthly representative days (averages hide warm hours).",
        f"Warm baseline: the flat {BASELINE_FLAT_PUE:.2f} is now an ILLUSTRATIVE parameter; by default the same model runs on a warm profile.",
        "Season/month hours validated to sum to 8760.",
        "'[SUCCESS] ... safely' verdict dropped; STANDING = False.",
        "Cold-water sinks (fjord / lake, ILLUSTRATIVE) and a rock sink from the gradient function; coldest available sink used each hour.",
        "District heating demand is now seasonal (degree-driven, ILLUSTRATIVE) instead of a flat 'half the year'.",
        "ORC discharge sink follows the monthly sink temperature; colder months give a better Carnot limit.",
        "DataCenter gains site_underground (surface sites carry no underground parasitic lines).",
        "New sections 16–19; numbering of 'No law broken' moves to 20.",
    ])

    H("Changes versus v1")
    BL([
        f"Battery cold side split: heat-pump source (cooling loop) and ORC sink ({g(ec.discharge_sink_c)} C) are separate inputs.",
        f"ORC discharge modeled at ILLUSTRATIVE {ORC_FRACTION_OF_CARNOT:.2f} x Carnot, separate from sCO2's {FRACTION_OF_CARNOT:.2f}.",
        f"Rock warming capped at an ILLUSTRATIVE {g(ROCK_WARMING_HORIZON_YEARS)}-year valid window, with flags for the horizon and for a sink approaching the store.",
        "FRACTION_OF_CARNOT sweep (" + " / ".join(f"{x:.2f}" for x in SWEEP_FRACTIONS) + ").",
        "DataCenter composer: cooling type, heat customer, battery, outage-sized LAES with cold reuse, optional molten salt, flexible compute, underground site lines.",
        f"Section 12 table now generated from EnergyFlow columns, in = out asserted. The v1 '{V1_STORED_REJECTED_MWH} stored, {V1_STORED_REJECTED_MWH} rejected' "
        "simplification is fixed: the battery lifts only part of the stored heat from the waste stream; the rest is compressor electricity.",
        f"Site parasitic loads ({B['site_parasitic_mw']:.2f} MW, ILLUSTRATIVE) are now included, so baseline numbers differ slightly from v1 "
        f"(e.g. {f0(B['battery_e_in'])} vs {V1_BATTERY_CHARGE_MWH} MWh_e battery charge).",
        "New sections: heat sales, hot liquid cooling, flexible compute, LAES cold reuse, underground site requirements, baseline vs evolved.",
        "Section 9 verdict replaced by 'No law broken' language. Economics still not claimed.",
        "v2.1: code and design document fused into one standard-library file; `doc` prints this document from live numbers.",
    ])

    H("1. The one non-negotiable")
    P(f"A data center rejects its power as heat at ~{g(air['temp_c'])} C (air) or ~{g(dtc['temp_c'])}–{g(COOLING['immersion']['temp_c'])} C (liquid). "
      f"Carnot between {g(air['temp_c'])} C and {g(ec.discharge_sink_c)} C is ~{carnot_waste*100:.0f}%; a real cycle gets "
      f"~{carnot_waste*FRACTION_OF_CARNOT*100:.0f}%. Low-grade waste heat cannot usefully generate electricity. Any design claiming otherwise is selling something.")
    P(f"At {g(bc.depth)} m with a 25 C/km gradient the rock is {rock:.0f} C. The rock is not a furnace. The underground is the thermos, not the fuel. "
      "High-temperature heat comes from the grid (cheap hours) and heat pumps, never from the rock.")
    P("New in v2: the most valuable thing to do with warm waste heat is often not to turn it back into electricity at all, but to sell it as heat (section 7).")
    P("Everything below obeys the first law (EnergyFlow columns, in = out asserted in code) and the second law (rte_guard: nothing exceeds 100% round trip).")

    H("2. Facility spec")
    T([["Item", "v1 baseline", "v2 evolved"],
       ["IT load", f"{B['it_load_mw']:.0f} MW_e", f"{E['it_load_mw']:.0f} MW_e"],
       ["Cooling", f"air, {B['cooling_temp_c']:.0f} C (ILLUSTRATIVE)", f"direct-to-chip, {E['cooling_temp_c']:.0f} C (ILLUSTRATIVE)"],
       ["PUE (ILLUSTRATIVE)", f"{B['pue']:.2f}", f"{E['pue']:.2f}"],
       ["Site parasitics (ILLUSTRATIVE)", f"{B['site_parasitic_mw']:.2f} MW", f"{E['site_parasitic_mw']:.2f} MW"],
       ["Total draw", f"{B['facility_mw']:.1f} MW_e", f"{E['facility_mw']:.1f} MW_e"],
       ["Heat captured in liquid/hot loop", f"{f0(B['hot_stream_mwh_day'])} MWh_th/day", f"{f0(E['hot_stream_mwh_day'])} MWh_th/day"],
       [f"Residual ~{g(RESIDUAL_AIR_C)} C air heat", f"{f0(B['residual_air_mwh_day'])} MWh_th/day", f"{f0(E['residual_air_mwh_day'])} MWh_th/day"],
       ["Grid connection", "yes, not islanded", "yes, not islanded"],
       ["Siting", f"halls 150–{g(bc.depth)} m ({g(bc.depth)} m modeled)", "same"]])

    H("3. Subsystem A — Waste-heat Carnot battery (diurnal shifting)")
    P(f"A heat pump lifts the waste stream into a {g(bc.battery_store_c)} C hot-water aquifer store (HT-ATES); an ORC discharges it when power is expensive.")
    P(f"Baseline ({g(air['temp_c'])} C source, {g(bc.discharge_sink_c)} C sink, {g(bc.battery_hours)} h of waste stream, {g(bc.idle)} h idle):")
    BL([f"Carnot COP {B['battery_carnot_cop']:.2f}; used COP {HEAT_PUMP_FRACTION:.2f} x = {B['battery_cop']:.2f} (ILLUSTRATIVE fraction)",
        f"Stored {f0(B['battery_capacity_mwh_th'])} MWh_th = {f0(B['battery_lifted'])} lifted from the waste stream + {f0(B['battery_e_in'])} MWh_e compressor work",
        f"Decay {B['battery_decay_pct_day']:.2f}%/day (ILLUSTRATIVE) → {f0(B['battery_retained'])} MWh_th retained",
        f"ORC at {ORC_FRACTION_OF_CARNOT:.2f} x Carnot = {pc(B['battery_discharge_eff'])} → {f0(B['battery_e_out'])} MWh_e out",
        f"Round trip {pc(B['battery_rte'])}; net loss {f0(B['battery_net_loss'])} MWh_e per cycle"])
    P(f"Evolved ({g(dtc['temp_c'])} C source): COP {E['battery_cop']:.2f}, round trip {pc(E['battery_rte'])}, but it stores less "
      f"({f0(E['battery_capacity_mwh_th'])} MWh_th) because heat sales take part of the stream first.")
    P("Honest accounting: this subsystem moves energy in time; it does not create it. Its case rests on the peak/off-peak spread or firm capacity.")

    H("4. Subsystem B — LAES backup (diesel replacement)")
    P(f"v2 sizes LAES from an outage-duration input instead of a fixed {g(bc.outage_hours)} h. Liquefaction {LIQUEFACTION_KWH_PER_KG:.2f} kWh/kg "
      f"and discharge {DISCHARGE_KWH_PER_KG:.2f} kWh/kg are ILLUSTRATIVE.")
    T([["", f"v1 baseline ({g(bc.outage_hours)} h)", f"v2 evolved ({g(ec.outage_hours)} h, cold reuse)"],
       ["Backup energy", f"{f0(LB['need_mwh_e'])} MWh_e", f"{f0(LE['need_mwh_e'])} MWh_e"],
       ["Charge energy", f"{f0(LB['electricity_in_mwh'])} MWh_e", f"{f0(LE['electricity_in_mwh'])} MWh_e"],
       ["RTE (electric)", pc(LB['rte']), pc(LE['rte'])],
       ["Liquid air", f"{f0(LB['mass_t'])} t / {f0(LB['volume_m3'])} m3", f"{f0(LE['mass_t'])} t / {f0(LE['volume_m3'])} m3"],
       [f"Boil-off (ILLUSTRATIVE U {TANK_U_W_PER_M2K:g} W/m2K)", f"{LB['boil_off_t_per_day']:.1f} t/day", f"{LE['boil_off_t_per_day']:.1f} t/day"]])
    P(f"The {g(ec.outage_hours)} h outage is ILLUSTRATIVE. The real number should come from local grid outage statistics and the site's reliability target. "
      f"Siting: surface or shallow; liquid-air tanks gain nothing from {g(bc.depth)} m.")

    H("5. Subsystem C — Molten-salt arbitrage tier (optional)")
    P(f"Charged resistively (heat-pump COP to 565 C is < 1, so the code falls back to COP 1), discharged through sCO2 at {FRACTION_OF_CARNOT:.2f} x Carnot. "
      f"Carnot 565 → 25 C = {pc(B['salt_carnot'])}; sCO2 = {pc(B['salt_eff'])}; per 100 MWh_e in: {f1(B['salt100_out'])} MWh_e out "
      f"({pc(B['salt100_rte'])} after 12 h decay). Off in both scenarios; it only makes sense on otherwise-curtailed power.")

    H("6. Why underground — the honest list")
    BL([f"The rock is insulation: decay scales with the store-to-rock gap and deep rock holds ~{rock:.0f} C steady.",
        "Security and footprint.",
        f"Stable condensing sink for the ORC and a free-cooling assist (shallow rock ~{shallow:.0f} C at 150 m).",
        "NOT for: geothermal power at 25 C/km, LAES depth, or free energy of any kind.",
        "New: underground also adds costs the thermodynamics hides; see section 11."])

    H("7. Heat sales (new)")
    P(f"The evolved case sells heat to a district heating network: ILLUSTRATIVE {g(cust.demand_mw_th)} MW_th demand at {g(cust.supply_temp_c)} C supply, "
      f"available {cust.availability*100:.0f}% of the year (heating season). On an annual-average day that is {f0(E['heat_sold_mwh_day'])} MWh_th sold. "
      f"Because the {g(dtc['temp_c'])} C loop sits above {g(cust.supply_temp_c)} C + {g(HX_APPROACH_C)} C approach, it is a plain heat exchanger: "
      f"{f0(E['heat_pump_e_for_sale'])} MWh_e of heat-pump power. With {g(air['temp_c'])} C air cooling the same customer at 70 C would need a heat pump "
      "(the code models and flags this). Heat sold counts as useful output; the ORC battery becomes the fallback for months without heat demand.")

    H("8. Hot liquid cooling (new)")
    T([["Cooling (all ILLUSTRATIVE)", "Loop temp", "PUE", "Share captured"]] +
      [[k, f"{v['temp_c']:.0f} C", f"{v['pue']:.2f}", f"{v['capture']*100:.0f}%"] for k, v in d["cooling"].items()])
    P(f"Hotter loops raise the battery heat pump's COP ({B['battery_cop']:.2f} at {g(air['temp_c'])} C → {E['battery_cop']:.2f} at {g(dtc['temp_c'])} C) "
      f"and allow direct heat sales. Direct-to-chip leaves part of the heat in ~{g(RESIDUAL_AIR_C)} C air ({f0(E['residual_air_mwh_day'])} MWh_th/day here), which is rejected.")

    H("9. Flexible compute (new)")
    P(f"Interruptible AI training work is modeled as a {ec.flex*100:.0f}% flexible share (ILLUSTRATIVE), moved out of above-average-price hours into cheaper hours "
      f"on an ILLUSTRATIVE {len(ec.prices)} h price signal, with loading capped at {FLEX_HEADROOM*100:.0f}%. Energy is conserved; {f0(E['flex_shifted_mwh'])} MWh/day "
      f"is shifted and the price-weighted cost index falls from {f0(E['flex_cost_index_base'])} to {f0(E['flex_cost_index_flex'])} (relative units, not money). "
      f"Shifting load has no round-trip loss, so it competes directly with the {pc(E['battery_rte'])} battery.")

    H("10. LAES cold reuse (new)")
    P(f"During discharge, cold not already returned to the liquefier pre-cools the halls (ILLUSTRATIVE {COLD_TO_HALLS_KWH_PER_KG:g} kWh/kg → "
      f"{f0(LE['cold_to_halls_mwh'])} MWh of cold), and {g(dtc['temp_c'])} C waste heat reheats the air before expansion "
      f"(ILLUSTRATIVE +{LE['boost_kwh_per_kg']:g} kWh/kg, using {f0(LE['reheat_mwh_th'])} MWh_th). Electric RTE rises from {pc(LB['rte'])} to {pc(LE['rte'])}; "
      f"counting the avoided chiller power (ILLUSTRATIVE COP {g(CHILLER_COP)}) the effective figure is {pc(LE['effective_rte'])}. "
      f"The code caps this at an ILLUSTRATIVE {LAES_EFFECTIVE_RTE_BOUND*100:.0f}% and still applies rte_guard (≤ 100%).")

    H("11. Underground site requirements (new)")
    P("Ledger lines with ILLUSTRATIVE parasitic loads or flags. These are not costs.")
    T([["Item", "Load (MW)", "Note"]] + [[n, f"{mw:.2f}", note] for n, mw, note in B["site"]])

    H("12. Daily energy balance (first law, from EnergyFlow)")
    cols = [("battery", "Battery (cycle)"), ("laes", "LAES (event)"), ("salt", "Salt (/100 MWh_e)"),
            ("heat", "Heat sales (day)"), ("dc", "Data center (day)")]
    for name, F in (("Baseline", FB), ("Evolved", FE)):
        P(f"{name} (MWh). In = out on every column is asserted in code.")
        cs = [(k, l) for k, l in cols if k in F]
        rows = [[""] + [l for _, l in cs]]
        for lab, a in (("Electric in", "electric_in"), ("Thermal in", "thermal_in"), ("Heat from cold", "heat_from_cold"),
                       ("Electric out", "electric_out"), ("Thermal out", "thermal_out")):
            rows.append([lab] + [f0(F[k][a]) for k, _ in cs])
        T(rows)
    P(f"Waste stream, baseline: of {f0(B['facility_mwh_day'])} MWh_th/day, {f0(B['battery_lifted'])} is lifted into the store and "
      f"{f0(B['waste_rejected_mwh_day'])} is rejected (v1 said '{V1_STORED_REJECTED_MWH} stored, {V1_STORED_REJECTED_MWH} rejected'). "
      f"Evolved: {f0(E['battery_lifted'])} lifted, {f0(E['heat_drawn_for_sale_mwh_day'])} sold, {f0(E['waste_rejected_mwh_day'])} rejected. "
      "LAES 'thermal in' is reheat waste heat; its 'heat from cold' is heat absorbed from the halls by the cold. "
      "LAES and heat-sales thermal out is the balancing residual.")

    H("13. Baseline versus evolved (annual-average day)")
    T([["MWh/day", "Baseline", "Evolved"],
       ["Useful output (electricity + heat sold)", f0(B['useful_output_mwh_day']), f0(E['useful_output_mwh_day'])],
       ["  of which battery electricity", f0(B['battery_e_out']), f0(E['battery_e_out'])],
       ["  of which heat sold", f0(B['heat_sold_mwh_day']), f0(E['heat_sold_mwh_day'])],
       ["Net grid purchases", f0(B['net_grid_mwh_day']), f0(E['net_grid_mwh_day'])],
       ["Facility waste heat rejected", f0(B['waste_rejected_mwh_day']), f0(E['waste_rejected_mwh_day'])],
       ["All heat to environment", f0(B['heat_to_environment_mwh_day']), f0(E['heat_to_environment_mwh_day'])],
       ["Flexible MWh shifted", f0(B['flex_shifted_mwh']), f0(E['flex_shifted_mwh'])],
       ["LAES charge energy (per event)", f0(LB['electricity_in_mwh']), f0(LE['electricity_in_mwh'])]])
    P("Most of the grid reduction comes from the lower PUE and the smaller battery charge, not from new energy. "
      "The evolved case's battery delivers less electricity because heat sales take first claim on the hot stream. "
      "Net grid purchases include battery charging minus discharge and LAES boil-off make-up; they exclude initial LAES fill.")

    H("14. Sensitivity: FRACTION_OF_CARNOT")
    T([["Fraction (ILLUSTRATIVE)", "sCO2 efficiency", "Net MW per 10 MW_th", "Molten-salt RTE"]] +
      [[f"{r['fraction']:.2f}", pc(r['sco2_eff']), f"{r['sco2_net_mw_e_per_10mw']:.2f}", pc(r['molten_salt_rte'])] for r in d["sweep"]])

    H("15. What the code does now / next")
    if n_tests["total"] < 0:
        # this render is running inside the silent test tally: skip the count line, no recursion
        BL(["Test counts are tallied by `--test`; this render is running inside the tally, so the count line is skipped."])
    else:
        BL([f"Done: all four section 8 items from v1, plus the composer extensions above. {n_tests['total']} tests "
            f"({n_tests['core']} ledger + {n_tests['doc']} document checks), run with `--test`."])
    BL(["v3 adds the climate layer (sections 16–19); v4 adds seasonal supply temperature and boosting (section 20); "
        "v5 adds price-timed battery dispatch (section 21). Next: a two-stage feed (direct preheat, boost only the top-up); "
        "real hourly weather files for a chosen site; real outage statistics."])

    H("16. Climate siting (new in v3)")
    P("All locations are GENERIC and every profile is ILLUSTRATIVE (sinusoid from an annual mean and amplitude, plus a day/night swing). "
      "They are not measured climate data for any real site.")
    T([["Profile (ILLUSTRATIVE)", "Annual mean", "Coldest month", "Warmest month", "Day/night half-swing"]] +
      [[p.name, f"{p.mean():.1f} C", f"{min(p.monthly_c):.1f} C", f"{max(p.monthly_c):.1f} C", f"{p.diurnal_amp_c:g} C"] for p in PROFILES.values()])
    T([["Heat sink", "Temperature"],
       ["ambient air", "hourly profile"],
       [f"rock loop at {g(SINK_DEPTH_M)} m (gradient)", f"{geothermal_temp_c(PROFILES['temperate'].mean(), SINK_DEPTH_M):.1f} C (temperate surface)"]] +
      [[f"{k.replace('_', ' ')} (ILLUSTRATIVE)", f"{mu:g} ± {amp:g} C"] for k, (mu, amp, _) in WATER_SINKS.items()])
    BL([f"{v['label']}: sinks {', '.join(v['sinks'])}; halls {('at ' + g(v['depth_m']) + ' m') if v['depth_m'] else 'at surface'}" for v in C["scenarios"].values()])
    P("Each hour the coldest available sink is used. A rock sink warms with use (see the rock-warming horizon flag); deep rock at 800 m is too warm to be a cooling sink, so v2's underground case cools to a shallow loop.")

    H("17. Seasonal PUE (new in v3)")
    P("PUE(T) is continuous and ILLUSTRATIVE: a free-cooling floor, a smooth ramp, then a mechanical-assist slope. For liquid cooling only the captured share "
      "rides the liquid curve; the residual air share follows the air curve. Annual PUE, same 50 MW IT load, ambient-air sink:")
    T([["Cooling \\ profile"] + [PROFILES[p].name for p in PROFILES]] +
      [[c] + [f"{C['pue_table'][c][p]:.3f}" for p in PROFILES] for c in PUE_CURVES])
    T([["Sink temp", "air", "direct_to_chip", "immersion"]] +
      [[f"{t} C"] + [f"{pue_curve(t, c):.3f}" for c in ("air", "direct_to_chip", "immersion")] for t in (-10, 0, 10, 20, 30, 40)])
    P(f"Savings, labelled correctly. The original example under its own step PUE and flat {OX['baseline_pue']:.2f} baseline: total facility energy "
      f"{OX['total_savings_pct']:.1f}% lower ({f0(OX['total_saved_mwh'])} MWh), cooling overhead {OX['overhead_savings_pct']:.1f}% lower. "
      f"With the continuous curve on the sub-arctic profile and the warm profile as baseline (air cooling): PUE {SA['annual_pue']:.3f} vs "
      f"{SA['baseline_pue']:.3f}, total {SA['total_savings_pct']:.1f}%, overhead {SA['overhead_savings_pct']:.1f}%. The flat baseline overstates the gain; "
      "and with liquid cooling the climate matters far less, because a 60 C loop free-cools almost everywhere.")

    H("18. Seasonal heat sales (new in v3)")
    P(f"District heating demand (ILLUSTRATIVE) = {g(HEAT_BASE_MW)} MW base + {g(HEAT_MW_PER_K)} MW per C below {g(HEATING_BASE_C)} C, capped at "
      f"{g(HEAT_NETWORK_MAX_MW)} MW, at {g(HEAT_SUPPLY_C)} C supply. It peaks when cooling need is lowest (correlation of demand with ambient: "
      + ", ".join(f"{C['scenarios'][k]['label']} {r['ambient']:.2f}" for k, r in C["pue_heat_correlation"].items()) + ").")
    T([["Month", "Sub-arctic ambient", "Demand (MW)", "Heat sold (MWh)", "Temperate demand (MW)", "Temperate sold (MWh)"]] +
      [[a["month"], f"{a['ambient_c']:.1f} C", f1(a["heat_demand_mw"]), f0(a["heat_sold_mwh"]), f1(t["heat_demand_mw"]), f0(t["heat_sold_mwh"])]
       for a, t in zip(SM["subarctic_hybrid"], SM["v2_temperate_underground"])])
    FH = C["flat_heat_assumption_heat_sold"]
    P(f"Annual heat sold: sub-arctic {f0(S['subarctic_hybrid']['heat_sold_mwh'])} MWh_th, temperate {f0(S['v2_temperate_underground']['heat_sold_mwh'])}, "
      f"warm {f0(S['warm_surface']['heat_sold_mwh'])}. v2's flat assumption (20 MW, half the year) gives {f0(FH['v2_temperate_underground'])} everywhere. "
      "A cold climate is worth more as a heat market than as a cooling bonus.")
    OF = C["orc_fixed_sink_15c_battery_e_out"]
    P(f"ORC discharge with a seasonal sink: in the sub-arctic surface case ORC efficiency is {pc(SM['subarctic_surface'][0]['battery_discharge_eff'])} in Jan vs "
      f"{pc(SM['subarctic_surface'][6]['battery_discharge_eff'])} in Jul; annual battery output {f0(S['subarctic_surface']['battery_e_out_mwh'])} MWh_e vs "
      f"{f0(OF['subarctic_surface'])} with a fixed 15 C sink (hybrid with fjord: {f0(S['subarctic_hybrid']['battery_e_out_mwh'])} vs {f0(OF['subarctic_hybrid'])}). "
      "Real gain, small; and in winter the heat is better sold than stored.")

    H("19. Scenarios: v2 evolved vs sub-arctic (annual)")
    keys = list(C["scenarios"])
    rows = [["Annual"] + [C["scenarios"][k]["label"] for k in keys]]
    for lab, k, fmt in (("Facility energy (MWh)", "facility_mwh", f0), ("PUE incl. site lines", "annual_pue", lambda v: f"{v:.3f}"),
                        ("PUE cooling only", "cooling_pue", lambda v: f"{v:.3f}"), ("Site parasitics (MWh)", "site_mwh", f0),
                        ("Heat sold (MWh_th)", "heat_sold_mwh", f0), ("Battery electricity (MWh_e)", "battery_e_out_mwh", f0),
                        ("Useful output (MWh)", "useful_mwh", f0), ("Net grid purchases (MWh)", "grid_mwh", f0),
                        ("Heat to environment (MWh)", "heat_env_mwh", f0)):
        rows.append([lab] + [fmt(S[x][k]) for x in keys])
    T(rows)
    P("All four run the v2 evolved composer (direct-to-chip, 30% flexible compute, 8 h LAES with cold reuse) once per month on that month's PUE, sink and heat demand; "
      "every monthly column balances. Sites are generic and profiles ILLUSTRATIVE. The underground hybrid draws slightly more than the surface site because of "
      "its ILLUSTRATIVE underground parasitic lines; its case is security, footprint and a steady sink, not energy.")

    H("20. Seasonal supply temperature and boosting (new in v4)")
    P(f"Supply temperature (ILLUSTRATIVE weather compensation) on the sub-arctic profile, by month: "
      + ", ".join(f"{MONTH_NAMES[i]} {t:.1f} C" for i, t in enumerate(V4["supply_sub_arctic"])) + ". "
      f"Loops (ILLUSTRATIVE): air {g(COOLING['air']['temp_c'])} C, direct-to-chip {g(COOLING['direct_to_chip']['temp_c'])} C, immersion {g(COOLING['immersion']['temp_c'])} C. "
      f"A loop feeds directly only if it is at least {g(HX_APPROACH_C)} C above supply.")
    rows = [["Scenario / cooling", "Boosted months", "Boosted heat (MWh_th)", "Boost electricity (MWh_e)", "Heat sold v3", "Heat sold v4",
             "Useful v3", "Useful v4", "Grid v3", "Grid v4"]]
    for (k, c), r in vp.items():
        a, b = r["v3"], r["v4"]
        rows.append([f"{CLIMATE_SCENARIOS[k]['label']} / {c}", f"{b['boosted_months']:.0f}", f0(b["boosted_heat_mwh"]), f0(b["boost_e_mwh"]),
                     f0(a["heat_sold_mwh"]), f0(b["heat_sold_mwh"]), f0(a["useful_mwh"]), f0(b["useful_mwh"]), f0(a["grid_mwh"]), f0(b["grid_mwh"])])
    T(rows)
    hd = vp[("subarctic_hybrid", "direct_to_chip")]
    P(f"Read-out, sub-arctic hybrid, direct-to-chip: {f0(hd['v4']['boosted_heat_mwh'])} of {f0(hd['v4']['heat_sold_mwh'])} MWh_th sold needs boosting, "
      f"costing {f0(hd['v4']['boost_e_mwh'])} MWh_e (v3: {f0(hd['v3']['boost_e_mwh'])}). Heat sold does not fall, because the hot stream is far larger than the capped demand; "
      f"the booster draws less from the loop (its electricity becomes heat too), leaving slightly more for the battery, so useful output moves "
      f"{f0(hd['v3']['useful_mwh'])} → {f0(hd['v4']['useful_mwh'])} MWh while grid purchases rise {f0(hd['v3']['grid_mwh'])} → {f0(hd['v4']['grid_mwh'])} MWh. "
      "Hotter loops (immersion) or a summer-only direct feed cut the boost; a warm site with a 60 C loop needs none.")
    rows = [["Network cap (ILLUSTRATIVE)"] + [f"{CLIMATE_SCENARIOS[k]['label']}: sold / boost e / grid" for k in V4["caps"]]]
    for cap in CAP_SENSITIVITY_MW:
        rows.append([f"{g(cap)} MW"] + [f"{f0(V4['caps'][k][cap]['heat_sold_mwh'])} / {f0(V4['caps'][k][cap]['boost_e_mwh'])} / {f0(V4['caps'][k][cap]['grid_mwh'])}"
                                       for k in V4["caps"]])
    T(rows)
    P(f"The {g(HEAT_NETWORK_MAX_MW)} MW cap binds in {V4['capped_months_subarctic']} of 12 sub-arctic months, and the hot stream could always supply more: "
      "in the coldest months heat sales are set by the network size, not by the data center. Heat sold moves much more with the cap than boost "
      "electricity or grid purchases do, so the cap is one of the most influential ILLUSTRATIVE numbers in the heat result.")

    H("21. Price-timed dispatch (new in v5)")
    bv, ev = B["battery_value"], E["battery_value"]
    P("The battery's energy model is unchanged. New in v5: its charge electricity is valued at the mean of the 12 cheapest hours "
      "of the ILLUSTRATIVE 24 h price signal and its discharge at the mean of the 6 priciest hours (the same relative units as the "
      "flexible-compute cost index, section 9). This is not money and not economics; it is the spread argument made legible.")
    T([["Price-weighted (relative units)", "Baseline", "Evolved"],
       ["Charge cost index", f"{bv['charge_cost_index']:.0f}", f"{ev['charge_cost_index']:.0f}"],
       ["Discharge value index", f"{bv['discharge_value_index']:.0f}", f"{ev['discharge_value_index']:.0f}"],
       ["Value ratio (discharge / charge)", f"{bv['value_ratio']:.2f}", f"{ev['value_ratio']:.2f}"],
       ["Round-trip efficiency (for reference)", pc(B['battery_rte']), pc(E['battery_rte'])]])
    P(f"Read plainly: at this ILLUSTRATIVE spread the baseline battery's discharged electricity is worth about {bv['value_ratio']:.0%} "
      "of its charge electricity in price-weighted terms. Energy arbitrage alone does not carry this battery; its case is firm capacity, "
      "diesel-free backup, and heat. Flexible compute (section 9) has no round-trip loss, so it always wins on spread: shift load first, store second.")

    H("22. No law broken")
    BL(["Thermodynamics: no law broken at any step; every column balances (asserted) and every RTE ≤ 100% by construction (rte_guard).",
        "Engineering: built from mature pieces: molten salt, ORC, LAES, HT-ATES, district heating, liquid cooling. The new part is how they are combined.",
        "Economics: not claimed here. It lives or dies on peak/off-peak spread, the heat price and contract, the value of diesel-free backup, and site geology.",
        f"The lie it doesn't tell: it never claims free energy from waste heat. The {pc(B['battery_rte'])} baseline RTE is printed next to the "
        f"{f0(B['battery_net_loss'])} MWh_e net loss."])
    P("PROTOTYPE FOR EXPLORATION, NOT A DESIGN. ILLUSTRATIVE numbers are not measured data. The ledger reports; it does not rule.")
    return blocks


DOC_SECTIONS = ["Changes versus v3", "Changes versus v4", "Changes versus v2", "Changes versus v1", "1. The one non-negotiable", "2. Facility spec",
                "3. Subsystem A — Waste-heat Carnot battery (diurnal shifting)",
                "4. Subsystem B — LAES backup (diesel replacement)",
                "5. Subsystem C — Molten-salt arbitrage tier (optional)",
                "6. Why underground — the honest list", "7. Heat sales (new)", "8. Hot liquid cooling (new)",
                "9. Flexible compute (new)", "10. LAES cold reuse (new)", "11. Underground site requirements (new)",
                "12. Daily energy balance (first law, from EnergyFlow)",
                "13. Baseline versus evolved (annual-average day)", "14. Sensitivity: FRACTION_OF_CARNOT",
                "15. What the code does now / next", "16. Climate siting (new in v3)", "17. Seasonal PUE (new in v3)",
                "18. Seasonal heat sales (new in v3)", "19. Scenarios: v2 evolved vs sub-arctic (annual)",
                "20. Seasonal supply temperature and boosting (new in v4)",
                "21. Price-timed dispatch (new in v5)", "22. No law broken"]


def _wrap(text, width=96, indent="", first=None):
    import textwrap
    return textwrap.fill(text, width, initial_indent=indent if first is None else first, subsequent_indent=indent)


def _text_table(rows):
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    def line(r):
        return "  " + " | ".join(c.ljust(w[i]) if i == 0 else c.rjust(w[i]) for i, c in enumerate(r))
    return "\n".join([line(rows[0]), "  " + "-+-".join("-" * x for x in w)] + [line(r) for r in rows[1:]])


def render_text(blocks):
    out = [DOC_TITLE, "=" * len(DOC_TITLE)]
    for kind, v in blocks:
        if kind == "H":
            out += ["", v, "-" * len(v)]
        elif kind == "P":
            out += [_wrap(v), ""] if out[-1] != "" else [_wrap(v), ""]
        elif kind == "BL":
            out += [_wrap(i, indent="    ", first="  - ") for i in v] + [""]
        elif kind == "T":
            out += [_text_table(v), ""]
    return "\n".join(out).rstrip() + "\n"


def render_md(blocks):
    out = [f"# {DOC_TITLE}", ""]
    esc = lambda c: c.replace("|", "\\|")
    for kind, v in blocks:
        if kind == "H":
            out += [f"## {v}", ""]
        elif kind == "P":
            out += [v, ""]
        elif kind == "BL":
            out += [f"- {i}" for i in v] + [""]
        elif kind == "T":
            out.append("| " + " | ".join(esc(c) or " " for c in v[0]) + " |")
            out.append("|" + "|".join("---" if i == 0 else "---:" for i in range(len(v[0]))) + "|")
            out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in v[1:]]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def design_doc(md=False):
    b = doc_blocks()
    return render_md(b) if md else render_text(b)


def _tests_doc(check, raises):
    d = scenario_numbers()
    B, E = d["baseline"], d["evolved"]
    txt, md = design_doc(), design_doc(md=True)
    blocks = doc_blocks(d)
    check("doc renders (text)", isinstance(txt, str) and len(txt) > 5000)
    check("doc renders (md)", md.startswith("# ZUCKY CENTER") and "|---" in md)
    check("doc title", DOC_TITLE in txt)
    heads = [v for k, v in blocks if k == "H"]
    check("doc has exactly the 22 headings in order", heads == DOC_SECTIONS)
    for h in DOC_SECTIONS:
        check(f"text has heading '{h[:24]}'", h in txt)
        check(f"md has heading '{h[:24]}'", f"## {h}" in md)
    check("no 'viable' in text", "viable" not in txt.lower())
    check("no 'viable' in md", "viable" not in md.lower())
    f0 = lambda v: f"{v:,.0f}"
    pc = lambda v: f"{v*100:.1f}%"
    for label, s in (("baseline useful", f0(B["useful_output_mwh_day"])), ("evolved useful", f0(E["useful_output_mwh_day"])),
                     ("baseline grid", f0(B["net_grid_mwh_day"])), ("evolved grid", f0(E["net_grid_mwh_day"])),
                     ("evolved heat sold", f0(E["heat_sold_mwh_day"])), ("baseline battery e_in", f0(B["battery_e_in"])),
                     ("baseline battery RTE", pc(B["battery_rte"])), ("evolved battery RTE", pc(E["battery_rte"])),
                     ("baseline LAES charge", f0(B["laes"]["electricity_in_mwh"])),
                     ("evolved LAES charge", f0(E["laes"]["electricity_in_mwh"])),
                     ("evolved LAES effective RTE", pc(E["laes"]["effective_rte"])),
                     ("baseline facility MW", f"{B['facility_mw']:.1f} MW_e"),
                     ("baseline COP", f"{B['battery_cop']:.2f}"), ("evolved COP", f"{E['battery_cop']:.2f}")):
        check(f"doc number matches composer: {label}", s in txt and s in md)
    row = next(r for k, v in blocks if k == "T" for r in v if r[0].startswith("Useful output"))
    check("sec 13 row equals composer", row[1:] == [f0(B["useful_output_mwh_day"]), f0(E["useful_output_mwh_day"])])
    # live, not hardcoded: perturbing the composer output changes the doc
    d2 = scenario_numbers(); d2["evolved"]["useful_output_mwh_day"] = 987654.0
    check("doc numbers are live", "987,654" in render_text(doc_blocks(d2)) and "987,654" not in txt)
    check("ILLUSTRATIVE labels kept", txt.count("ILLUSTRATIVE") >= 15)
    check("ledger line kept", "The ledger reports; it does not rule." in txt)
    check("No law broken present", "No law broken" in txt)
    check("STANDING still False", STANDING is False)
    check("text tables aligned", all(len(set(len(l) for l in tb.splitlines())) == 1
                                     for tb in (_text_table(v) for k, v in blocks if k == "T")))
    check("self-contained (no sibling imports)", _no_sibling_imports())
    C = d["climate"]
    for h in ("Changes versus v2", "16. Climate siting", "17. Seasonal PUE", "18. Seasonal heat sales", "19. Scenarios"):
        check(f"v3 section present: {h}", h in txt and h in md)
    check("Economics not claimed kept", "Economics: not claimed here." in txt)
    for lab, s in (("orig total %", f"{C['original_example']['total_savings_pct']:.1f}%"),
                   ("orig overhead %", f"{C['original_example']['overhead_savings_pct']:.1f}%"),
                   ("subarctic heat sold", f0(C["scenarios"]["subarctic_hybrid"]["annual"]["heat_sold_mwh"])),
                   ("surface grid", f0(C["scenarios"]["subarctic_surface"]["annual"]["grid_mwh"]))):
        check(f"v3 doc number live: {lab}", s in txt and s in md)
    check("generic/ILLUSTRATIVE climate note", "GENERIC" in txt and "not measured climate data" in txt)
    V4 = d["v4"]
    for h in ("Changes versus v3", "20. Seasonal supply temperature and boosting"):
        check(f"v4 section present: {h}", h in txt and h in md)
    hd = V4["per"][("subarctic_hybrid", "direct_to_chip")]["v4"]
    for lab, s in (("boost e", f0(hd["boost_e_mwh"])), ("boosted heat", f0(hd["boosted_heat_mwh"])),
                   ("cap 20 sold", f0(V4["caps"]["subarctic_hybrid"][20.0]["heat_sold_mwh"])),
                   ("cap 40 sold", f0(V4["caps"]["subarctic_hybrid"][40.0]["heat_sold_mwh"]))):
        check(f"v4 doc number live: {lab}", s in txt and s in md)
    check("v4 doc no 'viable'", "viable" not in txt.lower() and "viable" not in md.lower())
    for h in ("Changes versus v4", "21. Price-timed dispatch"):
        check(f"v5 section present: {h}", h in txt and h in md)
    bv = B["battery_value"]
    for lab, s in (("battery charge cost index", f0(bv["charge_cost_index"])),
                   ("battery discharge value index", f0(bv["discharge_value_index"])),
                   ("battery value ratio", f"{bv['value_ratio']:.2f}")):
        check(f"v5 doc number live: {lab}", s in txt and s in md)
    check("v5 doc no 'viable'", "viable" not in txt.lower() and "viable" not in md.lower())


def _no_sibling_imports():
    import ast, os
    try:
        src = open(os.path.abspath(__file__)).read()
    except OSError:
        return True
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import): mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom): mods.add((n.module or "").split(".")[0])
    return not mods & {"zucky_center", "underground_energy", "sco2_generator", "carnot_battery", "make_zucky_v2_pdf", "zucky_center_v2", "zucky_center_v3", "zucky_center_v4"}


_TEST_COUNT_CACHE = {}
_COUNTING = False   # while True, doc renders skip the count line instead of counting


def _count_tests():
    """Count checks by running them silently (cached). Used by section 15.

    v5: a re-entrancy flag replaces the old partial-cache dance. Any doc render that
    happens inside the silent run (via _tests_doc) prints a placeholder line instead
    of counting, so the count can never recurse and can never read a half-filled cache."""
    global _COUNTING
    if _COUNTING:
        return {"total": -1, "core": -1, "doc": -1}
    if not _TEST_COUNT_CACHE:
        n = {"core": 0, "doc": 0}
        def mk(key):
            def check(name, cond): n[key] += 1
            return check
        def raises(fn):
            try: fn(); return False
            except ValueError: return True
        import io, contextlib
        _COUNTING = True
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                for fn in (_tests_sco2, _tests_battery, _tests_laes, _tests_zucky,
                           _tests_climate, _tests_v4, _tests_v5):
                    fn(mk("core"), raises)
                # the shared critical-flag check at the end of run_tests, counted explicitly
                mk("core")("critical flag shared", "CRITICAL POINT" in critical_point_flag(25))
                _tests_doc(mk("doc"), raises)
        finally:
            _COUNTING = False
        _TEST_COUNT_CACHE.update(core=n["core"], doc=n["doc"], total=n["core"] + n["doc"])
    return _TEST_COUNT_CACHE


def _jsonable(o):
    """Recursively stringify tuple dict keys (v4 'per' uses (scenario, cooling) keys)."""
    if isinstance(o, dict):
        return {("/".join(map(str, k)) if isinstance(k, tuple) else k): _jsonable(v)
                for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(x) for x in o]
    return o


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(0 if run_tests() else 1)
    if "--json" in sys.argv:
        import json
        print(json.dumps(_jsonable(scenario_numbers()), indent=1, default=str))
        sys.exit(0)
    if "doc" in sys.argv[1:]:
        sys.stdout.write(design_doc(md="--md" in sys.argv))
        sys.exit(0)
    print(example_center())
    print()
    print(compare_climates())
    print()
    print(compare_v4())
    print()
    print(report_cooling(original_example_numbers()))
