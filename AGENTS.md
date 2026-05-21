# Repo Rules

## Control/Data Separation

- Do not use user-visible text content as a control signal, status marker, or message discriminator.
- Do not branch on terminal/output payload strings such as `includes(...)`, `startsWith(...)`, or tag-like text markers to decide event meaning.
- Control flow must use explicit structured fields or separate events, for example `message_type`, `kind`, `code`, or distinct socket event names.
- Terminal stream data is opaque display payload and must not be parsed for app-level state decisions unless the protocol explicitly defines that payload format.
- If an error must be shown in UI, send it through a typed error channel or attach explicit metadata; do not rely on printable text prefixes like `[ERROR]`, `[WARN]`, or similar markers.

## Review Check

- When reviewing frontend/backend messaging, check whether any branch depends on display text instead of typed metadata.
- If normal user content can collide with a control marker, treat it as a bug and redesign the message contract.

## MIBCRK-Only Planning

- Agent collaboration plans and other MIBCRK deployment notes must not be committed to the GitHub WebSSH repo.
- Put those files under `/mnt/d/workspace/MIBCRK/Tools/webssh/` and commit them in the MIBCRK `Tools` git repo when they need version control.
