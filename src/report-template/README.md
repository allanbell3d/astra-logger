# RCA / incident-and-repair template — v1.0.0

HTML is the required format for reports delivered to people, whether accessed through a published link or downloaded through the configured native route. The HTML files are self-contained and host-neutral; their generation does not depend on a dashboard or Tailscale. Markdown may remain the internal source/archive format, but it is not a substitute for the delivered HTML. Any agent may author the RCA; a different agent on any authorized host may record a repair. This is a reporting format, not a repair executor or notifier.

## Repository and optional integration locations

- Repository template: `src/report-template/rca-template.html`; the optional delivery package pins its own release assets.
- Repository helper: `src/agent/astra/rca_report.py`, beside `rca_report_shell.html`. The examples below run from the repository root on Linux and write only the specified local output.
- An installed template package may use `~/shared/astra/report-template/`; verify the selected instance instead of assuming that location.
The following paths belong only to the optional reply integration; they are not prerequisites for core ASTRA:

- Questions: `~/shared/dashboard/_private/forms/`.
- Answers: `~/shared/dashboard/_private/inbox/<publishing-agent>/<receipt>/answer.json`.

All hosts use their own user's home. Filesystem API/CLI paths expand `~`; JSON `item_path` and attachment paths stay relative to their defined root. Browser URLs use the current origin; do not embed a hostname.

Existing ASTRA installations may use the same helper at `~/.hermes/scripts/astra/rca_report.py`. The standalone distribution does not install Hermes or automatically patch an existing pipeline.

## Author a report

Extract the commented template's content block into a private working file:

```sh
python3 src/agent/astra/rca_report.py extract src/report-template/rca-template.html ./incident.json
```

Fill `id` (unique), title, author `agent`, affected hosts, component, severity, publication/update timestamps with timezones, next action, and the `rca` block. Remove `is_template` or set it false; set `document_type` to `rca`. Start at revision 1/status `open`, with an initial history entry. The required content covers impact, timeline/evidence, facts/hypotheses, checks/results, root cause/confidence, taken versus proposed actions, risks/permissions, rollback and post-repair verification. Unknown remains unknown; future results are not successful checks.

Author `questions` once. Types: `choice`, `multichoice`, `text`, `longtext`, `number`, `boolean`, `file`. Use unselected `choice` with `choices: ["Yes", "No"]` for explicit permissions. IDs are unique lowercase identifiers. File requests may be optional; text accepts `max_length`, numbers `min`/`max`.

Render a new report in an authorized agent folder:

```sh
python3 src/agent/astra/rca_report.py render ./incident.json "$HOME/shared/my-agent/incident-ID.html" --shared-root "$HOME/shared"
```

The helper generates the matching private form registration. Do not edit two copies of the question definition. The render CLI refuses to overwrite an existing report; use update for repair history. Folder-derived inbox ownership is independent of the RCA author/assigned repairer. A repairer on another host reads the same report's inbox using existing authorized access—no new cross-host routing service is included.

## Record a repair in the SAME report

Keep the filename, ID and original `rca` block. Update only `status`, `repair`, `questions`, `next_action`, `reason` or `owner_confirmation`:

```sh
python3 src/agent/astra/rca_report.py update "$HOME/shared/my-agent/incident-ID.html" ./repair-update.json --expected-revision 1 --actor repair-agent --shared-root "$HOME/shared"
```

Example assignment:

```json
{"status":"repair assigned","repair":{"assigned_agent":"repair-agent","assigned_host":"affected-host","assigned_at":"2026-09-22T12:00:00Z","attempts":[],"followups":[]},"reason":"Explicit owner assignment"}
```

Lifecycle: `open` → `repair assigned` → `repairing` → `fixed` → optional `verified by owner`. `deferred` / `won't fix` require a reason; resumption/reopening is recorded. Assignment requires a named repairer. Completed attempts record `agent`, `host`, `started_at`, `finished_at`, `actions`, `changes` including backup references, and `verification` entries with `check`, `result`, `passed`, `evidence_ref`.

Fixed requires recorded passing verification; owner-verified requires an explicit confirmation reference. These data checks are not identity authentication or permission to execute commands. Existing attempts are append-only. The updater locks the report directory, rejects stale revisions, preserves the original RCA hash, appends history and atomically replaces the same file. Changed questions increment form version. Older answers do not satisfy new repair questions.

Resolution status, read state and reply state remain independent. Metadata status/update time/revision make changes detectable for external notification workflows. The template itself sends no notifications and performs no repairs.

## Owner UX and offline mode

Status, severity and impact lead the page. The usability target is to understand those three facts within about five seconds. Evidence is expandable, dark/light modes follow the browser, and print expands details. Layout prioritizes approximately 390-pixel phone width and one-thumb use, and also supports a desktop viewport around 1366 pixels without turning the page into an unbounded wall of text. These are acceptance targets, not claims that every reader or device has been measured.

Rendering makes zero network requests. Questions remain visible offline. Copy/download exports answers and selected filenames—not attachment bytes—and labels them not delivered. Clipboard failure leaves selectable text.

**Connect dashboard replies** explicitly loads only the current host's widget/API over HTTPS. It verifies current form registration and transfers entered answers; reselect files before sending. Offline, missing plumbing or stale registration leaves the fallback visible. A receipt confirms inbox delivery, not repair execution or processing.

## Optional ASTRA adapter

The renderer accepts `from_markdown(text, finding_id, batch_id, lane, context)` and `publish(html_path, data, shared_root)`. Keep the existing Markdown archive, validation, store updates, lane boundaries and notification contract. Replace only the HTML presentation call after the appropriate review of your ASTRA version. Do not rerun batch completion to record a repair.

An optional block in original Markdown can supply concise fields/questions:

````markdown
```rca-meta
{"impact":"New events are delayed","problem":"Collector cannot read input","cause":"Permission mismatch confirmed by read test","confidence":"high","next_action":"Owner decision required","questions":[{"id":"prepare","label":"Prepare a repair proposal?","type":"choice","choices":["Yes","No"],"required":true}]}
```
````

Without this block, named evidence sections are preserved and no questions, approvals or completed repairs are invented. The supplied worked example is fictional and explicitly labeled; it is not evidence that a live system was repaired.

## Maintenance

Back up before upgrades. Keep the helper and shell versions together. Never rewrite historical incident evidence solely to normalize old filesystem paths. Test actual same-host submission, current form revisions, stable identity/RCA hashes, offline fallback, print and phone/laptop layouts. Human reading time is a usability goal, not a universal measured guarantee.
