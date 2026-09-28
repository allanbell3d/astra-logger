# ASTRA implementation

Intake, enrichment, grouping and storage are deterministic. ReviewStore owns persistent review writes; callers use its methods. Preserve captured evidence, bounded reads, source identity, journal cursors and explicit expiry/truncation. Classification, severity, triage decision, diagnosis, repair and delivery are distinct.

Use existing coverage/runtime/dispatch paths; do not add a parallel engine. A/B history and write targets remain independent, while requested comparison inputs must be identical. Reuse an unchanged diagnosis only within valid scope; delivery recovery must not create a new RCA. Keep policy/model choices in configuration.

For changes, run the focused behavioral tests and inspect failure causes. POSIX locks and durable directory writes require supported-platform verification. Never silence errors, fake locks or weaken evidence accounting to make tests pass.
