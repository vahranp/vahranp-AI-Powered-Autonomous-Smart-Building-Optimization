"""Bridges the synchronous EnergyPlus control loop to the async MCP client
session and the tool-calling agent pipeline.

Spawns mcp_server/server.py ONCE as a stdio subprocess for the whole run
and keeps the ClientSession alive across many control ticks (a fresh
`asyncio.run()` per tick would tear down the subprocess connection each
time, so this manually drives a single persistent event loop instead).

Per control tick: write the real EnergyPlus state to data/live_state.json
-> three specialists (tool-calling, forced structured output via a local
`propose_setpoints` schema, not a real MCP tool) each propose setpoints for
all zones in one call -> arbiter (tool-calling against the REAL MCP tools,
sourced live from the server's list_tools()) resolves them and actually
calls set_zone_setpoint/set_lighting_level over MCP.

The actual model calls go through _chat() below. This project runs entirely
against a local Ollama server -- no hosted/third-party LLM API is used
anywhere, by design, so the building's live sensor state never leaves the
machine.
"""
import json
import os
import asyncio

import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from guardrails.validator import MIN_DEADBAND_C

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
LIVE_STATE_PATH = os.path.join(PROJECT_ROOT, "data", "live_state.json")
os.makedirs(os.path.dirname(LIVE_STATE_PATH), exist_ok=True)  # data/ isn't tracked by git; don't assume it exists

MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
# Ollama's default temperature (~0.7-0.8) is tuned for varied free-text
# generation, not for reliably following "always default to the cheap end
# of the range" and emitting clean structured tool arguments. Low
# temperature here reduces run-to-run variance in the proposals and should
# lower the rate of malformed tool calls _normalize_proposals/arbiter_decide
# have to defensively drop -- more of each expensive invocation's real
# reasoning survives into an actual applied setpoint.
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))
# Ollama unloads an idle model after 5 minutes by default. Gaps between
# invocations (LLM_HEARTBEAT_TICKS x LLM_TICK_PACING_S, or longer when
# triggers are sparse) can approach or exceed that, forcing a cold reload
# mid-run -- pure latency waste that shrinks the effective invocation budget
# for no benefit. Keeping the model resident for the life of the run avoids it.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")


def _chat(system_prompt: str, user_prompt: str, tools: list) -> tuple[str, list]:
    """One real LLM call against the local Ollama server. Returns (content,
    tool_calls), where tool_calls is a list of {"function": {"name": str,
    "arguments": str|dict}} -- the shape specialist_propose/arbiter_decide
    expect. Tool schemas (PROPOSE_TOOL, the real MCP tool list) are the
    standard JSON-Schema function-calling format Ollama accepts as-is."""
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
    response = ollama.chat(
        model=MODEL, messages=messages, tools=tools, keep_alive=OLLAMA_KEEP_ALIVE,
        options={"temperature": LLM_TEMPERATURE},
    )
    message = response["message"]
    return message.get("content", "") or "", message.get("tool_calls") or []

PROPOSE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_setpoints",
        "description": "Propose heating/cooling setpoints for each zone given the current building state.",
        "parameters": {
            "type": "object",
            "properties": {
                "proposals": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "zone": {"type": "string"},
                            "heating_c": {"type": "number"},
                            "cooling_c": {"type": "number"},
                            "reason": {"type": "string"},
                        },
                        "required": ["zone", "heating_c", "cooling_c", "reason"],
                    },
                },
            },
            "required": ["proposals"],
        },
    },
}


def _parse_tool_args(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


def _normalize_proposals(args: dict) -> dict:
    """Small local models are inconsistent: 'proposals' sometimes comes back
    as an already-parsed list, sometimes as a JSON-encoded string nested
    inside the tool arguments, and individual entries sometimes have
    null/missing heating_c or cooling_c. Normalize defensively rather than
    trust the shape -- a bad zone entry should be dropped, not crash the run."""
    proposals = args.get("proposals", [])
    if isinstance(proposals, str):
        try:
            proposals = json.loads(proposals)
        except (json.JSONDecodeError, TypeError):
            proposals = []
    valid = []
    for p in proposals if isinstance(proposals, list) else []:
        if not isinstance(p, dict):
            continue
        if not isinstance(p.get("heating_c"), (int, float)) or not isinstance(p.get("cooling_c"), (int, float)):
            continue
        if not isinstance(p.get("zone"), str):
            continue
        valid.append(p)
    return {"proposals": valid, "dropped": len(proposals) - len(valid) if isinstance(proposals, list) else 0}


class MCPAgentBridge:
    """Owns one persistent event loop + MCP stdio subprocess for the run."""

    def __init__(self, on_event=None):
        # on_event(phase: str, detail: dict), called synchronously right
        # after each real MCP resource read / tool call / specialist LLM
        # call completes -- lets the caller (energyplus_loop.py) log genuine
        # pipeline events for the Operations Console without this bridge
        # needing to know about the DB or the current tick number.
        self._on_event = on_event or (lambda phase, detail: None)
        self._loop = asyncio.new_event_loop()
        self._stdio_cm = None
        self._session_cm = None
        self._session: ClientSession | None = None
        self.mcp_tools_schema = []

    def start(self):
        self._loop.run_until_complete(self._start())

    async def _start(self):
        server_params = StdioServerParameters(command="python", args=["-m", "mcp_server.server"], cwd=PROJECT_ROOT)
        self._stdio_cm = stdio_client(server_params)
        read, write = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        tools_result = await self._session.list_tools()
        self.mcp_tools_schema = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.inputSchema},
            }
            for t in tools_result.tools
        ]

    def stop(self):
        # anyio's cancel scopes are task-bound: _start() and every per-tick
        # call ran as separate top-level tasks via run_until_complete(), so
        # __aexit__ here (entered in _start()'s task) legitimately raises
        # "different task" from anyio -- it's a real asyncio/anyio
        # constraint given this bridge's call pattern, not a sign the run
        # itself failed. The subprocess is already reaped when the
        # interpreter exits either way; suppress rather than let a cosmetic
        # teardown error mask a real, already-completed run.
        try:
            self._loop.run_until_complete(self._stop())
        except RuntimeError as e:
            if "cancel scope" not in str(e):
                raise
        finally:
            self._loop.close()

    async def _stop(self):
        await self._session_cm.__aexit__(None, None, None)
        await self._stdio_cm.__aexit__(None, None, None)

    def write_live_state(self, zones: dict, energy: dict, carbon_and_weather: dict, recent_issues: list | None = None):
        with open(LIVE_STATE_PATH, "w") as f:
            json.dump({
                "zones": zones, "energy": energy, "carbon_and_weather": carbon_and_weather,
                "recent_issues": recent_issues or [],
            }, f)

    def call_mcp_tool(self, name: str, args: dict) -> dict:
        result = self._loop.run_until_complete(self._call_mcp_tool(name, args))
        self._on_event("mcp_tool_call", {"tool": name, "args": args, "result": result})
        return result

    async def _call_mcp_tool(self, name, args):
        result = await self._session.call_tool(name, args)
        return json.loads(result.content[0].text)

    def read_resource(self, uri: str) -> str:
        """Real MCP resource read -- used for the self-correction loop: the
        server-side resource parses recent guardrail interventions AND tails
        EnergyPlus's own error/warning log, so the arbiter sees genuine
        'what went wrong recently' feedback, not a fabricated summary."""
        text = self._loop.run_until_complete(self._read_resource(uri))
        self._on_event("mcp_resource_read", {"uri": uri, "chars": len(text)})
        return text

    async def _read_resource(self, uri: str) -> str:
        result = await self._session.read_resource(uri)
        return result.contents[0].text if result.contents else ""

    def specialist_propose(self, system_prompt: str, user_prompt: str, agent_name: str = "specialist") -> dict:
        try:
            content, tool_calls = _chat(system_prompt, user_prompt, [PROPOSE_TOOL])
        except Exception:
            # A transient connection failure (httpx.RemoteProtocolError etc --
            # observed for real: local Ollama dropped a connection mid-request
            # under concurrent load) is functionally the same problem as the
            # model returning nothing usable. Treat it the same way -- empty --
            # so the retry below gets a real chance to recover it instead of
            # the exception aborting this specialist's whole contribution (and,
            # since this propagates up through run_llm_agent_pipeline, the
            # entire invocation) outright.
            content, tool_calls = "", []
        if not tool_calls:
            result = {"proposals": [], "raw_text": content}
        else:
            args = _parse_tool_args(tool_calls[0]["function"]["arguments"])
            result = _normalize_proposals(args if isinstance(args, dict) else {})
        if not result.get("proposals"):
            # A real run showed each specialist coming back with ZERO usable
            # proposals (no tool call at all, or a tool-call-shaped blob
            # emitted as free text with formula-like values, e.g.
            # "heating_c": 21.0 if (22.04 - 21.02) > 0.5 else 18.5) in
            # roughly 40-60% of invocations -- for energy (the highest-
            # weighted specialist at 0.55) that meant its voice was silent
            # for well over half the expensive strategic calls, with nothing
            # recovering it (unlike the arbiter's own missing-zone retry
            # below). One bounded retry, explicit that only the tool call is
            # wanted, recovers real signal without materially changing the
            # invocation's total cost -- it only fires when the first
            # attempt actually produced nothing.
            retry_prompt = (
                f"{user_prompt}\n\nYour previous response did not include a valid propose_setpoints tool "
                f"call. Call the propose_setpoints tool now -- heating_c and cooling_c must be plain "
                f"numbers (e.g. 18.5), never a formula or conditional expression. Respond with the tool "
                f"call only, no other text."
            )
            try:
                content, tool_calls = _chat(system_prompt, retry_prompt, [PROPOSE_TOOL])
            except Exception:
                content, tool_calls = "", []
            if tool_calls:
                args = _parse_tool_args(tool_calls[0]["function"]["arguments"])
                result = _normalize_proposals(args if isinstance(args, dict) else {})
            else:
                result = {"proposals": [], "raw_text": content}
        self._on_event("llm_specialist", {"agent": agent_name, "proposal_count": len(result.get("proposals", []))})
        return result

    def arbiter_decide(self, system_prompt: str, user_prompt: str) -> tuple[dict, dict, str]:
        """Returns ({zone: {heating_c, cooling_c, rationale}}, {zone: pct}, raw arbiter text).
        Prepends real recent-issues context (read via the MCP resource, see
        read_resource) to the user prompt so the arbiter can self-correct
        against its own recent guardrail interventions, not just react to
        the current tick in isolation.

        Both set_zone_setpoint AND set_lighting_level tool calls are
        captured -- an earlier version only extracted setpoints, so a
        lighting tool call would round-trip over real MCP and then get
        silently discarded, leaving lighting rule-controlled even under the
        "llm" strategy despite the docstring's claim otherwise."""
        try:
            recent_issues_text = self.read_resource("building://recent_issues")
        except Exception:
            recent_issues_text = ""
        full_prompt = (
            f"{user_prompt}\n\nRecent issues (self-correction context, via MCP resource):\n{recent_issues_text}"
        )
        try:
            content, tool_calls_raw = _chat(system_prompt, full_prompt, self.mcp_tools_schema)
        except Exception:
            # Same defensive treatment as specialist_propose -- a transient
            # connection failure here previously propagated all the way up
            # through arbiter.resolve() and run_llm_agent_pipeline() uncaught,
            # aborting the whole invocation (still caught by _llm_worker's
            # outer try/except so it wouldn't crash the run, but the
            # specialists' work for this invocation was wasted for nothing
            # the arbiter itself did wrong). Treat it as "no tool calls" so
            # the caller's own missing-zone retry gets a chance to recover.
            content, tool_calls_raw = "", []
        decisions = {}
        lighting_decisions = {}
        for call in tool_calls_raw:
            name = call["function"]["name"]
            args = _parse_tool_args(call["function"]["arguments"])
            if not isinstance(args, dict) or not isinstance(args.get("zone"), str):
                continue
            if name == "set_zone_setpoint" and not (
                isinstance(args.get("heating_c"), (int, float)) and isinstance(args.get("cooling_c"), (int, float))
            ):
                continue  # small local models occasionally emit null/missing numerics -- skip, don't crash
            if name == "set_zone_setpoint" and args["cooling_c"] - args["heating_c"] < MIN_DEADBAND_C:
                # A real run showed the model calling set_zone_setpoint TWICE
                # for the same zone within one turn -- e.g. a sane first call
                # (20.75/25.25), then a second, contradicting call for the
                # SAME zone (24/18.5, heating ABOVE cooling) later in the
                # same response. Both round-trip over real MCP and "accepted"
                # just means the zone name was valid, not that the pair makes
                # sense -- the guardrail downstream would still clip it every
                # tick it's applied, but silently caching the invalid pair
                # means the zone's "genuine LLM decision" for the policy's
                # whole lifetime (up to ~40 ticks) is actually just the
                # guardrail's forced correction, never a real judgment call.
                # Reject here, before it ever reaches the cache, the same way
                # a malformed numeric type already is -- an earlier valid
                # call for this zone in the same turn is left standing rather
                # than being overwritten by this one.
                continue
            if name == "set_lighting_level" and not isinstance(args.get("pct"), (int, float)):
                continue
            try:
                result = self.call_mcp_tool(name, args)
            except Exception:
                continue
            if name == "set_zone_setpoint" and result.get("status") == "accepted":
                decisions[args["zone"]] = {
                    "heating_c": result["heating_c"],
                    "cooling_c": result["cooling_c"],
                    "rationale": result.get("rationale", ""),
                }
            elif name == "set_lighting_level" and result.get("status") == "accepted":
                lighting_decisions[args["zone"]] = result["pct"]
        return decisions, lighting_decisions, content
