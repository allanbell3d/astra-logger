# Configuration sources

severity_policy.json is the sole repository severity policy; installation copies it to the launcher's astra-severity-policy.json filename. Ordered rules use first match. Rule outputs may include tracked; the current default accepts ignore, watch or needs-attention. Use the loader's actual behavior and tests as implementation evidence. JSON Schema references are withheld from the initial baseline pending validation; do not infer that the runtime loads them.

astra-host.example.json is a neutral profile-routing example, not a deployed host selection. Do not add real recipient IDs, credentials, provider choices or host snapshots here. Older built-in policy defaults remain a known implementation gap; changing them requires explicit behavior review and regression evidence.
