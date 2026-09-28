# ASTRA contributor guidance

Status: canonical MVP target contract; implementation acceptance remains open.

## Purpose and authority

This file guides people and agents modifying ASTRA. The canonical documents are understandable without private notes, chat history, source receipts, or environment-specific context. They describe the target product and its accepted constraints; they do not prove that code has passed acceptance. Consult [TODO](TODO.md) for known gaps and open choices.

Keep this set internally consistent. Use links to its own documents where a reader needs detail. Do not add private identifiers, source citations, historical extraction material, personal paths, host-specific facts, credentials, or operational secrets. Preserve frozen historical archives rather than rewriting them to match the current target documents.

Ask the owner only when a genuine unresolved product decision would change the contract. Routine wording, structure, consistency, and narrowly scoped document edits do not need a permission checkpoint. Do not turn an assistant suggestion, a rejected idea, a roadmap entry, a historical report, or a test result into an owner decision.

## Product boundaries contributors must preserve

ASTRA is a single independent, host-local Hermes logger. It has no cross-host control plane, shared runtime state, cluster, federation, primary/secondary topology, or redundancy architecture. Portability is a design target, not a universal platform-test claim.

The normal path has one RCA lane. A second lane is optional and may be enabled or disabled by a user for comparison or another explicit purpose. When it is used, every lane receives the same frozen evidence and finding input. Each lane keeps separate leasing, completion, history, reporting, archive, publication, and delivery state. Never change the input to make lanes differ, and never share their write targets. Model differences are supplied by configuration, not code policy. The configured runtime roles target free steady-state triage and capable, low-cost RCA. Approved initial tuning during the first few weeks may compare different models, including inexpensive paid models, to refine tagging/filtering and the workflow toward reliable free-model operation; it is not a permanent paid-triage default or silent fallback. Do not assign free/lightweight triage models to RCA or silently substitute a fallback. These runtime constraints are separate from the contributor subagent routing rules below.

Evidence is retained in its original form until an approved retention policy removes it. Preserve the distinction between immutable diagnosis and append-only repair progress. Repeated known, unchanged problems remain accounted for but do not automatically receive another paid RCA. A material change or new actionable evidence may justify reopening an investigation; the applicable policy must say why.

Keep triage, RCA, repair, publication, and notification distinct. Triage decides bounded escalation. RCA investigates named evidence and can propose a repair. Repair needs separate authorization. Publication requires verified availability at its configured destination. Notification requires a successful send through the configured route and must be deduplicated. A local archive alone proves neither. A native downloadable HTML attachment can deliver a report without a separate publication service. HTML is required for human reports; an internal Markdown archive or a bare local path is not the delivered report.

## Scope and change control

Reuse existing code and patterns. Deliver the smallest practical end-to-end behavior before proposing an addition. For each proposed component or change, explain its necessary job, whether an existing mechanism can do it, and what would fail if it were removed. Prefer the simpler implementation that meets the full contract. Judge the result by whether it removes operator work; routine supervision, repeated permission troubleshooting and uncontrolled inference spend are failures of the intended experience. Do not add a feature, database/table, service, CLI, framework, abstraction, automation, hardening layer, or repair authority without explicit owner approval. If an approved task cannot be completed without such an addition, identify the concrete blocker and simplest alternatives.

Authorization for documentation, offline analysis or local implementation does not implicitly authorize live deployment, repair, schedule enablement, paid runs, provider changes, service restart or live-store mutation. Obtain the applicable authority before crossing those boundaries. Existing explicit authorization remains valid within its stated scope; an approved recurring operating policy does not require a new approval for every routine run. Before a live change, check current state; dated artifacts are not present-state proof.

Back up a file before overwriting it in a repository or deployment target. Do not prune backups. If the repository and a running copy differ and their order is uncertain, stop and compare them rather than overwriting either. Do not prune frozen archives or evidence as a side effect of documentation or implementation work.

## Implementation invariants

- All persistent review-store writes go through `ReviewStore` methods. Callers do not issue raw SQL.
- Initial severity/classification rules and model/provider selection belong to configuration. Triage must judge the actual supplied evidence, identify incorrect script tags and unanticipated scenarios, and explain its disposition; script labels are not unquestionable truth. Preserve the original tags and provenance. Engine code must not pin, choose, or silently improve a model, provider, or fallback route.
- The phase mechanism replaces a selected configuration block as raw text. A missing block stays byte-identical, restoration is byte-exact, and malformed input is refused before a write. Do not substitute YAML reserialization.
- Notice delivery is keyed to unique notice state. Avoid per-tick repeats and preserve concise reporting for known recurrence.
- Keep a report's published location distinct from its archive location. Do not claim delivery from analysis or archive creation alone.
- Preserve original evidence and an auditable relation from findings to their diagnosis and subsequent repair progress.

## Working method and verification

Make small, reviewable changes. State the intended behavior and acceptance evidence before changing a complex or risky area. For cleanup or refactoring, first identify behavior to preserve and add meaningful regression coverage when it is missing.

Use focused tests while iterating, then run the full repository test suite before a separately authorized deployment. Test success demonstrates the tested behavior only; it is not live acceptance. Validate schemas, stable CLI behavior, configuration handling, lane isolation, and state transitions when a change can affect them. Do not invent installation, deployment, or operational commands in public documentation unless an accepted interface specifies them.

When delegating, provide the purpose, exact path and responsibility scope, authorized inputs, exclusions, acceptance evidence, resource limit, expected output, and stop condition. Keep secrets and entire conversation histories out of worker prompts. A worker must report a conflict or missing authority instead of silently expanding scope.

## Required subagent routing and resource discipline

These rules govern contributors working on the repository, not ASTRA's configured runtime triage/RCA models. Follow explicit owner model choices. Otherwise use the least costly available model that can meet the task's quality and verification requirements; do not inherit the leader's model or reasoning level without evaluating the subtask.

Do small, straightforward work directly. Delegate only an independent, bounded subtask when it improves quality, speed or correctness. Choose both the role and the model deliberately:

| Subtask | Role or responsibility | Codex native starting model | Hermes candidates, adjusted to quota | Reasoning | External app candidates (not Hermes) |
|---|---|---|---|---|---|
| File/symbol lookup, inventories, exact comparisons, extraction and factual summaries | `explore` / `explorer`; scoped writer for summaries | Luna (`gpt-6-luna`) | Prefer `glm-5.3-flash`, `gpt-6-luna`, `deepseek-flash`; also `gemini-3.8-flash` | low; medium only when synthesis is needed | `haiku-4.5` |
| Document edits with clear requirements, routine implementation and focused tests | `writer`, `executor` / `worker`, `test-engineer` | Terra (`gpt-5.6-terra`) | Prefer `glm-5.3`, `gpt-5.6-terra`, `grok-4.7`, `deepseek`; also `gemini-3.8-flash` at medium/high | medium | `sonnet-4.6`, `sonnet-5` |
| Semantic reconciliation, code-contract checks, nontrivial implementation, diagnosis and substantive review | `writer`, `executor`, `debugger`, `code-reviewer` as appropriate | Sol (`gpt-6-sol`) | Prefer `gpt-6-sol`, `grok-4.7`, `glm-5.3`; also `gemini-3.8-flash` high | medium; high only for a stated difficulty | `sonnet-5`, `opus-5` |
| Official external docs for an already chosen technology | `researcher` | Luna for a narrow lookup; Terra or Sol for substantive synthesis | Narrow lookup: `gpt-6-luna`, `glm-5.3-flash`; substantive synthesis: `gpt-5.6-terra`, `gpt-6-sol`, `glm-5.3` | low to medium | `sonnet-4.6` |
| Comparing dependencies or designing boundaries with real tradeoffs | `dependency-expert`, `architect` or `critic` | Sol (`gpt-6-sol`) | Prefer `gpt-6-sol`, `grok-4.7`; also `glm-5.3`, `gemini-3.8-flash` high | medium initially; high for unresolved complexity | `opus-5` |
| A difficult, consequential question that the ordinary route cannot resolve reliably | Precisely scoped relevant specialist | Astra (`gpt-6-astra`) only with a concrete justification or explicit owner request | Escalate only the unresolved question using an available suitable candidate | State why the ordinary route is insufficient | Apply the same escalation rule |

Hermes and external-app columns retain the owner's routing preferences; they are not a verified provider catalog or a guarantee of model availability. Match the execution environment, current quota and callable model options. Preserve the task's quality and verification needs when adapting the route. These contributor preferences do not select ASTRA's runtime triage/RCA models.

The listed model identifiers are repository routing preferences, not a guarantee of availability. Check the current tool's model and reasoning options before dispatch. If a preferred model is unavailable, use an available model in the same or lower cost class that can satisfy the task; do not silently substitute Astra. State any necessary upward substitution and its reason.

A role name does not guarantee a particular model. Check whether the specialist preset fixes its model or reasoning level. If it prevents the intended routing, use a configurable worker/default agent with the same bounded specialist responsibility. When an explicit model override is needed, use a fresh or small-context worker with a self-contained brief; do not fork the entire conversation just to inherit an expensive leader configuration. Never claim a routing choice that the tool did not actually apply.

Before dispatch, state the subtask, chosen role/model/reasoning and a short reason. Use low or medium reasoning for routine work. Do not use high, extra-high, maximum or ultra reasoning as a blanket default. In particular, ordinary summaries, folder maps, text diffs and mechanical documentation edits do not justify Astra or an extreme reasoning level. Escalate only the unresolved part after identifying the concrete difficulty or observed quality gap; cost control must not become a reason to accept incorrect or incomplete work.

Keep each worker's inputs, output and stop condition narrow. Reuse a suitable existing worker for a related follow-up. Do not have several workers repeat the same broad audit, or reread the entire history when a scoped diff answers the question. Use an additional independent review only when it addresses a specific material uncertainty or the owner requests it. Use no more than six concurrent child agents; workers do not recursively delegate without a specific assignment. The leader integrates results and owns final verification.

## Implementation locations

| Path | Responsibility |
|---|---|
| `src/agent/astra/` | Deterministic intake/enrichment/grouping, review store and CLI, coverage, runtime and dispatch logic |
| `src/agent/astra_pipeline.py`, `astra_review.py`, `astra_dispatch.py` | Entry points and deployment glue; preserve interfaces called by installed units/wrappers |
| `src/deploy/cron-wrappers/` | Role prepare scripts and phase model engine |
| `src/deploy/systemd/` | Linux service adapters; unit/environment wiring is an installation contract |
| `src/config/`, `src/models/` | Severity/routing configuration and model playbook examples |
| `src/skills/` (withheld from initial baseline) | Proposed triage/RCA instructions and optional delivery adapters; include them only when ready and align them with packets, helper interfaces and this contract |
| `src/schemas/` (withheld pending validation) | Local reference contracts; no runtime dependency or JSON Schema enforcement is established by their presence |
| `tests/` | Behavioral regression and acceptance coverage |

Use the existing implementation as the starting point. These locations describe the current repository responsibilities. Public examples must agree with accepted interfaces before they are used as operating instructions.

Worker skill instructions are an execution interface, not a separate authority for product decisions. Align repository instructions and deliberately installed copies for the affected instance when changing a worker contract. Historical deployment comparisons do not prove current alignment; an unrelated installed skill is not automatically a core dependency.

## Questions that need owner decisions

Escalate a product question when it would change the user-visible contract. Use [Decisions](docs/DECISIONS.md) for confirmed MVP policy and deferred choices; do not reopen settled one/two-lane support, required minimal tracking, or the absence of automatic repair simply because an implementation has a gap. Record new owner answers in the applicable contract once given.

## Folder guidance and private material

Read the nearest folder AGENTS.md before changing its contents; the root product constraints still apply. Ignore directories named archive, archives, backup or backups, in any capitalization and at any depth, unless the owner explicitly includes them. Do not load private histories, runtime data, or backups as routine context. Use the smallest evidence slice needed for the task.

Keep private operational snapshots, cleanup evidence and superseded material under `internal_files/`; they are not public specifications. Preserve the owner's Git index and unrelated working-tree changes. Ignore rules do not remove files that Git already tracks; inspect the selected publication contents before any public release.
