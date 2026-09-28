# Offline regression tests

Retain tests for accepted behaviors even when they expose a real implementation bug. A passing assertion that enforces a superseded policy is not a valid target regression. Correct it without disguising the resulting implementation gap as a pass. The delivery-skill CLI/instruction test module is temporarily withheld with src/skills. Core delivery-engine tests use the included report template; they do not require the proposed skill directory. Core report-template and other independent regression tests remain included. Fix stale paths or fixtures without weakening assertions. Use repository assets and temporary stores/homes; mock services, native job APIs and network sends. Never require an owner's live home or restore deleted operational fixtures to make a test pass.

Byte-offset fixtures must write exact bytes. Timestamp tests must pin a timezone or compare instants according to the specific contract. Preserve POSIX lock and 0600 assertions; Linux execution is required for those cases. Do not fake fcntl or add blanket skips. The optional owner-playbook test may skip when its private fixture is absent.

Run focused tests after edits, then one full suite at a meaningful checkpoint. Report failures and platform limitations honestly. Never infer deployment readiness from parsing, matching source copies or historical green reports.
