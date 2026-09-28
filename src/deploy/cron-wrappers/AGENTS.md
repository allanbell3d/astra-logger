# Native role preparation and model phases

Keep triage, daily, RCA and optional B wrapper names stable. astra_rca_prepare_b.py is the B wrapper; dated comparison commissioning scripts are not its replacement. A worker consumes its preprepared batch and must not rerun prepare to obtain another lease.

astra_phase_models.py preserves raw configuration text and operation-specific restoration. Do not reserialize YAML, pick models, use newest-mtime backup recovery or substitute fake file locking. Run phase tests in an isolated home on Linux for lock/permission evidence. Preparing a role may mutate state or arm work, so never invoke wrappers against live profiles for a smoke test.
