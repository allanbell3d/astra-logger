# ASTRA

Status: canonical MVP target contract; implementation acceptance remains open.

ASTRA is a host-local health logger for Hermes. It keeps the useful evidence from routine system health activity, recognizes recurring or watched problems, distinguishes genuinely new or materially changed problems, and prepares a clear diagnosis without asking the operator to search logs by hand.

The product exists to reduce operator work: a useful diagnosis and a clear next action should replace routine log inspection, repeated supervision and uncertainty about model spend. It should retain enough history to account for a problem, avoid spending effort on an unchanged problem that has already been investigated, and keep the minimum status needed to ensure that necessary investigation, repair work, and jobs do not disappear between reviews. The operator can authorize a repair separately; useful follow-through does not grant an investigator automatic repair authority.

## What a user should receive

When ASTRA sees a meaningful problem, the intended outcome is a concise, actionable report rather than a stream of raw log lines. The report explains what was observed, what is known, why it matters, and what repair should be considered. Repeated known problems are retained and summarized without a new paid diagnosis unless material evidence warrants reopening them.

Investigation, repair, publication, and notification are separate facts. A diagnosis may recommend a repair; it does not perform one. A prepared report is not a delivered report. These distinctions make it possible to report honestly about what has and has not happened.

## Core flow

1. **Retain and normalize evidence.** ASTRA preserves incoming health evidence and groups related occurrences without discarding the original record.
2. **Recognize and account for findings.** Deterministic classification, severity policy, and recurrence tracking identify known, watched, informational, and investigation-worthy conditions.
3. **Triage.** The configured model judges the bounded evidence, notices incorrect script tags and scenarios the rules did not anticipate, and returns an informational, watch, tracked, or RCA disposition with an evidence-based reason for each supplied finding.
4. **Investigate.** One bounded RCA run may investigate several eligible errors, including unrelated ones, using retained evidence and relevant context. Each error keeps its own outcome and proposed next actions; overflow remains pending.
5. **Retain and deliver.** Reports for people are self-contained HTML. Retain the report and deliver a verified accessible HTML link or a downloadable HTML attachment through the configured native route. Publication is optional; the HTML format is required. A deduplicated notice informs the operator when there is something useful to act on.
6. **Track follow-through.** Repair and job progress can be recorded separately from the immutable diagnosis, so later work does not rewrite the evidence or conclusion that prompted it.

The ordinary production shape has one RCA lane. An optional second lane can be enabled for model comparison or another explicit user purpose. If it is enabled, both lanes receive the same frozen input set, while their leases, completion state, reports, archives, delivery paths, and configured models remain independent. The optional lane is not a redundancy mechanism and is not needed for normal operation.

## Target and current implementation

These documents state the intended product contract. They are not proof that every component, platform, integration, or operational workflow has already been implemented and accepted. In particular, portability is a target: ASTRA is designed as a Python-based, host-local component that should run wherever its supported dependencies and Hermes integration are available; this is not a claim of testing on every operating system.

The supported-platform target is native Windows and Linux operation using portable Python and configured paths; platform-specific collection and scheduling stay behind adapters. Steady-state triage targets free models. Initial tuning during the first few weeks may compare different models, including inexpensive paid models, to refine tagging/filtering and reach a reliable workflow achievable with free models. RCA uses capable, low-cost models rather than free/lightweight triage models. Exact models, providers and fallback chains remain operator configuration.

The current repository may contain incomplete, paused, experimental, or historical implementation material. Contributors must use the current acceptance criteria before claiming that a behavior is ready for use or deployment.

The product documents listed here, including this README, form the target contract. The implementation reference separately describes inspected local code and its remaining differences from that target. They replace earlier root guidance and document drafts. MVP policy choices are recorded in Decisions; moving this set into place does not certify implementation. Historical plans, reviews and change records can preserve useful evidence, but do not independently authorize implementation. Any retained public changelog records dated changes; it does not override the target contract or claim an unverified product version.

## Reading map

- [ARCHITECTURE.md](docs/ARCHITECTURE.md) explains components, data flow, boundaries, and topology.
- [REQUIREMENTS.md](docs/REQUIREMENTS.md) defines product behavior and non-functional constraints.
- [SPECIFICATIONS.md](docs/SPECIFICATIONS.md) records precise interface and behavior contracts.
- [IMPLEMENTATION-REFERENCE.md](docs/IMPLEMENTATION-REFERENCE.md) describes the inspected SQLite schema, helper and packet interfaces, renderer, and known differences from the target.
- [Report template contract](src/report-template/README.md) specifies the required HTML report, offline questions and updates to the same report after repair.
- [OPERATIONS.md](docs/OPERATIONS.md) describes safe operation and lifecycle boundaries.
- [ACCEPTANCE.md](docs/ACCEPTANCE.md) defines the evidence required to accept work.
- [DECISIONS.md](docs/DECISIONS.md) records active design decisions and their rationale.
- [DEFERRED-AND-REJECTED.md](docs/DEFERRED-AND-REJECTED.md) keeps non-current ideas from being silently revived.
- [TODO.md](TODO.md) separates implementation gaps, open choices, and later release work; it does not grant execution authority.
- [AGENTS.md](AGENTS.md) gives contributor constraints for this canonical document set and the implementation it describes.

No document in this set grants deployment, repair, schedule enablement, paid execution, or a new feature by implication. Those actions need their own explicit authorization.

## Repository layout

| Location | Contents |
|---|---|
| `README.md`, `AGENTS.md`, `TODO.md` | Product entry point, contributor rules, and current gaps |
| `docs/` | Architecture, requirements, specifications, operations, acceptance and decisions |
| `src/agent/` | Python package and installed entry points |
| `src/config/`, `src/models/` | Severity policy and neutral model example |
| `src/deploy/` | Linux scheduler/service adapters requiring instance configuration |
| `src/report-template/` | Required portable HTML report format, template and example |
| `tests/` | Offline regression suite; Linux is required for POSIX integration coverage |

Proposed worker skills and task execution prompts are intentionally withheld from this initial source baseline while they are prepared. The delivery skill CLI/instruction test file is withheld with that skill. Core delivery-engine tests use the included report template, and core HTML report tests remain included. Standalone legacy alarm/window utilities with no retained caller are also withheld pending confirmation of external use. Optional delivery adapters require their separately configured installed package. JSON Schema reference files are also withheld pending validation; runtime readers currently validate through their own code. Contributor model-routing preferences are consolidated in AGENTS.md.

For a repository test run on Linux, set `PYTHONPATH` to `src/agent` and use `python3 -B -m unittest discover -s tests`. Test environments must be isolated from operator homes and live stores. See [Operations](docs/OPERATIONS.md) before invoking runtime entry points; capture can initiate dispatch. Private operational history and archived material are not part of the public target contract.
