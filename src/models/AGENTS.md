# Model playbook example

models.example.yaml uses the phase engine's raw-text format. Bare phase markers are followed by top-level replacement blocks; do not parse/rewrite the whole file as ordinary nested YAML. Missing blocks remain unchanged; restore returns the exact pre-apply bytes.

Values are placeholders, not recommended providers, models or spending budgets. Validate phase parsing and replacement/restore through offline phase tests. Runtime credentials, native job pin precedence and accepted provider settings require instance validation. Do not copy an owner playbook into this public folder or choose models in code.
