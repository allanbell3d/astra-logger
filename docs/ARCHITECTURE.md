# ASTRA architecture

Status: canonical MVP target contract; implementation acceptance remains open.

This document defines the target component boundaries. It is not a statement that every contract is implemented. Implementation gaps and unresolved choices are in [TODO](../TODO.md); exact behavior is in [Specifications](SPECIFICATIONS.md). The [implementation reference](IMPLEMENTATION-REFERENCE.md) records verified local module, schema and helper declarations; it does not certify a deployed instance.

## Purpose and unit of operation

ASTRA turns local runtime and system health evidence into a small, accountable amount of useful investigation. It retains what happened, recognizes recurrence, asks for diagnosis when justified, and keeps unresolved work visible. Its value is fewer unnecessary investigations and clearer next actions, without losing evidence or hiding new failures.

One computer runs one independent ASTRA instance against its configured Hermes profile and local sources. Each instance owns its capture state, work directory, review store, native jobs, model configuration, and delivery configuration. Another computer running ASTRA is another independent instance. There is no coordinator, quorum, satellite role, distributed lock, or requirement that another computer be online. Sharing report files does not transfer control of a host's jobs or evidence.

The core is deterministic Python processing with filesystem and SQLite state. OS-specific collection and scheduling belong in adapters. Portability is a design requirement; supported operating systems must be demonstrated through adapter acceptance. A Linux deployment layout is not a portable installation contract.

## Data flow

```text
Local runtime logs and supported system sources
                 |
        capture + durable cursor
                 |
     lossless enrichment of captured records
                 |
      +----------+-----------+
      |                      |
enriched evidence      derived groups/feed
      |                      |
      +------ ReviewStore ---+
                 |
    known/inspected work and eligibility
                 |
       bounded triage decisions
                 |
        eligible RCA work item
                 |
      +----------+----------+
      |                     |
    lane A          optional lane B
      |             same input snapshot
      +----------+----------+
                 |
  separate reports and investigation outcomes
                 |
    retained work / repair / delivery state
                 |
   useful notices and bounded daily reporting
```

Collection proceeds independently of inference. A provider outage, unavailable model, paused review job, or failed notification must not stop capture. Conversely, a capture tick does not justify an inference request. Eligibility and native job control decide whether review should run.

The MVP passes through triage before RCA. A direct urgent bypass is deferred in [Decisions](DECISIONS.md); the drawing grants no alternative launch authority.

## Responsibilities and reasons for the boundaries

| Part | Responsibility | Why it exists |
|---|---|---|
| Source adapters and cursors | Read bounded new source records with source identity and positions; recover interrupted intake | Avoid rescanning everything, skipping partial records, or confusing rotation with new evidence |
| Enrichment and classification | Preserve captured fields and add deterministic semantic labels | Make evidence useful without paying a model to sort raw logs |
| Severity policy | Apply operator-configured importance independently of event classification | Change operating policy without changing the meaning of an event |
| Group and compressed projections | Count similar symptoms and provide compact views with evidence pointers | Reduce review input while keeping the original evidence available |
| `ReviewStore` | Own acknowledgement, leased membership, validated decisions, report history, coverage, and necessary work state | Prevent forgotten work and repeated investigation; give callers one mutation boundary |
| Native prepare/reconcile/dispatch adapters | Reconcile prior work, prepare bounded packets, and arm eligible existing jobs | Reuse the native control surface rather than create a scheduler |
| Triage | Decide what a leased finding needs using its supplied packet | Keep the common review path short and inexpensive |
| RCA | Investigate justified work, distinguish facts from hypotheses, and propose a verifiable repair | Spend deeper reasoning on actual unresolved problems |
| Report and notice handling | Retain diagnosis, expose retrievable output, and account for delivery separately | Recover a failed send without buying another investigation |
| Repair progress | Preserve necessary assignment, attempt, verification, and unresolved status | A completed diagnosis must not make unfinished repair disappear |

These are responsibilities, not a mandate for a service, process, table, framework, or abstraction per row. Existing modules and store methods should satisfy them. New physical structures need a concrete reason that an existing mechanism cannot meet the contract.

## Storage and ownership

Three configured roots have different owners:

| Root | Contents and authority |
|---|---|
| Hermes profile, selected by `HERMES_HOME` | Native profile configuration, environment, model playbook, job manifest and native job identities |
| ASTRA work root | Capture/review state, enriched evidence, derived groups/feed, result templates, local RCA archives, and notice ledger |
| Optional publication destination | Recoverable copies of reports for an operator; it never owns investigation truth or controls another instance |

The manifest connects native job IDs, host identity, work root, and configured delivery. Credentials belong in the profile's protected environment, not in evidence packets or public configuration examples. Host names, user names, and filesystem roots are parameters.

The existing local review store is the home for persistent review facts. Callers mutate it through `ReviewStore`; they do not introduce a competing database or ad hoc SQL writer. A compact file ledger remains appropriate where already used for notice deduplication. Necessary logical facts are specified independently of their final physical columns or tables; documentation of a fact is not approval for speculative schema expansion.

Enriched records are the evidence base. Groups and the compressed stream are rebuildable projections, not the sole acknowledgement ledger. Report copies are also projections. An archive or a successful process exit cannot stand in for an accepted finding decision, a completed investigation, a delivered notice, or a verified repair.

## Identity and isolation

ASTRA distinguishes a source record, an evidence record, a grouped finding, a leased batch, an investigation, a lane result, a report, a delivery attempt, and a repair attempt. Reusing one identifier for these different facts creates false completion or repeated work.

Grouping retains host/profile and diagnostic resource context. Separately, a bounded RCA batch may contain unrelated eligible errors to investigate in one run; batching is not causal grouping, and each member keeps its own outcome. Similar wording is not proof of a shared cause. Investigation coverage may connect different finding IDs when a persisted diagnosis actually establishes the relationship. That relationship must retain its scope, evidence basis, outcome, and reopening conditions.

Ordinary operation needs one RCA result. An operator may enable lane B to compare a different configured model on the same work. A comparison has one frozen input snapshot and independent lane outputs. A's completion must not remove B's assigned comparison work; B's absence must not invalidate A's diagnosis. Each lane owns its leases, completion, errors, and archives. No automatic winner selection, consensus, or report-merging service is required.

Shared profile configuration is a resource that requires careful coordination. Staggering jobs is not proof that each session loaded its intended model. Per-lane effective routing must be demonstrated before claiming comparison support.

## Investigation, repair, and delivery

The original RCA records what was known at investigation time. It remains immutable. Later repair attempts and verification results are attributed additions, not edits that make the original diagnosis appear more certain. Reports for people use self-contained HTML, delivered by an accessible link or downloadable file. Optional publication affects where a report can be accessed; it does not make HTML generation optional. See [Specifications](SPECIFICATIONS.md#62-required-report-content) for the report contract.

An investigation may conclude with a diagnosis or with insufficient evidence. Either is an explicit outcome. Neither implies that a fix was applied. A repair is fixed only when its declared verification checks pass; owner confirmation is a separate fact. Different lane conclusions remain visible even when they concern the same repair work.

A persisted report survives publication or notification failure. Recovery reuses that report and its identity; it does not reset the finding to obtain another paid RCA. Authoritative storage failure is a real failure, not successful degraded delivery. A send is successful only when the actual channel outcome is known.

## Simplicity and change

Keep collection, existing native scheduling, configuration ownership, and useful working components. Before adding anything, state the missing behavior, why existing code cannot provide it, and the smallest change that can. Do not remove essential coverage or repair tracking merely to reduce module or table count.

ASTRA diagnoses and records progress. It does not acquire permission to restart services, change models, repair a host, migrate a live store, or resume schedules by producing a report. Those actions have separate authorization boundaries described in [Operations](OPERATIONS.md).

## Runtime adapters and configuration ownership

The implementation separates a deterministic library from installed entry points. The library can enrich and group caller-owned JSONL without opening the review database or making model/network calls. The capture entry point discovers sources, holds its local single-writer lock and invokes that library. The runtime pre-script synchronizes retained evidence, reconciles review work and emits a JSON packet for a native Hermes job. Dispatch reconciles results and rearms existing native one-shot RCA cards; it does not implement a replacement scheduler. An ordinary interval card cannot be treated as a paused one-shot card: native triggering can enable it.

The source tree assigns these responsibilities:

| Location | Responsibility |
|---|---|
| `src/agent/astra/raw_sources.py`, `raw_pipeline.py`, cursor/tail/journal modules | Source discovery, bounded incremental capture and provenance |
| `src/agent/astra/pipeline.py`, `enrich.py`, `classify.py`, `system_rules.py` | Deterministic processing and semantic enrichment |
| `src/agent/astra/fingerprint.py`, `group.py`, `compressed.py`, `storage.py` | Identity, rebuildable projections and active evidence lifecycle |
| `src/agent/astra/review.py`, `coverage.py`, `review_cli.py` | Store ownership, leased work, investigation accounting and deterministic helper |
| `src/agent/astra/runtime.py`, `dispatch.py` | Native job packet, reconciliation and dispatch adapters |
| `src/agent/astra_pipeline.py`, `astra_review.py`, `astra_dispatch.py` | Installed entry points and environment wiring |
| `src/deploy/cron-wrappers/` | Prepare wrappers and phase configuration engine |
| `src/deploy/systemd/` | Linux collection/dispatch service adapters |
| `src/config/`, `src/models/` | Policy and owner-routing examples; JSON Schema reference files are withheld from the initial baseline pending validation |
| `src/skills/` | Worker input/output instructions and separately configured optional delivery adapters |
| `src/report-template/`, `src/agent/astra/rca_report.py` | Required human-readable HTML presentation and attributed updates |

These locations describe local source responsibilities, not a promise of an installed package layout. Existing Linux wrappers contain home-relative Hermes and work-directory assumptions and may start a user dispatch service. They must be adapted and accepted before Windows parity is claimed. The deterministic core uses Python standard-library processing; the existing phase adapter additionally requires PyYAML and POSIX locking. Do not mislabel those adapter dependencies as a universally portable stdlib-only deployment.

The profile manifest supplies `enabled`, `root`, `host`, `jobs`, `deliver` and `shared`, with optional helper and explicitly scoped commissioning inputs. A disabled instance returns a no-wake packet and must not arm review jobs. There is no established `ASTRA_DATA_DIR` environment interface. The installed wrapper's capture root and the manifest review root must agree; differing historical defaults are not an alternative authority. The publication root is declared once for the instance, kept separate from local archives and from unrelated components' folders.

The owner controls the model playbook, native job pins and fallback chains. Triage targets free models in steady state. Initial tuning during the first few weeks may compare different models, including inexpensive paid models, to refine tagging/filtering and reach that free-model target. RCA uses capable low-cost reasoning models rather than free/lightweight diagnosis. Provider examples do not become engine defaults. A quota error, permission failure or desired model name in a file does not authorize an automatic provider substitution. Verify effective routing in the actual session, including card pins and configuration-load timing.

## Configuration phase boundary

The existing phase engine supports the owner-authored `default`, `triage` and `rca` sections of `models.yaml`. It replaces each present column-zero configuration block wholesale using raw text. An absent block stays byte-identical. This preserves comments, quoting, line endings and multiline scalars without YAML reserialization. Restoration copies the exact pre-apply snapshot; it does not apply the playbook's default section. The detailed refusal, backup and load-timing rules are in [Specifications](SPECIFICATIONS.md#9-phase-configuration-contract).

Both comparison lanes use the RCA phase while their native pins may distinguish configured models. Shared profile configuration means overlapping apply/restore operations and model-load timing require coordination. The target is demonstrated session routing, not a claim of strict configuration isolation based on staggering alone.

## Evidence and output writers

Intake writes captured/enriched records and a committed cursor; grouping and compression derive their views from that evidence. `ReviewStore.sync_sources` scans changed `enriched-*.jsonl` buckets. Triage completion writes disposition, reason and review time in SQLite; it does not publish an ordinary triage report. RCA completion retains Markdown diagnosis and history; an HTML projection is the report delivered to a person. A native result template or cron transcript is an intermediate artifact, not proof of validated completion or successful delivery.

The runtime supplies result/archive/publication paths for the assigned lane. Dispatch owns the local notice ledger and configured script-notice route. Native job delivery owns its configured report-message route. A deployment may configure distinct routes for these purposes; do not infer either from the other. No core remote HTTP report server is established by these writers. An optional accessible publication destination or native HTML attachment satisfies delivery when its actual outcome is verified.

Keep original diagnoses and reports. Automated RCA archive cleanup is deferred; it is not permission to prune them. Compression and retained access to enriched logs beyond the 72-hour active window remain a tracked implementation/acceptance item even though the local storage module contains archival behavior. Source presence alone cannot establish accepted end-to-end retention.

The operator-facing objective is a useful phone-readable diagnosis and a simple authorized next action. Perfect prevention and guaranteed immediate root cause are impossible; prompt detection, preserved evidence, explicit uncertainty and a discriminating next check are required. Bounded leases, packet limits and spend controls operate outside the model's discretion. Reuse native jobs and existing storage; an additional observability platform, inference grouping stage or hardware requirement is not part of this architecture.
