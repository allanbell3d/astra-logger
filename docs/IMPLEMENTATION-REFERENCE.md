# ASTRA implementation reference

Status: verified local source declarations; deployed acceptance remains open.

This reference records existing mechanics without granting deployment or schema-change authority. [Architecture](ARCHITECTURE.md) defines component boundaries; [Specifications](SPECIFICATIONS.md) defines the intended product behavior. A declaration here may expose a gap against that target. It must not silently narrow the target to what the current code happens to do. Schemas below are descriptions, not migration instructions. All persistent review mutations belong to `ReviewStore` methods.

## Evidence, cursor and derived data

[CursorStore](../src/agent/astra/cursor.py) stores a `files` mapping keyed by real path, with `inode`, `dev`, `offset` and `size` per source. Rotation or truncation changes the source generation. [Raw intake](../src/agent/astra/raw_pipeline.py) uses separate file and journal state; its recovery journal captures enriched/compressed byte boundaries, prior file and journal cursors and affected store segments. File references use inclusive `byte_start` and exclusive `byte_end`. Journal records retain `__CURSOR`, boot/unit metadata and realtime time; their identity never depends on treating `journald:0-0` as a seekable file.

`ReviewStore.ingest` hashes host, profile, source path, record ID, byte range, journal cursor, event time and text. When present, inode, device and first-line content hash also participate. This produces a 64-character evidence ID. Equal journal text with different cursors remains different evidence. Source rotation can likewise produce different evidence despite equal text/offsets. The finding ID is a 20-character digest of fingerprint key and severity, with existing normalization/alias handling for volatile cron/cooldown presentation. These hashes are identity mechanisms, not root-cause proofs.

[Enrichment](../src/agent/astra/enrich.py) copies original captured fields. Its classification view excludes the classifier's `text` field and includes capture `event_ts` when supplied. Added keys are `astra.classification`, `astra.fingerprint`, `astra.severity` and `astra.provenance`. Provenance contains `source_path`, `byte_start`, `byte_end`, `rule_version`, `tier0_sig_original` and `severity_policy_version`; the original Tier-0 signature comes from the supplied original field or `sig`. A colliding existing key remains intact and the new value is placed beneath `astra.pipeline`.

Historical raw dictionaries used `text`, `ts`, `host`, `profile`, `file`, `tier0_sigs` and `rule_version`. Classification/mining dictionaries can additionally expose event, cause, domain, provider/model/platform, component/target, codes, attempts, matched rules, confidence and conflicts. They are not all mandatory fields of every current record. Preserve supplied metadata and distinguish original capture signatures from later matched rules. Unknown classifications remain unknown. A mining count represents evidence rows, not necessarily unique incidents; coverage is not accuracy. A normalized/redacted example cannot be described as a verbatim original. Avoid reusing scalar names to overwrite incoming metadata.

Provider context is independent of event and cause. Distinguish provider-specific quota/authentication/routing outcomes, generic rate limiting from a particular service's typing limit, and validation refusal from execution failure. Device extraction must require explicit hardware entities rather than near-matching words or project directories. Numeric/path normalization must preserve diagnostic resource identity; traceback source filenames are not automatically affected resources. A flat signature cannot substitute for event, cause and continuation context. Markdown rule tables describe rules but do not execute them.

[CompressedStream](../src/agent/astra/compressed.py) is a presentation interface. `read_tail` supports a time window and an explicit `max_bytes` read bound (local default 500,000 bytes); that default is neither the runtime review packet cap nor a recommendation to send half a megabyte to a model. It can exclude ignored/continuation rows and clip text while retaining timestamp, semantics and source pointers. The current native review packet is prepared from synchronized enriched evidence in SQLite, not directly from a compressed tail. Historical fragmented LLM-file batching is not the native review contract, although legacy helper code remains in the tree.

[Storage](../src/agent/astra/storage.py) declares `BUCKET_HOURS = 12`, `RETENTION_HOURS = 72` and `MAX_TOTAL_BYTES = 250 * 1024 * 1024`. Active files have `.jsonl`; compressed archives have `.jsonl.gz`. Event time selects local half-day buckets with deterministic provenance tie-breaks. These module defaults are distinct from manifest/operator controls. Retained beyond-window compression has local code support but remains an end-to-end acceptance item; RCA report archive automation is deferred.

## Local review schema

[ReviewStore](../src/agent/astra/review.py) opens SQLite with a 20-second connection timeout, uses row objects, and applies POSIX file mode `0600`. These are current declarations, not a Windows ACL or deployment acceptance guarantee.

| Table | Declared columns and key |
|---|---|
| `evidence` | `id TEXT PRIMARY KEY`, `gid TEXT NOT NULL`, `ts TEXT NOT NULL`, `row_json TEXT NOT NULL`; index on `gid` |
| `findings` | `id TEXT PRIMARY KEY`, nullable `decision`, `reason`, `reviewed_at REAL`; `rca_done INTEGER NOT NULL DEFAULT 0`; migrated `rca_a_done` and `rca_b_done` with the same integer default |
| `batches` | `id TEXT PRIMARY KEY`, `kind TEXT NOT NULL`, `ids_json TEXT NOT NULL`, `status TEXT NOT NULL`, `attempts INTEGER NOT NULL`, `lease_until REAL NOT NULL`, `created REAL NOT NULL` |
| `sources` | `path TEXT PRIMARY KEY`, `stamp TEXT NOT NULL` |
| `rca_history` | autoincrement `id INTEGER PRIMARY KEY`, `finding_id TEXT NOT NULL`, `batch_id TEXT NOT NULL`, `report_path TEXT NOT NULL`, nullable `shared_path`, `completed_at REAL NOT NULL`, nullable `summary`; migrated `lane TEXT NOT NULL DEFAULT 'a'` |
| `runtime_meta` | `key TEXT PRIMARY KEY`, `value TEXT` |

The lane migration seeds legacy done flags once and records `lane_seed_done`. It is historical compatibility machinery, not permission to reset flags to backfill B. The `batches` table has no `updated` column. `ids_json` contains exact assigned finding membership. Time fields declared REAL use epoch seconds; evidence `ts` and JSON document timestamps have their own string formats.

`sync_sources(root)` scans `enriched-*.jsonl` directly in that directory, records `mtime_ns:size`, and replays changed files idempotently. A generic caller-named `enriched.jsonl` does not match that scan contract. Rows must be JSON objects; malformed or oversized rows refuse scan completion and do not advance the source stamp. The per-line reader checks a 1,048,576-character bound; it must not be mislabeled as an encoded byte cap. Blank lines are skipped. Ingestion refuses absent/UNKNOWN event timestamps. Duplicate retained identities do not reinsert ordinary evidence; tracked-severity handling has its own existing path.

Finding dispositions in storage include NULL/unreviewed and internal values such as `ignore` and `evidence_expired`, in addition to the four triage result decisions. They are not interchangeable enums. Ingest currently sets a new finding to `ignore` when script severity is ignore or the record is a continuation; ordinary triage selection takes NULL or uncertain RCA findings, so ignored rows do not reach it. Tracked handling updates an existing unreviewed finding before inserting a new finding. A first unseen tracked finding therefore starts with NULL disposition and can reach triage; a second retained occurrence or replay while it is still NULL changes it to `tracked` and sets legacy `rca_done=1`, without an LLM review. Several occurrences in one synchronization can therefore suppress first-time review. That legacy flag does not prove a diagnosis. The repository severity policy gives `missing_permissions`/`missing_access` a first-match tracked rule without resource/platform restriction; the capture launcher loads an installed policy file, so the repository rule is not proof of the active policy on a running host. The packet carries script labels and compact context, but not the complete original rule/provenance fields. The result accepts disposition/reason and validated coverage/reopen references; it has no processed classification/severity override. Decision/reason can explain wrong tags or an unpredicted scenario. The excluded-candidate behavior is recorded as repair TD34, with tagging/filtering/model calibration in TD35; it remains an implementation gap to address with the next repairs, not a pending owner choice blocking the documentation. The confirmed judgment path uses existing decision/reason, without a new persistent override. Preserve original labels; do not assume unused extra JSON fields update them. Count findings directly rather than multiplying them through an unaggregated evidence join. Split evidence-unavailable work from retained actionable backlog and name the query time and count unit.

## Investigation provenance schema and references

[Coverage](../src/agent/astra/coverage.py) currently creates these additive tables in the same store:

| Table | Declared columns and key |
|---|---|
| `rca_investigations` | `history_id INTEGER PRIMARY KEY`, `problem_key TEXT NOT NULL`, `outcome TEXT NOT NULL`, `scope_json TEXT NOT NULL`, `basis_json TEXT NOT NULL` |
| `rca_coverage` | `finding_id TEXT NOT NULL`, `history_id INTEGER NOT NULL`, `signature TEXT NOT NULL`, `evidence_json TEXT NOT NULL`, `reason TEXT NOT NULL`, `covered_at REAL NOT NULL`, nullable `reopen_json`, `reopened_at REAL`; composite primary key `(finding_id, history_id, signature)`; index on signature |

`history_id` anchors an actual history/report record. Evidence basis includes `evidence_id`, payload SHA-256 and scoped identity. Scope retains host/profile, provider/model/target, relevant resources and job identity; material includes normalized text, severity and explicit impact. Basis records the original report hash, bounded cause excerpt and the fact that coverage does not assert repair. Current outcomes include `investigated`, `diagnosed` and `inconclusive`; prose recognition in local code is not proof that all reports satisfy substantive diagnosis acceptance.

Exact signature reuse and different-ID relations are different paths. Cross-ID relations require matching concrete scope and an explicit persisted report citation identifying the other finding. Local citation validation requires 30–3,000 characters, membership in retained report text and supported relation wording; hypothesis/negative/unrelated wording is refused. Optional triage `coverage` accepts `history_id`, `relationship_history_id`, `quote`; optional `reopen` accepts `history_id`, `reason`, `evidence_id`, `quote`. They are mutually exclusive and validated, not arbitrary metadata. The owner permits a new scoped investigation for material cause/resource/impact change or recurrence after verified repair, under evidence and triage rules; unchanged known recurrence remains counted without repeat RCA spend.

Latest evidence ranks by `ts DESC, id DESC`. The per-store cache reuses identity only when the evidence ID and full serialized payload match; database change/version and transaction state control reloads. It refreshes current decisions and lane flags. Packet items are reused only within their preparation transaction. Shared-resource candidates that lack a validated relation remain uncertain and return to triage; similarity alone cannot prove coverage. Savepoints keep provenance updates inside the caller's transaction. Coverage itself does not write lane completion or repair flags. Historic isolated-candidate tests and old deployed reports are separate from current production acceptance.

## Review helper interface

[The CLI](../src/agent/astra/review_cli.py) requires global `--root` (directory of enriched buckets) and `--state` (review SQLite path). The installed shim supplies environment/import wiring; direct module use requires the package on the Python import path. This is an interface reference, not an installation command.

| Operation | Input | JSON output / effect |
|---|---|---|
| `sync` | optional epoch `--now` | `scanned_files`, `ingested_rows`; synchronizes store without changing source evidence |
| `prepare` | `triage`, `rca`, `rca-a` or `rca-b`; `--max-bytes` default 12,000; optional epoch `--now` | no-wake reason or exact leased batch packet; `rca` aliases A |
| `complete` | `--batch`; triage `--result` JSON or RCA `--report` Markdown; optional archive/shared directories and epoch | validated triage result or persisted RCA result; no send success implied |
| `evidence` | finding/evidence `--id`; `--max-bytes` default 8,192, allowed 1–131,072 | bounded excerpt with source identity/status and truncation |
| `status` | optional epoch `--now` | current counters, leases, coverage and dispatch booleans |
| `history` | `--limit` default 20 | `rca_history` and `findings_history` arrays |
| `retention` | optional ISO `--now` | `gap_detected`, `unsynced_files`, `missing_sources`, `retention_hours` |
| `prune` | optional ISO `--now` | expires review evidence under existing policy and exposes unresolved expiry; it is a mutation |
| `recover` | `--batch`, concrete `--reason`; optional epoch | targeted recovered membership/retained exclusions; original batch and attempt history preserved |

Successful output is JSON on stdout. Caught validation/storage failures emit stderr and exit 1 without a JSON success payload; unsupported command syntax is refused by the parser. A result containing `ok: false` also exits 1. The helper makes no model call and implements no scheduler. Opening `ReviewStore` can create/migrate local schema and `status` can refresh coverage, so “read-only helper” describes source evidence handling and observational intent, not a universal guarantee of zero SQLite writes. Never use diagnostic inspection to authorize a missing production store's initialization.

A triage completion returns `batch`, `status: reviewed`, and validated `findings`. It requires every leased ID exactly once; missing/extra/duplicate IDs are reported mechanically. Reasons require at least 12 nonblank characters in current store validation; the worker's 160-character upper bound remains a separate target. Recovery also requires at least 12 nonblank characters and refuses a batch with no recoverable retained unfinished members. Current blocked recovery marks original batch `recovered` and records its reason in runtime metadata; it does not blindly reset all work.

## Packet, evidence and status I/O

A waking store packet contains `wakeAgent`, `batch`, `kind`, `pending_findings` and `items`. Each current item contains `id`, `host`, `agent` (profile), `function`, `event`, `cause`, `provider`, `model`, `component`, `severity`, `evidence_rows`, `first`, `last`, `sample`, `evidence`, `sample_truncated`, `sample_original_chars`, `prior_status`, `exact_prior_findings`, `investigation_coverage`, `coverage_candidates` and `repair_state`. The display sample is 240 characters, not complete evidence. Exact-prior context is bounded to three related findings. Overflow remains pending.

[Runtime preparation](../src/agent/astra/runtime.py) synchronizes the enriched directory and normally prunes expired review evidence before preparation. It requests 24,000 bytes from the store. It then supplies `helper`, `result_dir`, `archive_dir`, `shared_dir`, `commissioning` and optional `case_file`; triage also receives `result_template` and `result_path`. Its template contains every supplied finding ID with null decision/empty reason, avoiding accidental valid defaults. An optional work-root known-issue reference is capped at 8,192 encoded bytes. Final serialized packet cap is 32,768 bytes, including this context. Daily preparation supplies `kind: daily`, `host`, `status`, history limited to five and retention; completed unchanged fingerprint state yields no wake.

Runtime maps `rca`/`rca-a` to work-root `rca-a` and B to `rca-b`; shared output is `manifest.shared / lane_directory`. Thus a manifest whose shared root already ends in `rca` naturally produces an `rca/rca-a` path. Do not append another inferred `rca` layer. Non-RCA roles use the adapter's `rca` default directory. This path provision is not authority to publish triage reports.

Evidence output includes `id`, `finding_id`, `ts`, `host`, `profile`, `source_path`, `byte_start`, `byte_end`, `journal`, `source_identity`, `status`, `truncated`, `text`, `classification` and `severity`. Identity status includes `retained`, `verified` and `reason` (`not_retained` or `identity_mismatch` where applicable). Excerpt status is one of `retained_summary`, `journald_entry`, `journald_unretained`, `rotated_or_truncated`, `exact_source`, `source_file_inaccessible`, `source_file_missing`. A first-line hash verifies that line only; text fallback must retain its actual status. Selecting a finding ID resolves its latest retained evidence.

Status fields include `pending_triage`, `pending_rca`, `pending_rca_b`, `covered_findings`, `coverage_review_pending`, `reopened_findings`, `reused_rca_lanes`, `active_leases`, `retry_eligible`, `blocked_batches`, `total_findings`, `total_evidence`, `findings_evidence_expired`, `early`, `early_reason`, `dispatch_rca`, `dispatch_rca_a`, `dispatch_rca_b`, `dispatch_triage_early`, plus delivery/repair counts when available. Each active lease supplies `batch`, `kind`, `attempts`, `lease_until`, `remaining_seconds`. In local code `pending_rca_b` mirrors the uncovered count; it does not prove B enabled. A positive dispatch predicate is not owner schedule authority.

History exposes RCA history ID, finding/batch IDs, local/shared paths, completion time and summary. Its current serializer does not include the table's lane column. Reviewed findings include ID, decision, reason, review time, legacy `rca_done`, repair state and investigation coverage. Do not infer per-lane truth from legacy `rca_done`. Retention distinguishes changed/unscanned buckets from tracked source files absent from both active and recognized archive paths.

## RCA completion and remaining target gaps

Current preparation uses a 600-second lease and a maximum of two leased attempts. It returns no wake while a matching live role lease exists, and blocks exhausted work. These constants are not manifest knobs. A correction inside a live lease does not create another batch. Current RCA selection stops after one finding; legacy completion reads `ids_json[0]`. The confirmed multi-member RCA contract therefore requires further work before acceptance: per-member outcomes and refusal of unaccounted members must replace that first-ID assumption.

`validate_rca_report` checks a minimum 100 nonblank characters and recognizable patterns for the nine required sections. It does not demonstrate that checks occurred, conclusions are justified or every member was accounted. The non-delivery completion path saves Markdown `rca-<finding>-<epoch>.md`, optionally copies Markdown and renders HTML to shared output, sets the relevant lane flag, marks the batch done, inserts history and registers coverage. Returned keys are `batch`, `finding_id`, `status: persisted`, `report_path`, `shared_path`, `shared_markdown_path`, `notification_pending: true`. A direct call without archive override uses a legacy `reports/rca` default; native runtime supplies separate lane directories. Filesystem and SQLite effects do not constitute an atomic transaction across both media. This non-delivery path renders HTML only when `shared_dir` is supplied. A retained Markdown archive without that destination therefore does not satisfy mandatory downloadable HTML; generation and verified native HTML attachment without publication remain acceptance gaps. The optional delivery path is separate and must be checked for the selected instance.

Current code contains shared busy/coverage filtering across RCA roles and does not by itself establish frozen same-input comparison. The target requires one captured shared input, independent output/leases and completion of already assigned B work even after A succeeds. B disable allows the active bounded run to finish and prevents new launches/retries; B enable applies only to newly arriving work, without backlog backfill. Verify those controls and effective routing before claiming optional comparison accepted.

Runtime can recover a missed completion from a bounded set of native output files: the transcript must contain the batch in the prompt and a nonempty response after `## Response`, not `[SILENT]`. A native exit code or transcript existence alone is not validated completion. Original report persistence, verified HTML availability and actual successful native send remain separate facts.

## Report document and renderer interface

[The renderer](../src/agent/astra/rca_report.py) declares version `1.0.0`, schema version 1 and statuses `open`, `repair assigned`, `repairing`, `fixed`, `verified by owner`, `deferred`, `won't fix`. A new document contains ID/title/agent, affected hosts, component/severity/status, published/updated timezone timestamps, revision, topic/project, next action, immutable `rca`, mutable `repair`, questions/form version and append-only history. Repair contains assigned agent/host/time, attempts and followups. The original RCA contains problem, impact, cause, confidence and full named sections; Markdown conversion also preserves source Markdown and finding/batch/lane identity.

Current `from_markdown(text, finding_id, batch_id, lane, context)` derives stable report ID from host, lane and batch; it retains all sections while using bounded summary excerpts for the page lead. Optional original `rca-meta` JSON supplies concise title/severity/component/next-action/topic/project/agent, problem/impact/cause/confidence and questions. Without it, no permission questions or completed repairs are invented. `publish(path, data, shared_root)` renders a local HTML projection and may register configured replies. HTML presentation does not replace internal Markdown validation/archive interfaces.

The existing CLI offers `extract REPORT OUTPUT`, `render DATA OUTPUT [--shared-root ROOT]`, and `update REPORT CHANGE --expected-revision N --actor ACTOR [--shared-root ROOT]`. Rendering a new file refuses overwrite; update preserves the same file and original RCA hash. The only permitted update keys are status, repair, questions, next action, owner confirmation and reason. Existing attempt prefixes must remain unchanged. Stale revision, invalid transition, missing assigned repairer, unsupported version, missing verification or absent explicit owner confirmation refuses mutation. The updater currently imports POSIX `fcntl`; cross-platform acceptance cannot be inferred from the renderer's host-neutral document format.

Current permitted transitions are:

| From | To |
|---|---|
| open | repair assigned, deferred, won't fix |
| repair assigned | repairing, deferred, won't fix |
| repairing | fixed, open, deferred, won't fix |
| fixed | verified by owner, open |
| verified by owner | open |
| deferred | open, repair assigned |
| won't fix | open |

Same-status updates may append progress. Fixed checks the latest attempt for passing verification and required actor/host/times/actions. Owner-verified checks a confirmation reference; this is data validation, not human authentication. A new investigative recurrence is a new episode rather than alteration of the original cause. See [Specifications](SPECIFICATIONS.md#10-human-report-and-question-mechanics) and the [template contract](../src/report-template/README.md) for human/offline behavior. Separately configured delivery/reply adapters are described outside this core reference.
