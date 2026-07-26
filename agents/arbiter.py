"""Resolves the three specialists' proposals into one decision per zone by
calling the REAL MCP tools (set_zone_setpoint, set_lighting_level) via
Ollama tool-calling -- this is the one agent that actually acts, not just
proposes. The full specialist proposal text is passed in and logged
verbatim by the caller; that negotiation transcript is what the dashboard's
decision feed shows.

Self-correction: bridge.arbiter_decide() reads the building://recent_issues
MCP resource (real guardrail-intervention history + a real tail of
EnergyPlus's own error log) and appends it to the prompt below -- the
system prompt explicitly tells the model to use that feedback, closing a
genuine self-correction loop rather than deciding each tick in isolation.

Lighting: the specialists only reason about heating/cooling, so lighting
context (occupancy, daylight_lux) is given directly to the arbiter here --
it's the one agent instructed to call set_lighting_level too, using the
real daylight reading already in `zones`, not a guess.
"""
SYSTEM_PROMPT = (
    "You are the Arbiter for a building control system. You receive three "
    "proposals -- from an Energy specialist, a Comfort specialist, and a "
    "Carbon specialist -- which may conflict, plus objective weights, plus "
    "the raw zone state (occupancy, daylight_lux). Hard safe bounds, "
    "regardless of what any specialist proposed: 18.0-24.0C heating, "
    "22.0-28.0C cooling, at least 2.0C apart -- values outside this WILL be "
    "clipped by the guardrail layer, so never call set_zone_setpoint "
    "outside these bounds in the first place; that's a wasted call, not a "
    "safety net you can rely on. You will also be given recent issues: "
    "past guardrail interventions (cases where your prior proposals were "
    "out of safe bounds and got clipped) and recent EnergyPlus warnings/"
    "errors. Use that feedback to self-correct -- if a zone was recently "
    "clipped for exceeding a safe limit, propose within that limit this "
    "time rather than repeating the same mistake. When proposals conflict, "
    "default toward whichever is cheaper (Energy's or Carbon's) as long as "
    "it's still inside the Comfort specialist's stated range -- being "
    "inside that range at all satisfies comfort, regardless of where in "
    "the range it falls; don't average toward a tighter middle ground than "
    "any single proposal asked for, that only wastes energy. A prior real "
    "run measured average PMV of -0.04 and PPD of 6.8% -- comfort has "
    "consistently been far better than required, which means you have been "
    "resolving conflicts too conservatively; lean toward Energy's proposal "
    "more often, not less. COMPLETENESS IS CRITICAL: call set_zone_setpoint "
    "separately for EVERY SINGLE zone mentioned in the proposals -- not a "
    "sample, not just the first one or two, ALL of them, each as its own "
    "tool call, with the final heating_c/cooling_c and a short rationale "
    "explaining the tradeoff you made (mention if you adjusted for a recent "
    "issue). Skipping a zone is a real, tracked failure, not a minor "
    "omission. Call set_zone_setpoint EXACTLY ONCE per zone -- decide the "
    "final number before calling, don't call it again for a zone you "
    "already covered; a second, different call for the same zone silently "
    "overrides the first and a contradicting second guess is a real, "
    "tracked failure, not a refinement. heating_c and cooling_c must be "
    "plain numbers, never a formula or expression. ALSO call "
    "set_lighting_level, ONCE each, but ONLY for occupied zones whose "
    "daylight_lux is a real number (not null): use a high level (0.8-0.95) "
    "if daylight_lux is low, and dim it (0.1-0.4) if daylight_lux is "
    "already high -- daylight harvesting. Do NOT call set_lighting_level "
    "for a zone whose daylight_lux is null (e.g. Core_ZN) -- that zone has "
    "no windows and no daylight sensor, a fixed real-time rule already "
    "controls its lighting from live occupancy count with no latency, and "
    "any value you propose for it is ignored; skipping it is correct, not "
    "an omission."
)


def resolve(bridge, zones: dict, energy_proposal, comfort_proposal, carbon_proposal, weights: dict):
    """A real run showed the arbiter often only calling set_zone_setpoint
    for 1-2 of the occupied zones per invocation, not all of them -- small
    local models don't reliably honor "every zone" instructions in one
    tool-calling pass, and that incomplete coverage was the direct cause of
    "llm" strategy's cached-policy coverage staying well under the ~55-60%
    of decisions actually wanted. Rather than just repeating the
    instruction louder and hoping, this makes ONE targeted retry -- naming
    exactly which zones were missed -- if the first pass didn't cover every
    zone. A smaller, more specific task (just the missed zones) is
    realistically more likely for an 8B model to complete than the original
    all-zones-at-once call was."""
    zone_names = list(zones.keys())
    user_prompt = (
        f"Zone state (occupancy, daylight_lux): {zones}\n\n"
        f"You must call set_zone_setpoint for ALL {len(zone_names)} zones listed above: "
        f"{zone_names}. Do not omit any.\n\n"
        f"Objective weights: {weights}\n\n"
        f"Energy specialist proposals:\n{energy_proposal}\n\n"
        f"Comfort specialist proposals:\n{comfort_proposal}\n\n"
        f"Carbon specialist proposals:\n{carbon_proposal}"
    )
    decisions, lighting_decisions, arbiter_text = bridge.arbiter_decide(SYSTEM_PROMPT, user_prompt)

    # Checked independently -- a real run showed the arbiter call
    # set_zone_setpoint for every zone in one pass while calling
    # set_lighting_level for NONE of them (or vice versa). The original
    # retry only checked setpoint coverage, so a lighting-only gap was never
    # retried at all -- that zone's lighting silently fell back to whatever
    # an OLDER invocation had cached (or the deterministic rule), which is
    # exactly what left a lighting policy_issued_at_tick meaningfully older
    # than the co-cached setpoint's.
    # Zones with no daylight sensor (daylight_lux is null, e.g. Core_ZN) are
    # deliberately never expected to get a set_lighting_level call -- see the
    # system prompt above -- so they must never count as "missing" here, or
    # every single invocation would trigger a pointless retry chasing a call
    # that's correct to never make.
    lighting_eligible = [z for z in zone_names if zones.get(z, {}).get("daylight_lux") is not None]
    missing_setpoints = [z for z in zone_names if z not in decisions]
    missing_lighting = [z for z in lighting_eligible if z not in lighting_decisions]
    if missing_setpoints or missing_lighting:
        retry_lines = [user_prompt, "\nIMPORTANT: your previous response was incomplete."]
        if missing_setpoints:
            retry_lines.append(f"Call set_zone_setpoint for these missing zones: {missing_setpoints}.")
        if missing_lighting:
            retry_lines.append(f"Call set_lighting_level for these missing zones: {missing_lighting}.")
        retry_lines.append(
            "Do this now (in addition to, not instead of, any you already covered), using the same "
            "specialist proposals and resolution rules above."
        )
        retry_prompt = "\n".join(retry_lines)
        retry_decisions, retry_lighting, retry_text = bridge.arbiter_decide(SYSTEM_PROMPT, retry_prompt)
        decisions = {**retry_decisions, **decisions}  # keep first-pass results; fill only the gaps from the retry
        lighting_decisions = {**retry_lighting, **lighting_decisions}
        arbiter_text = (
            f"{arbiter_text}\n[retry covered missing setpoints {missing_setpoints}, "
            f"missing lighting {missing_lighting}]: {retry_text}"
        )

    return decisions, lighting_decisions, arbiter_text
