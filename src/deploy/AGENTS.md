# Deployment adapters

cron-wrappers/ contains installed role prepare entry points and the phase engine. systemd/ contains Linux service examples requiring instance configuration. These are adapters to existing Hermes scheduling, not permission to create a new scheduler or resume jobs.

Keep native API, environment, lock, timeout and restoration contracts consistent. Local source cleanup is separate from installation. Never infer that the newest filename or a deployed snapshot is authoritative. Verify the exact target and back up before any separately authorized deployment.
