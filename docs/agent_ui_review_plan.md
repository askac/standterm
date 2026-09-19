# Agent reliability, UI copy, and localization plan

## Scope and decisions

Complete reliability fixes before changing UI copy or introducing localization.
The traverse brief is review input, not a requirement to expand public discovery.

| Decision | Resolution |
| --- | --- |
| Tokenless agentinfo | Keep the limited bootstrap and handoff index. Do not expose all ungranted terminals or add localization metadata. |
| Omitted terminal | Preserve latest-handoff compatibility. Explicit terminal and token arguments remain authoritative. |
| Capture timeout | `wait_ms` bounds the entire capture after the write, including settling. Retain the completed write and captured events on timeout; never resend automatically. |
| Agent Mint default | Preserve the saved permission and current Full default. Copy changes do not change authorization. |
| Display and control | Localized text is display data. Keep protocol values, error codes, terminal IDs, and control decisions independent of wording. |

## Agreed terminology

The operator confirmed the following terminology. Keep these distinctions in
both the English source and Traditional Chinese translations.

| Concept | English UI wording | Traditional Chinese (Taiwan) | Meaning and boundary |
| --- | --- | --- | --- |
| Agent | Agent | Agent | Retain the name; avoid confusing the terminal agent with a network proxy. |
| Grant access | Authorize agent | 授權 Agent | The operator permits Agent access to the selected terminal. The primary action also applies the saved permission and creates a token; identify the permission and target in nearby text or the tooltip. |
| Allowed operations | Permission | 權限 | Determines what an authorized Agent may do. |
| Connection credential | Token | 權杖 | Presented by the Agent when connecting. Expiry does not necessarily reset the selected permission. |
| Create a credential | Create token | 建立權杖 | Creates a token for already enabled access; do not conflate it with changing permission or the primary Authorize agent action. |
| Observe mode | Read only | 唯讀 | Reads terminal information but cannot send terminal input. |
| Approval mode | Approval required | 需核准 | Each input proposal requires human approval before it is sent. |
| Direct mode | Direct input | 直接輸入 | Input does not require individual approval; file copies still require approval and other input gates still apply. |

The protocol values `observe`, `approval_pending`, and `direct_active` remain
unchanged. Permission labels describe behavior, not unrestricted authority.
Pause, Disable, Stop, Cancel, Reject, Close, and Dismiss remain distinct actions;
do not consolidate them merely to shorten labels.

## Work sequence

| Order | Work | State | Exit criteria |
| --- | --- | --- | --- |
| 1 | Preserve explicit CLI and REPL terminal selection | Implemented and validated | Explicit `main` and other IDs survive handoff loading; omitted IDs retain defaults; mismatched tokens cannot write. |
| 2 | Bound capture settling by the total deadline | Implemented and validated | Quiet, continuous, late, and absent output produce bounded results without replaying input; the send result survives a capture timeout. |
| 3 | Review English UI copy and terminology | Core terms agreed; initial English sources reviewed | Review each proposed change for target, permissions, consequences, and next action; check related tooltips, Desktop help, and tests before applying it. |
| 4 | Finalize the translation exchange table | Initial exchange validated with an independent translator | Stable keys, approved English source, context, and placeholder constraints are sufficient for an independent translator. |
| 5 | Pilot localization in one complete Core workflow | Deferred | Cover static and dynamic text, titles, and accessible names; preserve connected sessions and authorization; support English fallback. |
| 6 | Translate and integrate approved rows | Initial translations reviewed; runtime integration deferred | AI edits only target-language cells; validate keys and placeholders; review authorization and destructive-action wording. |
| 7 | Expand Core and Desktop coverage | Deferred | Verify secondary windows, native menus, setup, diagnostics, packaging, and representative layouts. |

## Validation of the reliability changes

The new explicit-terminal regression failed before the CLI/REPL fix. The new
continuous-output regression failed before the capture deadline fix.

| Check | Result |
| --- | --- |
| Complete backend smoke suite | 169 checks passed. |
| Complete CLI/helper smoke suite | 56 checks passed. |
| Independent read-only diff review | No correctness or regression findings; separate in-memory checks covered deadline boundaries and explicit overrides. |
| Initial copy-review table | 19 unique keys; valid columns and placeholders. |
| Translation exchange | 18 translations reviewed; one removal row excluded. All translator edits were confined to the target column; keys, placeholders, and agreed terminology were verified before the reviewer updated statuses. |
| Diff whitespace check | Passed. |

These checks ran in WSL. Browser, Desktop packaging, and physical UART checks
were not rerun for these backend/helper changes. No runtime UI wording changed.

## Deferred UI work

Stages 3 and 4 do not change runtime UI text. Translation does not start until
the English source for the selected workflow is reviewed. Extra screen diffing,
render hints, key aliases, and byte-limit options remain optional optimizations.
Unverified UART end-to-end coverage is a qualification gap, not evidence that
UART is broken.

## Copy review and AI translation exchange

The adjacent `ui_copy_review.tsv` is an initial review table, not a complete
catalog or a runtime resource. `en` contains the reviewed English source;
`zh-TW` holds its translation when available. `current_en` is the visible
English text, with
dynamic values normalized to the named placeholders declared in the row.
HTML emphasis is omitted from the table. Source references identify the current
implementation and can move as the code changes.

| Column | Ownership and purpose |
| --- | --- |
| `key` | Stable semantic identity, maintained by the implementer; never translate it. |
| `current_en` | Review baseline, not a second runtime source. |
| `en` | English source; becomes translation input only after review. |
| `zh-TW` | Target-language text; the only field an outsourced translation agent edits. |
| `context` | Meaning, audience, and placement. |
| `placeholders` | Named runtime values that must remain unchanged in translation. |
| `constraints` | Scope, safety, terminology, and formatting requirements. |
| `status` | `proposed`, `retain`, or `remove` during copy review; use `source-approved` before translation and `translation-reviewed` after review. |
| `source` | Implementation location for checking behavior and surrounding text. |

Use UTF-8 TSV with one physical line per row. Represent intended line breaks as
literal `\n`; disallow literal tabs or newlines inside cells. Use a proper TSV
reader/writer for spreadsheet exchange. A removal row has an empty `en` cell
and must not be translated. Empty translations fall back to English; they do
not mean that the UI text should be removed.

For translation, export only source-approved rows with their context and
constraints. Keep the key set and all non-target cells unchanged on return.
Source approval records editorial review for translation, not deployment to
the UI. A translation agent fills only the target-language column; the reviewer
updates the status after checking its meaning and formatting.
Validate duplicate/missing keys, named placeholder multiplicity, and unknown
columns before accepting an AI-produced file. Render interpolated values as
text; do not let translations introduce executable markup.

Keep terminal output, commands, paths, fingerprints, protocol enums, and error
codes unchanged. Human-facing labels may be localized independently of the
machine-facing connection prompt. Do not translate an arbitrary backend error
by matching its English message; use a known structured error code or preserve
the original diagnostic as fallback.

## Localization implementation boundary

Use the approved table as the only manually maintained catalog and generate
runtime dictionaries from it. A small exporter/validator and lookup helper are
sufficient for a pilot; a new UI framework or bundler is not required. Keep the
current direct-launch workflow and verify generated resources during packaging.

Store language as a local display preference, not server-global session state.
Do not reload a connected UI merely to change its language. A first version
may apply the choice on the next UI opening; live switching across all open
windows is a separate scope decision.

The pilot must include dynamic states, `title`, `aria-label`, document language,
and a narrow-window check. Keep behavior tests tied to IDs and typed state;
assert localized wording in dedicated presentation checks. Fix the locale of
existing English smoke tests so OS language does not change their results.

The initial estimate is 4-7 engineering days for common Core workflows and
15-25 days cumulatively for full Core plus Desktop. These are preliminary
estimates, excluding the reliability fixes, website and CLI documentation,
remote terminal output, and complete live language switching. A full string
inventory and pilot are needed before committing to a delivery estimate.
