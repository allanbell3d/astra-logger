# ASTRA operations and configuration

Status: canonical MVP target contract; implementation acceptance remains open.

This document defines the operating contract and the checks required before live use. It is not a claim that a particular installation is commissioned. Installation gaps and unresolved mechanisms are in [TODO](../TODO.md). Do not run a capture wrapper as a harmless demonstration: a configured wrapper can start dispatch.

## 1. Configuration ownership

An instance has a configured Hermes profile (`HERMES_HOME`), an ASTRA work root, and an optional publication destination. Paths and host identity are instance configuration. Do not hard-code a developer's home directory, a shared drive, a personal machine name, or a paired-host arrangement into component behavior.

`astra-jobs.json` connects the existing native jobs to the instance. Its relevant fields are:

| Field | Meaning |
|---|---|
| `enabled` | Whether ASTRA review/dispatch may wake or arm model jobs |
| `root` | ASTRA work directory used by the review runtime |
| `host` | Explicit local identity carried into evidence and reports |
| `jobs` | Role-to-native-job mapping; the configured second RCA lane is optional |
| `deliver` | Native delivery destination/configuration |
| `shared` | Publication destination when configured |
| `helper` | Optional configured helper location |
| `commissioning`, `case_file` | Optional bounded commissioning inputs; not permission to run production tests automatically |

Validate paths, role mappings and required configuration before mutation. A disabled manifest makes prepare return `{"wakeAgent":false}` and dispatch refrain from arming jobs. This review gate does not stop the independent capture adapter or implicitly alter its scheduler. Disabling capture is a separate deliberate operation.

The work-root configuration must be applied consistently across capture and review. Existing wrappers with fixed `~/logs-watch` or `~/.hermes` assumptions need alignment before portability can be claimed. There is no documented `ASTRA_DATA_DIR` interface; do not invent one in instructions while code still reads another source.

Severity belongs in the JSON severity policy. The repository source is `src/config/severity_policy.json`; the installed capture launcher expects its copy at `~/.hermes/scripts/astra-severity-policy.json`. A library/CLI call with an explicit policy path loads that JSON and fails on a missing or invalid file. Omitting the path selects the built-in policy, currently an older version; do not assume those two routes classify identically. Preserve the installed filename contract while correcting policy drift. Models, providers, API routes and fallback chains belong in operator-owned profile/job configuration. Profile credentials belong in its protected environment. Public examples use placeholders and must not contain tokens, live channel identifiers, private URLs or personal paths.

## 2. Native jobs and dispatch

Capture is driven by the supported OS adapter independently of inference. The deterministic capture library contains no LLM scheduler. Native Hermes jobs are the visible control surface for triage, RCA and daily review. Reuse these jobs; do not replace them with a new scheduler, daemon or patch to Hermes core.

Dispatch performs bounded reconciliation, emits deduplicated useful notices, and rearms eligible native jobs. Reconciliation checks earlier results before purchasing further work. An empty queue or unchanged already-accounted work must not wake an agent.

RCA cards intentionally use one-shot semantics. A consumed one-shot needs a new eligible arm time. Changing it to an ordinary interval job is not an equivalent repair. A native trigger operation can enable a card; therefore an operator-paused card must be identified and respected rather than silently reenabled each tick.

Read lane-specific eligibility and ownership from the review state. An active lease can explain why no new wake occurs; that alone is not a broken scheduler. Blocked members remain held, while unrelated eligible work can continue. Neither backlog age nor native job success bypasses evidence and completion checks.

Each configured lane has its own job mapping and independent result accounting. The inspected dispatch path recognizes B through the `rca-b` job mapping; this is not yet a complete accepted enable/disable interface. The target requires no new B launches while disabled, an already running B investigation allowed to finish, and no suppression of assigned comparison work by A. Enabling B selects only new work arriving after enablement, without historical catch-up. Verify these behaviors using the simplest existing configuration/time boundary before publishing a control command.

Triage and daily job cadence are operator configuration. Do not infer that RCA runs on a daily timer because there is a daily summary job. RCA is eligibility-driven. No schedule resume, immediate paid run, or backlog drain is implied by a documentation or configuration-file edit.

## 3. Model playbook contract

Triage is mandatory and targets zero-cost operation with free models in steady state. During the first few weeks of initial tuning, compare different models, including inexpensive paid models, to calibrate tagging, filtering and bounded context toward a workflow achievable with free models. Keep the tuning configuration and its bounds explicit; temporary paid comparisons do not become an automatic production fallback. RCA uses capable low-cost reasoning models rather than free or lightweight models. Provider, model and fallback selection still belong to operator configuration. Initial tags come from the deterministic script. Triage must recognize incorrect tags and reason about scenarios the script did not predict, using an evidence-grounded disposition and reason while preserving original tags and provenance; this does not authorize editing the severity policy.

### 3.1 Ownership and section syntax

The phase engine applies an operator-owned playbook to the profile configuration. It does not select models or repair an invalid provider route by substitution. Both RCA lanes currently share the `rca` phase; native job routing and effective session configuration must provide the intended independent model choices.

The playbook uses bare section delimiters at column zero:

```yaml
default:
  # Configuration-style contents for this section.
triage:
  # Configuration-style contents for this section.
rca:
  # Configuration-style contents for this section.
```

The comment-only example shows delimiters, not a usable model configuration. Section headers indented as nested YAML are invalid for this parser. Duplicate sections or duplicate configuration keys must be refused. Structural validation is not proof that a provider supports a model or that its credentials work.

### 3.2 Raw block replacement

Within a selected section, a configuration block consists of a column-zero key and the raw body belonging to it. Indented lines and YAML list entries belonging to that block must stay with it. Applying the phase replaces each supplied configuration block as raw text. It does not deserialize and reserialize the whole YAML document.

| Playbook change | Required result |
|---|---|
| A block is supplied | Replace that entire block with the supplied raw text |
| A block is absent | Preserve the existing block byte-for-byte |
| A child key is omitted inside a supplied replacement block | Remove that child as part of replacing the whole block |
| A phase happens to produce the same bytes | Treat as idempotent; do not claim a semantic-only match is byte equality |
| Invalid structure or unsafe state | Refuse before writing any configuration bytes |

Preserve quoting, wrapped scalars, comments, line endings and file permissions where the transformation does not explicitly replace them. Do not perform a deep key merge. Do not assume an absent block means delete it. Do not introduce YAML serialization to “tidy” the result.

### 3.3 Apply, load and restore

Use the existing locking and atomic-write mechanism. Before an apply, retain a unique backup tied to that apply operation. A pending un-restored apply must not be overwritten by another apply. Restore uses the exact backup recorded for the operation, not whichever backup has the newest modification time.

Restoration is byte-exact undo. Applying the `default` section is not equivalent to restoring the prior configuration. A valid restore must recover the same prior bytes even if another backup or a newer playbook exists. Preserve backups; do not prune them during repair or document cleanup.

The selected phase must be applied before the intended native job loads configuration. Restore after the intended session has loaded its configuration, or after the finite failsafe if it never starts. The implementation must identify which session/load event satisfies that condition. A timer or a staggered dispatch alone does not prove model isolation.

For comparison, verify each lane's effective model, provider and route from its actual session context, including intentional native job pins and any fallbacks. A shared `rca` phase must not cause both lanes to use the same model unintentionally or one lane to load the other's transient configuration. The exact accepted mechanism is an implementation gap; do not add new per-lane phases or profile services without evaluating the existing mechanisms first.

### 3.4 Provider failures

A quota/rate limit is not permission to switch models. A route/model identifier error is not proof of provider outage. Diagnose the configured endpoint family, API mode, model identifier, provider route and actual error. Some endpoint families require qualified model identifiers; verification must use the selected provider's contract before changing a value. Do not hard-code a particular provider/model into the engine or public architecture.

Record a failure clearly and preserve bounded job state. Silent expensive fallback and repeated retry without a bound defeat the purpose of the component.

## 4. Routine operation and recovery

### 4.1 Interpreting state

Use [Specifications](SPECIFICATIONS.md) to distinguish review, diagnosis, repair and delivery. A native card marked successful proves only its reported job outcome until the expected durable result is checked. A valid report saved locally proves persistence, not publication or notification.

The useful routine view is small: capture freshness, eligible/leased/blocked work, retained evidence availability, recent investigation outcomes, pending repairs, and delivery failures. This is a reading of existing state, not a requirement to create another monitoring service.

Daily review summarizes this state and recurrence using existing records. It does not launch RCA, probe the host, mass-recover jobs or mutate statuses. Ordinary unchanged watch patterns belong in one compact end-of-day balance. Immediate notices are reserved for genuinely new failure signatures or critical service routing outages. Send one or two short sentences with an accessible HTML report link or downloadable HTML artifact; never dump raw JSON, debug logs or a long Markdown report into chat. Deduplicate notices through the durable notice ledger and record success only after the send succeeds.

### 4.2 Invalid or blocked batches

Inspect the named batch and its exact expected IDs. Distinguish a malformed result, a missing result, an active lease, an expired lease, a blocked attempt sequence, and unavailable evidence. A correction must stay within the same valid membership and allowed attempt policy.

Use the supported targeted recovery method with an explicit batch and reason when recovery is authorized. Preserve the old attempt record. Do not use bulk SQL, clear all flags, mark all members reviewed, reset every job or release expired evidence into new RCA calls. A helper accepting recovery arguments does not itself establish that recovery is appropriate.

Use lane-aware completion for RCA results. The current review CLI routes batch kinds `rca`, `rca-a` and `rca-b` to `ReviewStore.complete_rca`; triage uses its own completion contract. Preserve this routing and verify that an installed helper has the matching behavior. An RCA result must never be interpreted as triage merely because its batch kind includes a lane suffix.

Unrelated eligible work should continue. If the implementation blocks an entire role because one batch failed, correct that specific eligibility defect rather than discard blocked history.

### 4.3 Capture, store and configuration failure

For a capture interruption, recover the cursor/evidence commit through the journal mechanism and rebuild derived views. Do not skip unreadable bytes to make freshness look good. For a corrupt job store or review store, preserve the original bytes and identify the writer/failed operation before replacement.

Back up a database consistently; copying a live database file without accounting for its active journal/WAL state is not a proven consistent snapshot. Rehearse changes on a copy. Never restore an old whole database over fresh monitoring evidence merely to reverse a failed metadata change.

Do not initialize a substitute empty review database when the intended one is missing or misconfigured. Do not raise timeouts, install another service, change journal mode or redesign the schema as an automatic response to a lock error. First establish the narrow failure and reuse the existing bounded transaction/retry mechanisms.

For interrupted phase application, identify the operation's saved state and restore that exact prior configuration. Do not guess from backup modification times or apply a default phase as an undo.

### 4.4 Publication and notification failure

First check that the accepted diagnosis is persisted. Keep that outcome while recovering any configured publication or native send. Verify the actual artifact and configured destination. An incorrect sending identity or destination permission is a routing problem; a successful local file write cannot prove channel delivery.

Reuse the existing report identity/content and pending state. Do not run RCA again to get another notification. Do not claim a URL works without checking it. Human reports require accessible HTML, available by a checked link or native download. A retained local path can be reported as a local fallback, with delivery failure stated accurately; it does not satisfy delivery to an operator who cannot access it. Missing or corrupt approved report assets must cause an explicit failure; do not silently improvise replacement assets.

Record successful notice state only after success. Preserve the ledger across restarts. Review wording changes for their effect on full-message dedup keys so a cleanup does not unintentionally create repeated notices.

## 5. Repository entrypoints and change boundaries

When running package modules from an uninstalled repository checkout, put the repository's `src/agent` directory on `PYTHONPATH` for that process. This supplies the import path; it is not a package installation. Set it using the current shell's environment syntax rather than assuming a POSIX prefix works on every OS. `astra` is the JSONL enrichment/grouping module; `astra.review_cli` is the review-store interface. The first does not create the compressed feed or review database by itself.

Review `sync` scans `enriched-*.jsonl` directly inside the directory supplied as `--root`, normally the work root's `enriched` subdirectory. A caller-named `enriched.jsonl` does not match that interface. Do not pass arbitrary filenames and infer successful ingestion from an empty result. This filename convention is not an instruction to broaden discovery or rename live evidence.

Do not confuse these library entrypoints with the capture/deployment wrapper that can start dispatch. A valid import path does not establish that an invocation is read-only: review operations can write state.

### Changes and deployment

Routine authorized local editing and focused verification may proceed. New product features or significant physical additions require the owner to understand their need, scope and resource cost before implementation. A diagnosis, brainstorming discussion or old plan does not authorize live repair.

Before overwriting an existing repository or deployment file, create the required backup. Keep historical plans frozen. Keep superseded working guidance outside the active documentation. Existing archive deletion and live deployment require their own scope; local documentation promotion does not authorize either.

For a separately authorized deployment:

1. Confirm the target instance, intended changed files, configuration and paused/running schedule state.
2. Resolve repository-versus-installed differences by inspection, not by assuming the newest filename is authoritative.
3. Run focused acceptance for the change and the full current test suite before deployment. The repository suite command is `python3 -m unittest discover -s tests -v`.
4. Back up destination files and any affected store consistently; copy only the scoped change.
5. Verify transferred bytes and the applicable compile/import checks. Use a bounded real invocation only when its side effects and any model spend are authorized.
6. Mirror proven installed fixes back into the repository deliberately, with the same backup and verification discipline.
7. Update the change record and behavioral documentation. Keep schedule activation a separate explicit decision.

A compile check does not prove runtime behavior. A historical green suite does not prove the current tree. A matching source sample does not prove installed configuration, native model routing or live delivery.

The deterministic core uses the Python standard library; the phase engine has an external YAML-validation dependency. Do not advertise the entire deployed system as dependency-free. Packaging, Python compatibility, OS adapters, license, and public configuration examples require release checks; this document is not a clean-machine install guide until those gaps are closed.

Public release must include the actual code, schemas, tests and canonical guidance needed to maintain the component. Keep credentials, operational databases, private provenance, raw history, backups and generated run artifacts out of the public package. A private-sounding directory name is not an access-control or ignore rule; verify the actual release contents before publishing.

## 6. Diagnostic mechanics

These checks support bounded diagnosis of a named problem. They do not authorize repairs, live model trials, schedule changes or provider changes. Preserve incident-time evidence before using present-state observations. When the cause is unproven, state `Root cause: undetermined`, distinguish hypotheses and identify the next discriminating check.

### 6.1 Establish identity, scope and time

Start with host-local identity, profile, component, event, cause, severity label, finding ID, evidence IDs and relevant timestamps. Keep event time, investigation creation, completion, publication and repair verification separate. For historical reuse at decision time T, a prior diagnosis must have completed before T and its report must be present in the named snapshot. A later report or a present healthy service cannot retroactively establish what was known or healthy at T.

Preserve distinct evidence, retries, findings, lanes, investigations and publication identities even when their prose is identical. Similar text alone cannot prove the same cause. Different resource paths, profiles, error codes or mechanisms remain discriminating facts. One investigation may include unrelated errors, but each member needs its own outcome; do not force one invented cause across the batch. Mere current-store absence cannot prove that historical evidence never existed.

Diagnosis completion, repair execution, passing repair verification and owner confirmation are distinct. A DONE file, final chat line or native `last_status=ok` cannot substitute for durable completion and artifact checks. Count recurrence deterministically from recorded sources; do not spend model calls tallying old logs. Known unchanged issues do not receive repeat paid RCA. A materially changed cause, resource or impact, or recurrence after verified repair, may justify investigation under normal triage and evidence criteria. Frequency alone is not an approved reopening trigger. Coverage and notification deduplication solve different problems: a sent-notice ledger does not prevent repeat analysis.

### 6.2 Triage result validation and bounded recovery

Mandatory triage classifies the supplied compact leased packet; it does not investigate the wider host. Validate every leased finding ID exactly once, including decision type and allowed value. Finding IDs and evidence hashes are different identifiers. Use the lease membership as the batch size; global pending counts are not membership. Reject missing, extra, duplicate and unknown IDs without weakening exact coverage.

A batch-specific result skeleton and capped missing/extra/duplicate feedback support one correction within the existing lease. A syntax validator refusing malformed JSON is a serialization failure, not evidence of security or curator policy refusal. Read-before-write, empty sanitized payloads and unexpected completion with undecided members require inspection of the actual result and atomic store transition, not assumptions about what the worker intended.

Keep retry accounting tied to actual leasing. Old result files or job timestamps must not consume a fresh attempt. A blocked batch remains a visible coverage risk; exclusion from the next packet does not mean its members were reviewed. Authorized targeted recovery preserves original batch membership and attempt history, records a reason and releases only unfinished members whose evidence remains available. Expired evidence must not leak into a fresh paid batch. A helper traceback should become one clear bounded helper failure, rather than a new incident containing its own diagnostic stdout.

### 6.3 Provider and silent-agent diagnosis

Read the exact route that executed. Compressed evidence supplies class, frequency, first/last time, profile, component, event, cause and severity; it may not contain provider, model or base URL. Profile error/agent logs supply provider, endpoint, model, API mode, error type, attempt count and backoff. Compare current configuration modification time and stripped route fields with those logs. A job pin or a room transcript alone does not prove the effective route.

Inspect main, auxiliary, title, approval, delegation and fallback slots separately. A key on disk does not prove that the service inherited it. A model appearing in a provider catalog does not prove that the selected endpoint supports chat, tools or the required payload. Plugin enablement does not establish that files exist in the active profile's plugin directory.

| Observed evidence | Check and limit |
|---|---|
| Uniform immediate HTTP 404 | Inspect endpoint family, base URL and API mode before blaming the key or model; the status alone is not a proven cause. |
| HTTP 400 with a bare model identifier | Check identifier qualification and payload against the configured endpoint contract. Catalog existence is insufficient. |
| HTTP 429 or `RESOURCE_EXHAUSTED` | Distinguish rate, quota, capacity, project, region and request shape; this is not proof of empty billing. |
| Multiple affected profiles within minutes | Shared project/region quota is a hypothesis until quota and actual routes are checked. |
| Generic payment/credit wording with no provider request | Check whether an optional credential was absent; do not infer a billing failure from a local warning template. |
| Attempt `1/1` | No retry budget may explain a turn ending silently. |
| Attempt `1/15` with long backoffs | Delayed completion remains possible; measure before declaring a stall. |
| Mid-turn fallback/restart-limit error | Attribute the failure to the actual slot and transition; neither status nor configuration authorizes a route substitution. |

A running gateway is present health evidence only. The monitoring profile can itself suffer a route failure and be unable to page. A watch-level quota family may explain quiet failure; report that distinction rather than increasing severity or changing routes without authority. Bound new-error log reads and redact keys, authorization headers, bearer values and tokens.

### 6.4 Messaging, permission and classifier distinctions

Discord 401 authentication differs from 403 permission/access failures; 50013 and 50001 require sender-and-destination checks. Restricted destinations can fail by design. An operator permission change or expected transient interruption is recorded context and does not automatically warrant RCA. Verify the configured authorized destination before calling a provider or messaging platform unavailable.

Socket closure, stale health, reconnect codes or a running PID do not independently establish a heartbeat, reconnect or adapter defect. Compare bounded incident-time logs and exact affected profile state. For read-only HTTP diagnostics, a client/edge rejection can differ from the platform's API permission rejection; identify the response layer before interpreting its code.

Do not misclassify a lane's own timed-out search, malformed shell snippet or quoted old helper error as a new originating system failure. Tool stdout quoting an error is not necessarily an actual helper JSON rejection. Preserve genuine errors while checking the source shape and context before suppressing diagnostic echoes. Proposed filters require proof that real failures remain captured.

### 6.5 Scheduler, store and capture diagnosis

Inspect the persisted native job store, including paused cards, rather than assuming a listing includes all cards. Read lease ownership and expiry before diagnosing a missing wake. A consumed one-shot needs a future eligible arm time; a plain resume of a past one-shot can fail. Native triggers can enable paused work, so an operator pause must survive reconciliation.

For a disappearing fire claim, preserve the job-store bytes/snapshots, compare claim token and heartbeat timing, identify possible external writers and keep attribution unproven unless evidence names the writer. If corrupt bytes have already been replaced, missed job membership may be unknowable. A copied job file appearing nearby is a lead, not proof of who wrote or why.

For store locks, establish timeout, transaction length, concurrent writers and journal mode. A successful immediate retry indicates contention, not proven data loss. Use short transactions and existing bounded retry; leave journal-mode changes, shared locks and schema additions as separately evaluated proposals. Distinguish JSON job-store corruption from SQLite corruption even when the finding label says `database_corrupt`.

For freshness, inspect the capture adapter and advancing enriched/compressed output, not only cron status. Cursor identity includes inode/device, offset and size. Rotation and truncation must preserve source identity; identical journal text with different cursors can be different events. A bounded tail must obey its byte limit. Wall-clock-dependent freshness fixtures cannot establish a stable historical window. Recover incomplete append/cursor transactions through the existing journal, preserving unreadable bytes and all original fields.

Inspect a finding's age and evidence availability without multiplying counts through a one-to-many evidence join. Retention expiry explains unavailable evidence but does not establish whether the retention window or slow backlog handling caused the loss. Bulk relabeling old findings is store hygiene rather than a repair. Keep RCA reports; archive-retention automation remains deferred. Main enriched evidence beyond the current window still needs an approved compression/retention policy recorded in TODO.

### 6.6 Performance and proof discipline

Measure the named operation against a retained store snapshot, recording size, finding/evidence counts, before/after wall time, cold/warm conditions and test scope. Separate slow pre-script, history lookup, provider response and report validation from the overall timeout. A timeout extension is not a measured correction.

Optimization must preserve identity, latest-evidence order, lane isolation, decisions, counts and retention. Reuse identity calculations only when evidence ID and full payload are unchanged; refresh mutable disposition and lane flags. Packet reuse belongs to its preparation transaction. A microbenchmark or isolated store replay does not prove bounded live end-to-end behavior.

Offline acceptance uses hash-verified inputs or read-only copies, retains report identity and chronology, and checks no mutation afterward. Pair enumeration, matching hashes, a completed worker and heuristic text similarity cannot substitute for reading report substance and checking each consequential mechanism. Preserve abstention when evidence is incomplete. Replay measurements prove only the tested corpus and predicates; they do not establish paid calls avoided, money saved or production suppression safety.