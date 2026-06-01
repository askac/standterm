# Backend Plugin Contract

This document describes the StandTerm backend plugin contract that is exposed
through `terminal_policy.connection_options`. It is intended for maintainers and
plugin authors. User-facing terminal behavior is summarized in `README.md`.

## Policy Shape

Every terminal backend is registered as a `TerminalBackendPlugin`. The registry
calls each plugin with a `BackendPolicyContext` and publishes one policy option
per backend:

```json
{
  "connection_type": "ssh",
  "label": "SSH",
  "allowed": true,
  "start_fields": []
}
```

`connection_type` is a normalized identifier such as `ssh`, `local_shell`, or
`uart`. `allowed` is the current client-side availability result after local
access and browser authorization checks. Plugins may expose additional
connection-specific metadata, but new start form metadata should be declared in
`start_fields`.

## Start Fields

Plugins declare start form inputs by returning `BackendStartFieldSchema` values
from `get_start_form_schema(context=...)`. The registry normalizes the schema
before the frontend receives it. Unknown keys, invalid field names, invalid
types, duplicate names, duplicate select values, and type-incompatible defaults
fail fast while building terminal policy.

A start field may contain:

| Key | Required | Notes |
| --- | --- | --- |
| `name` | yes | Lowercase identifier: `^[a-z][a-z0-9_]*$`. |
| `value_type` | yes | `boolean`, `integer`, `number`, `string`, or `enum`. |
| `input_type` | yes | `checkbox`, `number`, `password`, `select`, or `text`. |
| `label` | no | Non-empty display label. |
| `default_value` | no | Must match `value_type`; omitted for secret fields. |
| `required` | no | Boolean; defaults to `false`. |
| `secret` | no | Boolean; defaults to `false`. Secret fields never expose defaults. |
| `options` | for select/enum | List of `{ "value": ..., "label": "..." }`; labels are optional. |
| `min_value` / `max_value` | no | Must match `value_type`; intended for comparable limits. |
| `max_length` / `max_bytes` | no | Positive integer limits for text-like payloads. |

Input and value types must be compatible:

- `password` fields must be `string`.
- `checkbox` fields must be `boolean`.
- `number` fields must be `integer` or `number`.
- `select` inputs and `enum` values must declare non-empty `options`.

The frontend uses these fields as metadata for the existing SSH, Local Shell,
and UART controls. It is not a generic dynamic form renderer yet. Existing
controls prefer `start_fields` defaults and options, and keep legacy policy
fallbacks for compatibility with older servers and consumers.

## Current Built-In Fields

SSH declares:

- `host`: required string text field.
- `port`: required integer text field, constrained to `1..65535`.
- `username`: required string text field.
- `password`: optional secret string password field with no exposed default.

Local Shell declares `local_shell_kind` as a WSL-only enum select field. Native
Windows, Linux, and macOS do not expose a start field for Local Shell because the
host shell is chosen by the launcher or process environment.

UART declares:

- `serial_port`: required string text field.
- `baud_rate`: required integer select field.

UART detected port listing is still carried by the legacy `available_ports`
policy key because the current UI uses a datalist-style text input for manual
or detected serial devices.

## Runtime Defaults

The current runtime-mutable low-risk defaults are:

- `default_connection_type`
- `ssh.default_host`
- `ssh.default_port`
- `ssh.default_user`
- `local_shell.default_kind` on WSL
- `uart.default_baud_rate`

Runtime setting updates go through the settings update path: version/CAS check,
schema digest check, plugin validation, settings snapshot rebuild, audit, and
policy refresh. The effective settings snapshot is passed into
`BackendPolicyContext.settings_snapshot`, so start field defaults and omitted
start payload fields resolve to the same runtime values.

These settings are in-memory runtime state only. Persistence across StandTerm
server restarts has not been added.

## Settings Policy

The browser receives typed settings state through the `settings_snapshot`
Socket.IO event. A snapshot includes:

- `settings_version`: current runtime settings version.
- `settings_schema_digest`: digest of the declared settings schema.
- `settings_schema`: core and plugin-declared setting metadata.
- `mutable_settings`: the current low-risk runtime settings that may be updated.
- `capabilities`: read/update capability state for the connected browser.
- `scoped_settings_admin_grant`: scoped low-risk update grant state, if any.

The frontend sends `settings_update_request` with:

- `setting_key`
- `value`
- `expected_version`
- `expected_schema_digest`

The server rejects stale writes with typed conflicts. A mismatched schema digest
returns `settings_schema_conflict`; a stale runtime version returns
`settings_version_conflict`. Successful updates return `settings_update_result`
and trigger a refreshed settings snapshot and terminal policy.

Settings view is available to local or browser-authorized clients. Low-risk
runtime updates require local access or a scoped `settings_update_low_risk`
admin grant. High-risk plugin settings are declared in schema for visibility,
but the current UI does not expose high-risk mutable updates.

## Compatibility Notes

Keep these compatibility surfaces unless there is an explicit migration plan:

- The Socket.IO event for starting any terminal backend remains `start_ssh`.
  The name is legacy, but the payload includes `connection_type` and is routed
  through the selected backend plugin.
- The frontend still supports legacy policy keys:
  - UART: `available_ports`, `baud_rates`, `default_baud_rate`
  - Local Shell: `shell_options`, `default_shell_kind`
- Do not branch on browser-visible text or terminal display payloads. Control
  decisions must use typed policy, settings, Socket.IO, or external-agent JSON
  fields.
- Secret start fields must not expose `default_value`.

When adding a backend, implement `build_policy_option()`,
`get_start_form_schema()`, `validate_start_payload()`, `create_bridge()`, and
`connect_bridge()`. Add focused backend and browser smoke tests for the exposed
policy shape and the start payload validation behavior.
