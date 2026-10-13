# Compatibility Analysis: Hearth, llama.cpp, NEDB, AiAssist Secure

Project Imagine, temporal grounding + native tool use. Analysis track.
Date: 2026-10-13. v12 is frozen; this phase is planning only.

Working principle for all four components: "The model proposes. The host
authorizes. The database verifies." The `<<<TOOL_CALL>>>` envelope is a
protocol between the model and the host; the host-side orchestration is a
parallel build, and this analysis keeps the contract clean so both tracks
proceed independently.

---

## 1. Hearth

**What it is.** Hearth is Mark's model residency protocol: a Rust core
(published on crates.io and PyPI as hearth-engine, and on npm as
`@interchained/hearth`) that keeps declared models warm and reports honest
named states, Unknown, Loading, Resident, Lost with Evicted and GpuDetached
variants, Failed, and Stopped, so routers get actionable answers instead of
ambiguous timeouts. It is the residency layer for the PIN p2p inference
network, and on this box a Hearth Fleet declaration took imagine-v12 from
load_started through probe_ok to Resident, which is how the model currently
serves.

**What changes.** Tool availability must be declared as a *host capability*,
not as model state. A Fleet declaration says imagine-v12 is Resident; it says
nothing about whether `schema.inspect` or `nedb.provenance` are mounted on
that host. The admission path that routes a request to a Resident model needs a
parallel capability check against the host's tool registry before the request
is accepted as tool-capable. Concretely: the declaration or the router query
gains a capability dimension (which tools this host will execute for this
model), separate from the residency state machine, which itself is untouched.

**What does NOT change.** The residency state machine, the named states, the
warm/loading semantics, and the probe flow. A tool-capable model loads,
resides, and is lost exactly like a direct-answer model. Hearth never parses
envelopes, never executes tools, and never sees tool observations; it answers
one question, "is the model ready to take requests," and that question is
unchanged.

**Open questions.**
- Does the capability declaration live in the Fleet config schema, in a
  sidecar registry the router consults, or entirely inside AiAssist Secure's
  orchestration layer with Hearth unaware? The analysis recommends the
  registry live with the orchestrator and Hearth carry only a capability
  pointer, but this is undecided.
- Should a host whose tool registry is unhealthy (e.g. NEDB unreachable) keep
  the model Resident for direct answers while refusing tool-capable routing?
  That split behavior needs a router-level decision.

---

## 2. llama.cpp

**What it is.** llama.cpp is the inference runtime serving Imagine v12: a
llama-server built from Mark's aiassistsecure/llama.cpp fork (carrying the
imagine tokenizer fix), running the imagine-v12 Q4_K_M GGUF on port 8081 with
context length 8192 as a persistent systemd service. It does token prediction
over a GGUF file. Everything about envelopes, the gate, and tool orchestration
lives outside it today.

**What changes.** Almost nothing in the runtime, but two host-side budgets
need explicit accounting. First, context window: tool observations are text
injected into the prompt between model turns, so the 8192-token budget must
cover the system prompt plus schema plus the tool call plus potentially large
observations plus the final generation. The host needs a declared budget split
(and truncation policy for observations) before the first multi-turn loop runs.
Second, the host's stop-and-act hook: when the model emits `<<<TOOL_CALL>>>`,
generation stops and control passes to the orchestrator. Whether that hook is
implemented as a stop sequence on the existing `<<<END>>>` marker plus
host-side dispatch, or as a dedicated server-side hook, is a host-track
implementation choice; either way the model file and the sampler are
untouched.

**What does NOT change.** GGUF inference is unchanged: no new model format, no
grammar-constrained decoding requirement for v1 (the deterministic parser
track validates envelopes after generation, the way `sentinel.py` extracts by
delimiter today), and no changes to Mark's llama.cpp fork. Envelope parsing
stays host-side, exactly as `stealth/sentinel.py` does it now: find the
delimiters, slice, never re-parse the payload.

**Open questions.**
- The exact context budget split (prompt vs observations vs generation) and
  the observation truncation policy. Large `sql.execute` results are the
  obvious pressure point.
- Latency behavior of multi-turn tool loops through llama-server: acceptable
  as-is, or does the host need streaming or caching of the prefix across
  turns? Unmeasured; the baseline plan's Tier 2 follow-through probes will
  produce the first numbers.
- Whether the fork needs any change at all for v1. Current answer: no, but
  the host-track prototype should confirm the stop-and-act hook works with the
  existing server before declaring this closed.

---

## 3. NEDB

**What it is.** NEDB is Mark's schemaless embedded database: content-addressed
Merkle DAG, hash-chained, append-only history, with a pgwire endpoint that
speaks PostgreSQL wire protocol through his own vendored parser. It is
bi-temporal: `AS OF <seq>` queries transaction time (when a row was written)
and `VALID AS OF "<date>"` queries valid time (when a row was true in the
world), and causal provenance travels with each row as selectable columns
(`_id`, `_hash`, history). The repo's execution gate already uses the pgwire
endpoint plus time travel to verify writes, which is why NEDB is the natural
backing store for the v1 tools.

**What changes.** The v1 tools map onto surfaces NEDB already exposes, so the
changes are wiring, not engine work. `schema.inspect` and `sql.execute` /
`sql.explain` are served through the pgwire endpoint the gate already talks
to. `nedb.provenance` maps to the bi-temporal queries: transaction-time
history via `AS OF <seq>` and valid-time via `VALID AS OF`, with the
provenance columns selectable like any other column. The important semantic
the tool must preserve is the event-time vs transaction-time distinction,
which NEDB already models and ordinary databases do not; the tool's contract
should expose both explicitly rather than collapsing them into one timestamp.

**What does NOT change.** No engine changes are needed for v1: no new storage
format, no new query language, no change to the append-only history or the
vendored parser. NEDB stays schemaless, which means the gate-side schema
catalog (`stealth/schema.py`, and the `CATALOG` in `stealth/schemas.py`)
remains the authority on what tables and columns exist; the engine will plan
whatever it is given, exactly as the gate's L2 BIND level documents.

**Open questions.**
- Should `schema.inspect` read from the gate's schema catalog or query NEDB
  directly? Because NEDB is schemaless, a catalog-backed inspect is the
  honest answer for what the model may reference, but the two could drift.
  Undecided; the analysis leans catalog-backed with a drift check.
- Whether pgwire `EXPLAIN` output is sufficient for what `sql.explain` needs
  to tell the model, or whether the tool should add a normalized summary.
  Unmeasured.
- Row-cap and timeout enforcement for `sql.execute` currently live in the
  gate's L3; the tool path needs the same bounds enforced host-side even when
  the gate is not in the loop. That enforcement point is a host-track design
  decision.

---

## 4. AiAssist Secure / PIN

**What it is.** AiAssist Secure is Mark's p2p inference network (PIN): a
daemon-based network where nodes connect over SSE streaming and websocket
handshakes through a relay, and this box participates as the `muse-box` node
running pin-clientd against the gateway. It is the serving path through which
Imagine reaches end users and Mark's own agents. The tool-use feature lands
here as a host-side orchestration layer: the model proposes a tool call, and
everything after that, validation, execution, observation return, is the
host's job.

**What changes.** The orchestration layer owns four things the model must
never own. (1) The tool registry: a closed, versioned list of the five v1
tools with their argument schemas; the model names a tool, the host resolves
it against the registry, and unknown names are rejected. (2) Argument
validation: every argument is validated against the registry schema before
anything executes; this is where malformed JSON and out-of-range arguments
die. (3) Permission enforcement: `sql.execute` is read-only by construction
(SELECT/WITH only, row caps, statement timeouts, same L1 safety shape as the
gate), and anything write-shaped requires explicit human approval plus host
enforcement, never model assertion. (4) The audit trail: every tool call, its
arguments, its observation, and the final grounded answer are logged as one
traceable request lifecycle. Tool observations re-enter the model explicitly
marked as tool-originated, untrusted data; they cannot redefine permissions,
identity, or instructions.

**What does NOT change.** The relay and daemon transport are unchanged:
connect, handshake, streaming, and result uplinks work exactly as today, and
tool observations travel as ordinary inference traffic between turns. The
network topology, operator trust model, and node registration are untouched.
The model file served through the network is still v12, frozen.

**Open questions.**
- Where the registry physically lives: in the orchestrator beside the relay,
  in pin-clientd on each node, or both with the node carrying a signed copy.
  The analysis recommends a single orchestrator-owned registry for v1 to avoid
  version skew across nodes, but this is undecided.
- The audit trail's storage and retention: what is logged, where it lands,
  and how long it is kept. Needs a decision before any user-facing tool call.
- The exact human-approval UX for the write path: approval exists as a
  requirement, but no flow is designed yet. v1 tools are read-only, so this
  can trail the read-only launch, but the enforcement hook must exist before
  any write-shaped tool is registered.

---

## Cross-component notes

- **Contract ownership.** The `<<<TOOL_CALL>>>` envelope grammar and the tool
  JSON schemas are owned by the protocol RFC and deterministic parser tracks.
  Hearth, llama.cpp, NEDB, and AiAssist Secure all consume that contract;
  none of them redefine it.
- **Failure direction.** Every component fails closed toward direct answers:
  unhealthy tool registry, unreachable NEDB, or unparseable envelope all
  degrade to the v12 behavior that exists today (answer directly, or decline
  with UNANSWERABLE/CLARIFY), never to an unauthenticated tool execution.
- **The one new shared concept** is the host capability declaration from
  section 1: the router must know which hosts will execute tools before it
  routes a tool-capable request. Everything else is existing machinery
  pointed at a new protocol.
