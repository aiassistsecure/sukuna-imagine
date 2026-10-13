# Risk Register: Imagine Temporal Grounding + Native Tool Use

Project Imagine. Analysis track. Date: 2026-10-13. v12 is frozen; this phase
is planning only, so several mitigations are specified as design requirements
for the model track and host track builds rather than as existing controls.

Owner values: **model track** (training, corpus, eval) or **host track**
(orchestration, registry, parser, enforcement). Some risks are shared.

| ID | Risk | Likelihood | Impact | Mitigation | Owner |
|---|---|---|---|---|---|
| R-01 | Tool misuse: the model chooses the wrong tool (e.g. `sql.execute` when the question needs `sql.explain`, or `clock.now` when the schema already answers it) | Medium | Medium | Closed registry with argument schemas so a wrong choice fails validation loudly; host cross-checks the chosen tool against the question type where cheap to do so; baseline Phase 4 tool-selection accuracy sizes the judgment slice of the corpus; judgment probes ship as permanent regression tests | Model track (judgment training), host track (validation) |
| R-02 | Malformed outputs: bad JSON, unterminated envelopes, prose around the block, or two blocks where exactly one is required | High | Medium | Deterministic delimiter extraction in the host parser (an unterminated block yields nothing, per the `sentinel.py` rule); strict JSON schema validation with bounded retries, then fall back to CLARIFY or direct answer; format negatives in the corpus; malformed rate is a baseline metric that gates corpus sizing | Model track (format training), host track (parser strictness) |
| R-03 | Prompt injection via tool observations: observation text contains instructions, permission claims, or identity redefinitions ("you are now X", "ignore the registry") and the model obeys them | High | High | Observations are untrusted data by construction: wrapped in origin-marked blocks, never concatenated into system instructions; permissions and identity are fixed host-side and cannot be altered by model input or observation content; corpus includes injection-attempt negatives where the correct behavior is to ignore the injected instruction and continue the task | Host track (marking and enforcement), model track (injection negatives) |
| R-04 | SQL execution hazards: unbounded SELECT (full-table scans, cartesian products) or accidental writes issued through `sql.execute` | Medium | High | `sql.execute` is read-only by construction: SELECT/WITH only, the gate's L1 safety shape (single statement, denylist, no DDL/DML), row caps, and statement timeouts enforced host-side on every call; the database role used by the tool path is read-only; anything write-shaped requires explicit human approval plus host enforcement, never model assertion | Host track |
| R-05 | Fabrication in the post-execution summary: the model invents rows, values, or conclusions beyond what the tool observation returned, presenting confabulation as grounded | Medium | High | Host verifies the final answer against the observed tool results (row counts and cited values cross-checked mechanically where feasible); grounded-summary examples in the corpus train "stay inside the observation"; baseline Tier 2 follow-through probes measure grounded-summary fidelity before training and as a regression after | Model track (grounded-summary training), host track (answer verification) |
| R-06 | Envelope smuggling and nested markers: a `<<<TOOL_CALL>>>` payload containing nested `<<<END>>>` or fake observation markers, or model output that forges a tool-originated observation block | Low | High | Exactly-one invariant: payloads are scanned for nested markers and rejected; the host parser extracts by delimiter and never re-parses payload bytes; observations are constructible only by the host (model-emitted "observations" are treated as ordinary text and never executed or trusted); smuggling probes in the permanent test suite | Host track (parser), model track (negative examples) |
| R-07 | Registry spoofing and unknown tools: the model calls a tool not in the registry, or a registry entry is tampered with so a familiar name resolves to unexpected behavior | Low | High | Closed, versioned registry owned by the orchestrator; the host allowlists tool names and rejects anything unlisted; registry served by the host, never supplied by the model; registry version pinned per deployment and included in the audit trail | Host track |
| R-08 | Unbounded tool-call loops: the model chains tool calls indefinitely (call, observe, call again) burning latency and compute | Medium | Medium | Hard bounds per request: maximum call count and total wall-clock budget enforced by the orchestrator; loop detection on repeated identical calls; on bound hit, the host returns the best available direct answer or CLARIFY | Host track |
| R-09 | Time-zone confusion: relative windows ("last 7 days", "this quarter") resolve to the wrong absolute range because the zone is ambiguous or shifted | Medium | Medium | `clock.now` returns an explicit offset and IANA zone, never a bare wall time; temporal eval runs under fixed-clock fixtures across zones (baseline Tier 3); canonical storage in UTC with zone applied at the boundary | Model track (temporal training), host track (clock contract) |
| R-10 | Corpus train/eval contamination: baseline probe templates, held-out temporal/tool templates, or telemetry-family phrasing leak into training, inflating metrics and hiding real gaps | Medium | High | Held-out templates quarantined before the baseline is final; template-level dedup of every new corpus slice against all eval sets; seeded, recorded splits; a contamination audit runs before any training job starts and its report ships with the model card | Model track |
| R-11 | v12 capability regression from rebalancing: adding the tool-use slice shifts the corpus mix and degrades the 40/40 held-out SQL performance that v12 currently holds | Medium | High | The 40/40 telemetry suite is a blocking regression gate on every training experiment; rebalancing is done incrementally with the suite re-run at each step; the SQL slice stays dominant and the tool-use slice is sized by the baseline decision rule, not fixed up front | Model track |
| R-12 | Context-budget exhaustion: large tool observations (wide `sql.execute` results, verbose `EXPLAIN`) crowd out the prompt or truncate generation inside the 8192-token window | Medium | Medium | Declared budget split per request (prompt vs observations vs generation); observation truncation policy with row caps applied before injection; oversized observations summarized or paged rather than dropped silently | Host track |
| R-13 | Stale schema snapshot: `schema.inspect` returns a cached catalog that has drifted from the live database, so the model writes SQL against columns that no longer exist | Low | Medium | Catalog-backed inspect with a freshness timestamp on every response; drift check between the gate's schema catalog and the live endpoint on a schedule; the gate's L2 BIND stays the backstop and rejects hallucinated references even when the snapshot lied | Host track |

## Residual-risk notes (high-impact items)

- **R-03 prompt injection via observations.** Residual risk remains even with
  origin marking, because a sufficiently capable future model may follow
  injected instructions phrased as data rather than as commands. The durable
  defense is architectural, not prompt-deep: permissions and identity live
  host-side and are unreachable from model input. Accept the residual on the
  condition that no host decision (permissions, registry, identity) is ever
  influenced by observation content, and re-probe with adversarial
  observations on every model change.
- **R-04 SQL execution hazards.** Read-only construction plus a read-only
  database role makes accidental writes structurally impossible through the
  v1 path, which is the strong claim. The residual is denial-of-wallet style
  harm: expensive but permitted SELECTs. Mitigate with per-request cost caps
  alongside row caps, and keep the human-approval requirement for any future
  write-shaped tool as a hard gate, not a roadmap item.
- **R-05 post-execution fabrication.** Mechanical cross-checking catches
  invented rows and mismatched values, but it cannot catch selective
  emphasis: a summary that is technically inside the observation yet
  misleadingly framed. Residual accepted with two compensations: grounded
  summaries are always delivered alongside the raw observation in user-facing
  surfaces where feasible, and the fidelity probes stay in the permanent
  regression suite.
- **R-06 envelope smuggling.** Delimiter scanning plus host-only observation
  construction closes the known smuggling shapes. The residual is novel
  marker shapes the scanner does not recognize (unicode lookalikes,
  split-across-chunks markers). Mitigate by normalizing model output before
  scanning and by treating any unrecognized marker-like text inside a payload
  as cause for rejection rather than passthrough.
- **R-07 registry spoofing.** A single orchestrator-owned registry removes
  version skew across nodes, which is the main spoofing vector in a p2p
  topology. Residual: compromise of the orchestrator itself. That is an
  infrastructure-security risk, not a model risk; note it here so it is not
  lost, and handle it in the AiAssist Secure security review, not the model
  track.
- **R-10 train/eval contamination.** Dedup and quarantine catch known
  templates. The residual is paraphrase-level leakage: training phrasing so
  close to a held-out probe that the model effectively memorizes the answer
  shape. Mitigate with embedding-similarity screening of new corpus slices
  against eval sets in addition to template dedup, and treat any post-hoc
  contamination finding as cause to re-run the affected evals.
- **R-11 v12 capability regression.** The 40/40 gate catches regressions that
  the suite covers. The residual is capability the suite does not cover: the
  known 2026-10-13 out-of-schema refusal gap is the live example of a v12
  weakness outside the 40/40. Mitigate by running the refusal/clarification
  probes (baseline Tier 4) alongside the 40/40 on every experiment, so
  rebalancing is measured against both the suite and the known gap.
