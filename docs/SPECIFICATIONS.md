# ASTRA behavior specifications

Status: canonical MVP target contract; implementation acceptance remains open.

This is the detailed target contract. [Requirements](REQUIREMENTS.md) provides outcome-level requirements; [Operations](OPERATIONS.md) owns native scheduling, configuration and recovery procedures. [Decisions](DECISIONS.md) records confirmed MVP policy and deferred improvements. Additional product choices and physical additions still require the applicable explicit authority.

## 1. Records, identity, and time

### 1.1 Identity boundaries

| Identity | Meaning | Must not be substituted with |
|---|---|---|
| Source reference | Location of the captured observation in a file or journal | A display sample or group fingerprint |
| Evidence ID | One retained enriched record | A finding ID |
| Finding ID | A group of operationally similar evidence requiring disposition | Proof that every occurrence has the same cause |
| Batch ID | Exact leased membership and role for one review attempt sequence | The entire global backlog |
| Investigation identity | A persisted diagnostic effort and its covered scope | A notification or repair attempt |
| Lane identity | The independent result stream within an RCA run | A separate version of the input evidence |
| Report identity | The retained diagnosis artifact and its original content | Its current filename alone or a send receipt |
| Repair attempt identity | An attributed attempt and verification result | Investigation completion |

The existing review interface uses 20-character finding IDs and 64-character evidence hashes. They are different arguments. Completion templates must carry the supplied finding IDs unchanged; an agent must not derive replacement IDs from evidence hashes or prose.

A file reference includes its source path, device/inode identity where available, and byte range. `byte_start` is inclusive and `byte_end` is exclusive. A captured `source_identity` may include a SHA-256 of the first raw line without CR/LF. For a folded multiline record, that hash verifies the first line, not every byte in the range. Verification must not overstate what was hashed.

A journal reference uses its journal cursor and relevant boot/unit metadata. A placeholder such as `journald:0-0` is not a file byte range and cannot be sought as one. A missing, rotated, truncated, expired, or inaccessible source must be reported as such; fabricated source text is never an acceptable fallback.

### 1.2 Time and scope

Retain event time separately from capture and processing time. Parse explicit timezone information when present. Interpret naive runtime timestamps using the source host's configured local timezone, not the reader's timezone or another host's timezone. Journal realtime timestamps expressed in microseconds must be converted to seconds before normal timestamp processing.

Retain host, profile, source kind, and relevant component identity throughout the flow. Runtime-profile evidence and system evidence remain distinguishable streams; system records must not silently become profile failures. Similar records on different hosts or profiles must not collapse into one finding. A report may explicitly discuss a broader relationship, but that does not confer cross-host scheduling or storage authority.

Counts must name their units: source records, retained evidence rows, findings, review decisions, RCA runs, reports, and notifications are not interchangeable. Recurrence counts and first/last occurrence times describe evidence occurrence, not the number of model calls.

## 2. Capture, enrichment, and retention

### 2.1 Bounded intake

Source adapters discover configured runtime logs, supported rotated logs, and supported system sources. Initial discovery uses a bounded tail; its exact adapter budget must be stated in configuration or adapter documentation. It does not claim to reconstruct all prior history. After initial discovery, cursors identify new complete records and expose backlog rather than silently dropping it.

For file intake, track source identity, offset and observed size. A changed inode/device or a file truncated below the stored offset resets reading to byte zero for that source generation. Overlapping globs must be deduplicated by real path. Output paths, state files and their journal sidecars must not alias an input path.

Only complete parseable records may advance the committed cursor. An incomplete final line or malformed JSON row holds the cursor at the unreadable boundary and produces a bounded diagnostic. Later content cannot be declared consumed merely because it appears after the bad row. Recovery or deliberate source repair must retain evidence of the obstruction.

Source continuation lines, such as traceback frames, can attach to their parent event. If capture caps continuation length, the record must identify that truncation. Lossless enrichment means preserving every field that capture supplied; it is not a false promise that a capped source adapter retained every original source byte.

### 2.2 Commit and crash recovery

The intake writer must serialize concurrent writes using the existing local locking mechanism. A durable journal protects evidence append and cursor commit. Data must be flushed before the corresponding cursor is committed. An interrupted append is recovered to the last committed byte boundary, with the corresponding cursor restored. Derived group/feed output is then rebuilt from committed evidence.

Do not acknowledge records that only reached an in-memory buffer. Do not rebuild state by keeping a newer cursor and discarding the evidence it claims to cover. Recovery must be idempotent, including after a second interruption.

### 2.3 Enrichment and policy

Preserve original capture fields, including original text. Add ASTRA-owned classification, fingerprint, severity and provenance. Normal output uses `astra.classification`, `astra.fingerprint`, `astra.severity` and `astra.provenance`. When an input already owns colliding ASTRA fields, preserve those fields and place pipeline additions under `astra.pipeline` rather than overwriting the input.

Classification describes event type and available cause/domain/component/tool/target/provider/model/resource context. A classifier label is an interpretation with evidence limits, not a verified RCA. Severity is applied separately by the repository policy `src/config/severity_policy.json`, using ordered first-match rules and the configured default. The baseline default is `watch`. Policy labels include `ignore`, `watch`, `needs-attention` and `tracked`; these are not the triage decision enum. Rule outputs may use all four labels; the currently supported default label is one of `ignore`, `watch` or `needs-attention`. Runtime readers currently perform validation in code. The JSON Schema reference files are withheld from the initial baseline pending validation against actual producers/readers; they are not a runtime dependency or proof of enforced input validation.

Do not infer billing failure from generic payment wording or a missing optional key. A `429`/`RESOURCE_EXHAUSTED` response can establish rate/quota limitation without proving empty credit. A `403`/`50013`/missing-access response can establish route or identity permissions without proving a general service outage. Authentication failures remain distinct. These cases still need their exact target, scope and impact: neither all permission errors nor all quota errors are harmless by definition.

Initial event/severity tagging is performed by scripts. The triage LLM must notice wrong tags and apply evidence-grounded judgment to scenarios the scripts did not anticipate; it must not rubber-stamp a rule label. Record the mismatch or unanticipated scenario in the existing disposition/reason while preserving original script tags and provenance. This does not authorize rewriting raw evidence, inventing a new override field or changing configuration. Current static filtering can exclude ignored/tracked observations before triage sees them. Candidate visibility and filtering are recorded repairs to tune during initial implementation/operation, alongside the other work in [TODO](../TODO.md). No one admission strategy is prescribed here. Compare different models, including inexpensive paid models during the first few weeks, to make the bounded workflow achievable with free triage models in steady state. Preserve supplied evidence and distinguish script tags from actual assessment; verify the resulting filter behavior against representative missed, expected and recurring cases.

Quoted errors, successful retries, healthy telemetry, tool output, and deliberate refusal by a validator must be interpreted in their actual context. Text produced while investigating a system is not automatically another failure of that system.

### 2.4 Grouping and compact views

Group fingerprints combine host/profile, normalized text and relevant semantic dimensions. Normalize volatile timestamps, process IDs, and equivalent retry presentation where appropriate. Preserve exact affected-resource paths and relevant provider/model/target differences. A traceback source-code filename is not automatically the affected resource; stripping all paths can nevertheless merge distinct real failures and is prohibited.

Groups retain recurrence counts and current occurrence/evidence pointers over the active window. The compressed stream is chronological and severity-filtered; it can omit `ignore` rows and standalone continuation noise, and clip displayed text. It retains timestamps, useful semantics and source/evidence pointers. It must identify clipped samples using truncation/original-length information rather than imply completeness.

A group means similar symptoms. It does not prove shared root cause, safe suppression, investigation coverage or repair. Policy edits may rebuild groups and compressed views from retained enriched rows without rewriting the original captured fields. The compressed stream is not the sole acknowledgement store.

### 2.5 Evidence layout and lifecycle

| Relative work-root path | Contract |
|---|---|
| `enriched/enriched-YYYYMMDD-00.jsonl` and `enriched-YYYYMMDD-12.jsonl` | Active enriched records in local half-day event-time buckets |
| `archive/enriched/*.jsonl.gz` | Compressed archived buckets displaced from the active window or cap |
| `groups/groups-current.jsonl` | Rebuildable group projection |
| `compressed/compressed-events.jsonl` | Rebuildable compact chronological projection |
| `review.sqlite` | Durable review and necessary work accounting |
| `review-results/` | Exact membership templates and agent result files |
| `rca-a/`, `rca-b/` | Separate local lane archives |

The retained baseline is 12-hour buckets, a 72-hour active evidence window, and a 250 MiB active size cap. These are the local `astra.storage` defaults `BUCKET_HOURS`, `RETENTION_HOURS`, and `MAX_TOTAL_BYTES`; they are not documented manifest configuration knobs. The storage API can receive an explicit size cap, which is distinct from an operator-facing retention setting. Moving a bucket out of active storage is distinct from deleting its archive. Long-term archive pruning needs an explicit retention policy; it must not be presented as infinite retention or invented during a documentation cleanup.

Evidence is logically append-only until the configured retention action: retained observations cannot be silently edited away. This does not require every physical bucket file to remain forever append-only; chronological bucket rebuilding and archival must preserve committed record identity and content. Store ingestion must rescan changed/rebuilt buckets idempotently. A saved append offset or last timestamp alone cannot reliably ingest a rewritten bucket.

Retention does not acknowledge a finding. If evidence needed for unresolved work expires, retain the work identity, disposition, investigation/repair history, missing-evidence status, and reason it cannot proceed. Do not create an RCA from empty or stale evidence simply to clear the backlog.

## 3. Review storage and leased work

### 3.1 Persistent facts

`ReviewStore` is the mutation boundary for review state. Its existing store includes evidence, findings, batches, source scan state, report history and small runtime markers. Necessary investigation coverage and repair progress belong with that existing authority. A requirement to remember a fact is not a mandate for a new table or database; physical changes must be justified and approved before implementation.

Persist exact batch membership, role/lane, input basis, creation/lease times, attempt count, completion or failure, and targeted recovery reason. Persist each finding's validated disposition and the report-backed relationships needed to avoid duplicate diagnosis. Retain report identity and separate publication/notice facts. These facts must survive restart and must remain inspectable without a model call.

Use protected local storage permissions appropriate to the OS; POSIX `0600` is the existing database convention, not a Windows ACL specification. Keep write transactions bounded. A lock or contention failure is an explicit operational failure, not authority to introduce a new database platform or bypass the store API.

### 3.2 Preparation and completion

Preparation reconciles earlier work before selecting eligible members. Only the selected member IDs belong to the new batch; the global queue count is context, never the result cardinality. The batch owns those members during its lease. Selection and ownership must be durable so concurrent ticks cannot purchase the same assigned work twice.

Quiet or unchanged eligible state returns `{"wakeAgent":false}`. A waking packet identifies role, batch, exact findings, compact evidence, relevant prior status and expected result destination. A prepare operation cannot silently create another model task because an earlier result was inconvenient.

Validated completion is atomic with respect to member accounting. Reject missing, extra, duplicate or unknown finding IDs, invalid enums, malformed fields, wrong batch/lane or incompatible completion state. Preserve the original membership and return a bounded mechanical explanation. A helpful template contains all expected IDs with explicitly unfilled decisions; it must not contain plausible defaults that could be accepted accidentally.

Attempt accounting occurs when work is leased, not only after successful processing. The retained baseline is a 600-second lease and two attempts before blocking, currently enforced by values in `ReviewStore.prepare` rather than documented manifest knobs. These bounds concern review ownership; they do not grant a 600-second triage runtime or authorize changing native timeouts. Invalid output can receive one correction within the same live lease, without preparing a new batch or changing membership.

When attempts are exhausted, the batch remains blocked and visible. Its members stay owned until explicit targeted recovery. Unrelated eligible findings may proceed. Recovery names the blocked batch and reason, preserves previous attempts and decisions, and cannot manufacture evidence that has expired. A blocked batch is neither silently abandoned nor permission to freeze an entire lane indefinitely.

## 4. Triage contract

### 4.1 Input and limits

Triage operates on its leased packet only. It must not read extra logs, query the database, fetch evidence/history, browse the host, run RCA, repair anything, edit jobs/configuration, prepare another batch, or send a separate message. If supplied evidence is insufficient, its decision and reason must say what is unresolved rather than invent facts.

The existing interfaces have different budgets; callers must not treat them as one number:

| Boundary | Baseline limit | Owning interface |
|---|---:|---|
| Direct store/CLI prepare default | 12,000 bytes | `ReviewStore.prepare`; CLI `prepare --max-bytes` default |
| Runtime request to store preparation | 24,000 bytes | `runtime.prepare_job` calls the store with this explicit budget |
| Final runtime packet including added context | 32,768 bytes | `runtime.prepare_job` validates its serialized packet |
| Optional owner known-issues reference | 8,192 bytes, within the final packet cap | `runtime.prepare_job` validates the attached text |
| Per-item display sample | 240 characters | `ReviewStore._item` builds the sample and truncation metadata |

Packet limits are encoded byte budgets; character counts cannot substitute for them. All wrappers must preserve the final cap, including result templates and known-issue guidance. A finding must not be acknowledged merely because it did not fit this packet.

An optional `triage-known-issues.md` in the work root can supply bounded explicit owner guidance. It is scoped input, not inherited model memory, a replacement diagnosis ledger, or an automatic exact-match suppression rule. It must not override a genuine material change.

Triage should finish within 60 seconds; acceptance requires completion within 120 seconds for the representative bounded packet. Do not solve a failed bound by quietly increasing timeout, retrieving more context, or escalating to a more expensive model.

### 4.2 Result format

The result is a JSON array containing exactly one object per supplied finding ID:

```json
[
  {
    "id": "<supplied finding ID>",
    "decision": "watch",
    "reason": "Recurring symptom without new impact in the supplied evidence."
  }
]
```

The placeholder above illustrates the schema; it is not a valid batch result. Use the supplied finding ID, not its evidence hash. `id`, `decision` and `reason` are strings. The decision is exactly one of:

| Decision | Meaning and effect |
|---|---|
| `informational` | No investigation/action is warranted by the supplied evidence; retain the record and its decision |
| `watch` | Retain and count the symptom; reassess if its relevant evidence or impact changes |
| `tracked` | Link to known investigation/work that already accounts for the current issue; retain outstanding repair obligations |
| `rca` | Further diagnosis is justified; enter RCA eligibility checks rather than assume a job has already run |

A reason must be specific to the supplied finding, at least 12 nonblank characters and no more than 160 characters. This is the worker-output contract; store validation currently enforces only the minimum, with upper-bound enforcement recorded in [TODO](../TODO.md). Optional `coverage` or `reopen` references may be used only when the supplied contract exposes valid existing references. They are mutually exclusive and must be validated by the store. Do not invent reference IDs or use prose as a substitute for a persisted link.

The native final message is `[SILENT]` unless there is a meaningful escalation or failure that the operator should see. When a message is needed, use at most two lines and 350 characters. Do not publish a triage report, dump finding IDs, enumerate routine backlog, or send independently of the native delivery route.

## 5. Investigation coverage, recurrence, and reopening

### 5.1 A reusable investigation

Coverage answers “has this diagnostic question already been answered for this scope?” It does not answer whether the system is repaired. A reusable result requires:

1. A validated, persisted investigation outcome and retrievable original report.
2. An explicit evidence basis, relevant scope and relationship to the current finding.
3. Completion before the new eligibility decision being evaluated.
4. No material change that invalidates that explanation for the current observation.

Scope includes host/profile, event semantics, relevant resource/target, provider/model or route where diagnostic, severity/impact, and the occurrence context. Equal normalized text alone is insufficient. Exact signatures can support deterministic reuse only when these scope dimensions and the retained diagnostic basis agree. Relating a different finding ID requires explicit report-backed support, not a model's unsupported statement that it “looks the same.”

For chronological replay, use the candidate investigation/batch creation time as decision time `T`; a prior investigation is available only if its completion precedes `T`. Do not let later reports or repairs explain earlier decisions. Verify that the referenced report/evidence was actually accessible at that time; present-day file presence is not historical access proof.

An inconclusive report records completed investigative work and its limits. It cannot be reused as proof of a known root cause, but it must prevent an unchanged automatic replay of the same unproductive checks. Reconsider it only when new useful evidence or a specific authorized follow-up exists.

### 5.2 Decision table

| Observation | Required treatment |
|---|---|
| Same scoped issue, covered by an adequate diagnosis, no material change | Record recurrence and retain work/repair status; do not buy the same RCA again |
| Different finding ID, report explicitly establishes same scoped issue | Reuse the documented relationship; retain the new evidence and count |
| Similar text, different affected resource or diagnostic provider/model/route | Reassess scope; do not reuse merely by text similarity |
| New evidence contradicts the diagnosis or changes impact | Return for a scoped reopening decision with the change identified |
| Same symptom after a repair was verified fixed | Treat as possible recurrence/regression requiring reassessment, not an automatic harmless repeat |
| More occurrences but no other change | Continue counting/reporting without new RCA; frequency-only reopening is deferred beyond the MVP |
| Evidence expired or original diagnostic basis is inaccessible | Preserve the unresolved condition; do not assume covered, invent cause, or launch blind RCA |
| Report saved but publication or notice failed | Recover delivery using the saved report; retain investigation completion |
| A completed during an already requested A/B comparison | Preserve B's assigned input and outstanding result obligation |
| B disabled and no comparison was requested | A's valid result is sufficient; absence of B is not an RCA trigger |

Reopening records the prior investigation, the changed evidence/scope or repair context, and the reason. It creates a new investigation episode without rewriting the original conclusion. Do not reopen for queue age alone. Any future numerical frequency policy must state its window, baseline, threshold and reset behavior before use.

## 6. RCA input, execution, and completion

### 6.1 Eligibility and evidence

Normal eligibility requires an accepted `rca` disposition, retained usable evidence, no adequate unchanged coverage, and no conflicting ownership for the requested lane/work. A native schedule tick is not itself eligibility. The MVP requires validated triage before RCA. A direct urgent bypass is deferred; retain useful detection/priority without granting it direct RCA authority.

Each packet must state its assigned findings, evidence pointers, relevant prior investigation/work status, and output contract. A bounded packet may contain multiple eligible errors, including unrelated ones; do not require evidence of a shared cause merely to put them in one RCA run. Keep each finding's diagnosis and report attribution separate, and leave overflow pending without acknowledgement. Every member needs an explicit accounted outcome before batch completion. A shared report may contain those separate outcomes; completion cannot read only its first ID and mark all members done. Missing member outcomes must refuse completion without consuming the batch.

RCA may retrieve bounded named evidence and perform relevant read-only checks. `ReviewStore.get_evidence`, exposed by the review CLI evidence operation, defaults to 8,192 bytes and accepts explicit requests from 1 to 131,072 bytes. A higher permitted bound is not a reason to request the maximum. State why a check distinguishes hypotheses before expanding scope. A live observation gathered by one comparison lane is an additional observation with its own time and origin, not evidence that the lanes received different initial packets; do not feed one lane's results into the other lane.

RCA must not change models, credentials, jobs, services, installed software, host configuration, or files as a repair. It must not perform broad unrelated log mining. Read-only inspection remains limited by permissions and the incident's scope.

### 6.2 Required report content

Reports delivered to people, including RCA reports, use self-contained HTML. The [report template contract](../src/report-template/README.md) specifies the existing renderer, offline behavior, identity and repair-update interface. Preserve the required report content when rendering it; HTML is the delivery format, not a shortened version of the diagnosis. Existing Markdown source and archive artifacts may remain for validation and internal processing, but they do not replace the delivered HTML.

A separately configured publication service is optional. Provide a verified HTML link accessible to the recipient; when that route is unavailable, provide the HTML as a downloadable file through the configured native delivery route. The downloaded report must remain readable without a server or private-network access. Failure to render HTML remains a report-delivery problem: retain the diagnosis/source and recover its HTML delivery without buying another RCA or silently substituting Markdown. This requirement does not create new reports for silent triage or routine notices.

Every RCA report contains these nine sections, using stable recognizable headings:

| Section | Required substance |
|---|---|
| Observed impact | What failed, who/what was affected, scope and what is not established |
| Timeline and evidence | Time-ordered relevant observations with retrievable evidence references and stated gaps |
| Facts versus hypotheses | Separate observed facts from interpretations and competing explanations |
| Checks and results | What was actually checked, results, and what was not checked |
| Root cause | Supported cause and confidence, or `Root cause: undetermined` with the next discriminating check |
| Proposed fix | Smallest justified correction; no claim that it was executed |
| Risks and prerequisites | Access, dependencies, side effects and conditions required to attempt the fix |
| Rollback | How to recover from the proposed change, or why rollback is not applicable |
| Post-repair verification | Concrete checks that would demonstrate restoration and detect recurrence |

A PID change, a successful ping, or a `socket_closed` message by itself does not establish a reconnect-loop cause. A failed broad inspection command is not proof that its intended target failed. Do not assert checks ran when they were only proposed. When evidence is insufficient, preserve that uncertainty visibly.

The report's artifact references are operational evidence, not references to documentation source history. Public project documentation must stand alone; actual RCA reports still need to identify the evidence supporting their incident-specific conclusions. Sensitive report content is operational data, not public repository material by default.

### 6.3 Persistence and result accounting

Validate report shape, member identity and lane before accepting completion. Persist the original report to the applicable lane archive. Record a per-member diagnostic outcome, its report relationship and coverage basis. A partially accounted batch remains incomplete or explicitly partial; it cannot become wholly complete because the first member succeeded.

Record the result as persisted before reporting notification success. Publication may be optional, pending or failed; those states do not roll back a valid saved diagnosis. The local result must make report paths and pending delivery facts explicit. A scheduler's successful exit is insufficient evidence of this transaction.

Keep a stable identity across idempotent replay of the same completed batch and same original report. Changed original content under an already accepted identity is a conflict to expose, not an overwrite to make the run appear green. Later diagnostic changes belong to a new episode or an explicitly attributed amendment that preserves the original, never silent replacement.

The final RCA notice is at most two lines and 350 characters, with a concise outcome and either a verified accessible HTML report link or the retained HTML report attached for download through a native route that supports attachments. For attachment delivery, verify that the successful send included that exact HTML report. A local archive path may describe a saved artifact, but it does not prove that the operator received it. When delivery fails, say so and retain the pending delivery obligation.

## 7. One lane and optional comparison

In single-lane mode, A can perform the whole investigation flow. While B is disabled, create no new B leases, wake-ups or model runs and do not fabricate completion flags. An investigation already running when B is disabled may continue to its normal bounded completion, including the model calls needed within that existing run; this does not authorize a new launch or retry after it ends. Preserve its result and existing B history.

For an enabled comparison, capture the same findings, evidence snapshot, relevant prior context and task instructions for both lanes before either result changes shared eligibility. Lane-specific batch identity, output paths and model routing may differ. Later arriving evidence belongs to a subsequent decision unless the comparison is deliberately restarted with a new shared snapshot.

Each lane has separate lease/attempt accounting, completion flags, reports and delivery outputs. Shared “busy” or “covered” predicates must not suppress the counterpart's already requested work. A report from A must not enter B's input as a hint. A's adequate diagnosis can serve ordinary work while B's outstanding comparison status remains visible.

Switching B off prevents new launches while allowing an already running investigation to finish and retain its result/history. Do not cancel it mid-task. When enabled, B applies only to new work arriving after enablement. Older waiting, already-started and completed investigations are excluded from automatic B selection. Use the simplest existing configuration/time boundary; do not implement historical catch-up or a backlog-management subsystem for the MVP.

Both configured models must actually load in their respective sessions. Shared playbook configuration, native card pins, and load timing need concrete implementation verification. A file containing the desired model name is insufficient proof. No automatic model substitution, voting, or mandatory dual-result approval is part of this contract.

## 8. Repair and delivery state

### 8.1 Independent state dimensions

These are logical facts, not a required new database schema or a new CLI enum:

| Dimension | Necessary distinctions |
|---|---|
| Review work | Pending, leased/running, validated complete, failed/retryable, blocked, evidence unavailable |
| Investigation | Not investigated, in progress, diagnosed, inconclusive; with coverage/reopening relationships |
| Repair work | Unassigned/pending, assigned, in progress, attempted with result, verified fixed, explicitly deferred or will not fix |
| Owner confirmation | Not supplied versus explicit confirmation linked to the applicable repair/result |
| Report | Not persisted versus persisted original diagnosis |
| Publication | Not configured, pending, available, failed/conflicted |
| Notification | Pending, successfully sent, failed; with deduplication state |

“Diagnosed but unrepaired” is a valid steady state. It retains the repair obligation without forcing repeated RCA. “Inconclusive” is not “fixed.” “Will not fix” is an explicit operator disposition with reason, not an agent's convenient substitute for an unresolved problem. Deferred work retains its reason and outstanding condition.

### 8.2 Repair updates

Each repair attempt records actor, time, intended action, actual action, result, and verification checks/results. An unsuccessful or partial attempt remains visible. A fixed status requires passing the relevant declared checks; owner verification requires explicit confirmation linked to the result. A reply receipt or assigned repairer is not evidence that an attempt occurred.

Keep original RCA text immutable and append progress. Use the existing revision/conflict mechanism when concurrent updates are possible: an update based on an older revision must not overwrite a newer attempt. Preserve conflicting inputs for deliberate resolution. If two lane reports cover the same repair, reflect shared repair progress consistently while retaining their separate original conclusions.

Any authorized repair execution remains a separate task. Status tracking neither provides a repair executor nor grants permission to execute a proposed fix.

### 8.3 Delivery and notices

The authoritative record owns report identity and current accepted progress; external copies are recoverable projections. Persist first, then publish if configured, verify the referenced artifact, and send through the configured native route. Record successful sends only after success. Failed publication, wrong permissions, missing assets, or send failure must remain visible without invalidating the diagnosis.

Recovery reuses the same report identity and retained content. Identical replay is a no-op or completion of pending work. Changed bytes under the same original identity, incompatible revisions, or a missing authoritative store are explicit errors. Do not silently initialize another database, replace approved report assets, or restore stale records to match an older copy.

The existing notice ledger hashes the full message string. Persist successful notice state across ticks and restarts. Unchanged text/state must not be sent every tick. A wording change creates a new dedup key and may cause one new send; changing wording to evade deduplication is not acceptable. Analysis deduplication and notice deduplication solve different problems and both are required.

Daily reporting uses retained deterministic counts and existing work outcomes: new diagnoses, unchanged recurrence worth summarizing, pending repairs, blocked/expired work and unresolved inconclusive investigations. It must not fetch new incident evidence, run probes, create batches, reopen work or change dispositions. Emit a useful compact report through the configured native route; if there is no useful update, remain silent. Scheduling and notification policy belong in [Operations](OPERATIONS.md).

## 9. Phase configuration contract

This contract governs the existing phase adapter, not model selection. Its local declaration is in [the phase engine](../src/deploy/cron-wrappers/astra_phase_models.py). The owner supplies `models.yaml` sections `default`, `triage` and `rca`; the engine must preserve configured values even when a provider/model pairing looks unusual or a fallback chain disagrees with an explanatory comment.

A block starts at a column-zero key and includes its subordinate text. Unindented YAML list entries belonging to that block remain in it. For each block present in the selected section, replace the live block completely with the selected text. Keys within a replaced block that the selected block omits are removed. Blocks absent from the section retain their bytes, comments and order. Do not perform a key-level merge, reconstruct YAML from parsed objects, normalize quotes, or silently correct values.

Compute splice ranges against the original configuration and apply them consistently in one pass. Verify the complete resulting YAML in memory before writing. Invalid YAML, duplicate section headers, duplicate column-zero keys and ambiguous structure refuse the operation before a config write. Semantic equality is insufficient for a no-op: quoted and unquoted values, formatting and multiline differences still require the requested text replacement. Skip only when the relevant text is byte-identical.

Use the existing lock for apply, arm and restore. Refuse an overlapping apply/arm while an unrestored live backup owns restoration. Keep the apply's exact original backup reference; do not select a different backup by modification time. Backup names must not collide, and older backups must not be pruned. Preserve configuration mode bits and line endings, including CRLF. Write through a flushed temporary file and atomic replacement. A malformed request must leave the current configuration unchanged.

Restore the exact pre-apply bytes from that operation's snapshot. Applying `default` is not restoration. Idempotent restoration and repeated same-phase application must not replace the original restoration baseline with a later phase state. A watcher may restore after the intended session has demonstrably loaded its configuration, with a bounded failsafe deadline for a session that never starts. Actual Hermes snapshot/failover behavior and loaded model must be verified for the installed version; file edits alone do not prove session isolation. Restore generation checks must prevent an old watcher from undoing a newer operation.

Phase failure remains explicit and may produce one deduplicated concise notice. It does not authorize changing model/provider/fallback policy, hiding refusal, or arming a job without its expected configuration. See [Operations](OPERATIONS.md) for authorized native scheduling and recovery.

## 10. Human report and question mechanics

The required HTML is self-contained and host-neutral. Show status, severity, impact and next action first; make detailed evidence expandable, respect browser dark/light preference and expand details for print. Phone width must remain scannable and desktop width bounded. Rendered offline reports make zero network requests and keep questions visible. An inaccessible optional reply route must not make the diagnosis unreadable or block monitoring.

Keep the report ID, filename and immutable original RCA hash across repair updates. Initial reports start at revision 1 and `open` with an attributed history entry. A separate authorized repairer may be any agent; the diagnosis author and folder-derived reply ownership do not automatically assign repair authority. The existing renderer's concrete fields and commands are recorded in [Implementation reference](IMPLEMENTATION-REFERENCE.md#report-document-and-renderer-interface).

Question types are `choice`, `multichoice`, `text`, `longtext`, `number`, `boolean` and `file`. Use unique lowercase IDs. Permission questions start unselected; an explicit Yes/No choice must not imply consent by default. Text can constrain `max_length`; numeric input can constrain `min`/`max`; attachments may be optional. Author one question definition and derive registrations from it. Changes advance the form version; old answers cannot satisfy a new permission question.

Offline copy/download exports entered answers and selected filenames, not file bytes, and visibly says the answers were not delivered. Clipboard failure leaves selectable text. Explicit online submission uses the currently configured HTTPS origin and current form registration; file selection may need repeating. Submission receipt means inbox delivery only, not answer processing, human identity authentication, permission fulfillment or repair execution. Neither a caller-supplied actor string nor network access establishes owner confirmation. Do not execute instructions in uploaded content.

Updates lock their target, require the expected revision, preserve existing attempts, append attributed history and atomically replace the same file. Stale or incompatible updates are explicit conflicts and preserve both inputs for resolution. `fixed` requires passing recorded checks; `verified by owner` requires an explicit confirmation reference. Deferred and will-not-fix dispositions require a reason. Resolution, reading and reply state remain independent. Record repair progress without completing the original batch again or rewriting its diagnosis.

This is a report contract, not a notifier or repair executor. Optional reply/delivery integrations remain separately configured. Recover failed HTML publication or delivery using the existing diagnosis, original identity and latest accepted revision; do not create another RCA to obtain a message.
