# ASTRA implementation and decision backlog

Status: canonical MVP target contract; implementation acceptance remains open.

The canonical set defines the target. This list identifies what prevents a claim that the target is implemented and accepted. It is not authorization to execute every item, expand the component, deploy, spend on model runs, delete history, or resume schedules.

Status basis: a bounded reading of the local source and documentation. No live store was inspected and no production job was run. Offline suite results are recorded for the current cleanup; they do not establish native deployment readiness. Owner-reported capture/enrichment operation and paused cron are operating context, not independent runtime verification.

## 1. Apply settled policy without reopening decisions

[Decisions](docs/DECISIONS.md) records the accepted product choices. This backlog contains remaining implementation/acceptance work. Multi-error RCA including unrelated members, mandatory triage, evidence-based material-change reopening, frequency-only reopening deferred, active-B finish on disable, B new-arrival-only enablement, retained RCA reports, HTML delivery and minimum lifecycle tracking are settled.

- [ ] **TD1** — Verify the actual triage output path supports script-first tagging plus substantive LLM judgment: identify bad classification/severity or unforeseen scenarios in supported disposition/reason output while retaining original tags/provenance. Inspect existing methods before proposing any persistent override field or schema; do not infer policy-write authority.
- [ ] **TD2** — Verify free steady-state triage and capable low-cost RCA routing with bounded failure/fallback behavior and observable effective primary/auxiliary/fallback configuration. Keep the approved initial tuning exception in TD35 distinct from the steady-state configuration.

## 2. Correct eligibility and lane isolation first

- [ ] **TD34 — Repair script-based triage exclusions with the next implementation repairs.** Local `ReviewStore.ingest` excludes new `ignore`/continuation findings from ordinary triage. A first unseen `tracked` finding can initially await review, but a second occurrence/replay while unreviewed can set `decision=tracked` and legacy `rca_done=1` before any LLM assessment. The repository's broad missing-permissions/access rule can feed this path. Retained evidence and an automatic flag are not proof of assessment or diagnosis. Correct and tune candidate selection using existing mechanisms; preserve quiet accounting for proven unchanged/expected events and original tag provenance. Include repeated first-time permission errors, intentionally restricted routes, unknown cases and continuation context in the repair checks. Current details are in [Implementation reference](docs/IMPLEMENTATION-REFERENCE.md#local-review-schema). This is repair/tuning work, not an unanswered policy question blocking documentation; no one admission strategy has been selected in advance.
- [ ] **TD35 — Tune tagging, filtering and triage during the first few weeks.** Compare different models, including inexpensive paid models temporarily, to identify classifier/filter mistakes and sufficient bounded packet context. Use the results to adjust the existing rules and workflow toward reliable operation achievable with free triage models; do not assume a free model can compensate for arbitrary missing context or poor filtering. Retain representative outcomes, missed/false escalations and observed cost/limits. Keep model choices and tuning bounds in configuration, avoid uncontrolled fallback, and establish the free-model steady-state configuration after tuning. This extends the planned repair/commissioning work; it does not introduce a new service, schema or mandatory recurring model sweep.

| Gap | Source-local observation | Required result |
|---|---|---|
| A can suppress B | `review.py` excludes globally covered findings and uses global busy membership; A completion registers coverage used by later eligibility | A requested comparison retains B's identical assigned input and separate outcome regardless of A completion or lease state |
| Optional B gate needs implementation acceptance | Dispatch recognizes the mapped card and existing pause state; prepare/dispatch do not yet prove the complete accepted gate | No new launches while closed; active B finishes; enabling B selects only new work after enablement without historical catch-up |
| Per-lane effective models are unproved | Both wrappers use the shared `rca` phase; native pin precedence and actual session loading were not checked | Demonstrate different configured models on the same input without shared-phase races or silent fallback |
| Coverage needs behavior acceptance | `coverage.py` retains signature/scope/report relationships, but source presence does not prove correct suppression or deployment | Unchanged scoped diagnoses reused; distinct resources/routes/impact and genuine post-repair recurrence reconsidered |

Retain existing useful coverage work as the starting point. Correct the smallest mechanism that fails the contract. Do not replace it with a new architecture because a prior implementation is imperfect. Required permanent diagnosis tracking and reuse of the existing `rca_investigations`/`rca_coverage` methods are settled. Existing code still needs behavior and deployment acceptance; any further physical schema expansion needs its applicable approval.

## 3. Complete exact member and lifecycle accounting

- [ ] **TD3** — Implement bounded multi-error RCA preparation and exact per-member completion through both `ReviewStore.complete_rca` and `Delivery.complete`. The current first-ID behavior must not mark other members complete. Unrelated eligible errors may share a batch, but each needs its own outcome; preserve pending overflow and refuse unaccounted membership without consumption.
- [ ] **TD4** — Retain lane identity in bounded RCA history output. The inspected `ReviewStore.get_history` selects stored RCA records but omits their `lane` field from the returned report dictionaries; callers must be able to distinguish A and B results.
- [ ] **TD5** — Verify exact triage membership, allowed decisions, the lower and upper reason bounds, and final encoded packet limits through the wrapper used in operation. The inspected completion enforces the 12-character lower reason bound; the target 160-character maximum needs alignment.
- [ ] **TD6** — Verify attempt accounting at lease, the retained 600-second/two-attempt behavior, correction within the same lease, explicit blocked state, and progress for unrelated IDs.
- [ ] **TD7** — Prove that source-bucket rewriting/rescanning is idempotent and that expired evidence leaves unresolved obligations visible. Do not infer these outcomes from historical test counts.
- [ ] **TD8** — Map the minimal logical diagnosis, repair, report, publication and notice states to existing store methods. Fill only demonstrated gaps; propose any necessary physical schema change explicitly before implementing it.
- [ ] **TD9** — Verify that inconclusive work and diagnosed-but-unrepaired work do not repeatedly trigger identical paid investigation, while a meaningful new development can reopen the issue with a recorded reason.

## 4. Verify report and delivery recovery

- [ ] **TD10** — Ensure every human report, including RCA, daily review and repair/closure reports when produced, is delivered as self-contained phone-readable HTML. Verify configured publication, a direct HTML link and downloadable HTML without private-network access; retain existing Markdown source/archive interfaces and recover rendering/delivery failures without another RCA.
- [ ] **TD11** — Trace the existing persistence and delivery path narrowly enough to identify which required state is already present and which behavior is missing. Preserve working mechanisms; do not turn partial implementation into a mandate for new infrastructure.
- [ ] **TD12** — Prove that publication/send failures retain the original diagnosis and retry delivery with the same report identity, without new RCA or loss of repair progress.
- [ ] **TD13** — Prove immutable original diagnosis, attributed repair attempts, conflicts on stale incompatible updates, passing checks before fixed status, and explicit owner confirmation before owner-verified status.
- [ ] **TD14** — Verify deduplicated native notices across repeated ticks and restart, immediate incident alerts only for genuinely new signatures or critical service routing outages, and one compact deterministic daily recurrence balance. Preserve separately required report/repair closure sends and requested replies. Check the actual destination and send result; neither a saved file nor native `last_status=ok` proves receipt.
- [ ] **TD15** — Verify that daily review reports useful existing outcomes/counts and unresolved obligations without changing eligibility or launching work.

## 5. Align configuration and portability

- [ ] **TD16** — Complete Linux suite verification. The isolated Windows checkpoint has 259 tests: 196 passed, 18 failed, 44 errored, 1 optional private-fixture skip. Remaining failures include POSIX locking/permissions, directory fsync and syncing read-only archive handles; cleanup exceptions can obscure the first failure. WSL could not start because virtualization is unavailable. Do not remove these tests or advertise Windows support on this evidence.
- [ ] **TD17** — Review existing host-profile defaults in runtime/deployment adapters before public installation guidance. Neutral examples do not remove hardcoded assumptions in source.

- [ ] **TD18** — Remove disagreement between configured work/profile roots and hard-coded wrapper assumptions using the smallest existing configuration path.
- [ ] **TD19** — Prove phase apply/load/restore ordering, refusal before mutation, backup identity and pending-operation locking. Do not mistake staggered start times for confirmed lane model isolation.
- [ ] **TD20** — Verify supported OS collection and scheduler adapters before advertising portability. The Python core does not establish that every shell wrapper or service unit works everywhere.
- [ ] **TD21** — Align the affected repository triage/RCA skill instructions with packet schemas, helper behavior and the deliberately installed copies before commissioning. Historical cross-host divergence is not proof of today's state, and this does not authorize broad skill synchronization or importing unrelated skills.
- [ ] **TD22** — Align the built-in default policy with the explicitly selected release policy without silently changing classifications. `src/config/severity_policy.json` is the repository policy source (v4.2-tracked); the installed capture launcher reads its deployed name `~/.hermes/scripts/astra-severity-policy.json`. Explicit policy paths load JSON and missing/invalid files fail. Omitting a policy path uses the older v4.1-table built-in default. The duplicate repository JSON has been retired; this does not alter any installed policy.
- [ ] **TD23** — Establish the supported Python/dependency versions, install/import path and safe entrypoints before writing a copy-paste installation guide. The capture wrapper can start dispatch and is not a side-effect-free example.

## 6. Accept, then commission within explicit scope

Current commit-preparation checkpoint: a staged-tree export retained 27 passing tests plus two subtests in a focused Windows run. Corrected target assertions exposed four failures in three existing repair areas: urgent RCA still bypasses validated triage (`test_review.py`), B either skips the frozen A input or is suppressed by A (`test_repair_flow.py`, `test_investigation_coverage.py`), and a missing original report still establishes coverage (`test_investigation_coverage.py`). These remain implementation repairs under the eligibility/lane gaps and TD8–TD9; the assertions must not be reverted to the superseded behavior. The separately corrected broad-label attribution test reaches HTML publication but fails on Windows directory fsync; Linux acceptance is outstanding. This was a focused checkpoint, not a full-suite pass.

Use [Acceptance](docs/ACCEPTANCE.md) for a finite set of representative cases. For an eligibility change, use a small time-ordered retained sample and a separate held-out sample that demonstrate reuse and genuine reopening. Do not rerun the entire historical corpus or infer reduced spend from annotation-only comparisons.

- [ ] **TD24** — Pass focused tests for the change, then the current repository suite at the implementation completion/deployment checkpoint.
- [ ] **TD25** — Record what passed, what failed and what remains untested; avoid claims based on old run artifacts.
- [ ] **TD26** — Perform a separately authorized bounded native invocation for the approved lane/configuration and delivery destination when offline proof is sufficient to justify it.
- [ ] **TD27** — Deploy only scoped files with backups and transfer verification. Confirm schedule state separately; synchronization must not resume cron.
- [ ] **TD28** — If a clean start is selected for MVP commissioning, inspect exact configured logs/work roots, review state, cursors and pending jobs; propose a fresh configured root or exact reset and obtain live-action/deletion authority before changing it. Do not implement historical backlog management.

## 7. Follow-up: compressed enriched-log retention

- [ ] **TD29** — Extend the existing main enriched-log rotation so logs leaving the 72-hour active window are retained in compressed form instead of being deleted. Inspect and reuse the current rotation/storage path first. Verify the compressed copy is durable and reconstructs the original content before removing the active file, and preserve enough identity/path information for retained evidence to remain discoverable. Cover boundary rotation, interrupted compression and failed writes with focused tests. This is a separate follow-up from RCA report archives; it does not authorize a new long-term deletion policy or live migration.

## 8. Prepare public release and replace old documentation

- [ ] **TD30** — Accept the target document set against the complete settled product contract; keep implementation gaps visibly separate.
- [ ] **TD31** — Include maintainable source, schemas, tests and public documentation; do not adopt an old proposal to hide all tests/docs from version control.
- [ ] **TD32** — Verify package/license metadata, neutral examples and exclusion of credentials, operational records, private provenance, backups and generated artifacts. Check actual ignore/release rules, not directory names. Review configuration examples, historical signature catalogs, `.env` files and variants, deployment wrappers and worker instructions for real identifiers or credentials. Sanitize intended examples and exclude runtime/private artifacts; file presence or an old public label is not publication approval.
- [ ] **TD33** — Review the exact publication and remaining private-folder deletion set before irreversible cleanup. Superseded local files have been preserved privately; existing archives and remote hosts remain outside this cleanup.
