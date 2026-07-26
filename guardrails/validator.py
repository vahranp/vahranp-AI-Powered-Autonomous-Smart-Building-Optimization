"""Hard safety bounds applied to every arbiter decision before it reaches
EnergyPlus. This is the reliability layer: log every clip/rejection so the
dashboard can surface "AI proposed X, guardrail corrected to Y" moments.

MIN_DEADBAND_C exists because of a real crash: EnergyPlus raises a SEVERE
error and TERMINATES the entire simulation (a fatal, not a warning) if the
effective heating setpoint isn't comfortably below the effective cooling
setpoint ("DualSetPointWithDeadBand: Effective heating set-point higher
than effective cooling set-point"). Clipping heating_c and cooling_c to
their own independent ranges is not sufficient to prevent this -- the two
ranges overlap (18-24 heating, 22-28 cooling), so two independently valid
clipped values can still be an invalid (inverted or too-tight) pair. This
was discovered by an LLM-driven run actually crashing EnergyPlus, not by
reading the manual -- the deadband check below is what would have caught
it before it reached the actuator.
"""

SAFE_HEATING_RANGE_C = (18.0, 24.0)
SAFE_COOLING_RANGE_C = (22.0, 28.0)
MIN_DEADBAND_C = 2.0


def validate_setpoint(zone: str, heating_c: float, cooling_c: float) -> dict:
    original_heating, original_cooling = heating_c, cooling_c

    clipped_heating = min(max(heating_c, SAFE_HEATING_RANGE_C[0]), SAFE_HEATING_RANGE_C[1])
    clipped_cooling = min(max(cooling_c, SAFE_COOLING_RANGE_C[0]), SAFE_COOLING_RANGE_C[1])

    if clipped_cooling - clipped_heating < MIN_DEADBAND_C:
        clipped_cooling = min(clipped_heating + MIN_DEADBAND_C, SAFE_COOLING_RANGE_C[1])
        clipped_heating = max(min(clipped_heating, clipped_cooling - MIN_DEADBAND_C), SAFE_HEATING_RANGE_C[0])

    intervened = clipped_heating != original_heating or clipped_cooling != original_cooling

    return {
        "zone": zone,
        "heating_c": round(clipped_heating, 2),
        "cooling_c": round(clipped_cooling, 2),
        "guardrail_intervened": intervened,
        "original": {"heating_c": original_heating, "cooling_c": original_cooling} if intervened else None,
    }
