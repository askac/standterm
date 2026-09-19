# Agent reliability, UI copy, and localization plan

## Scope and decisions

Complete reliability fixes before changing UI copy or introducing localization.
The traverse brief is review input, not a requirement to expand public discovery.

| Decision | Resolution |
| --- | --- |
| Tokenless agentinfo | Keep the limited bootstrap and handoff index. Do not expose all ungranted terminals or add localization metadata. |
| Omitted terminal | Preserve latest-handoff compatibility. Explicit terminal and token arguments remain authoritative. |
| Capture timeout | `wait_ms` bounds the entire capture after the write, including settling. Retain the completed write and captured events on timeout; never resend automatically. |
| Primary authorization default | Preserve the saved permission and current Direct input default. Copy changes do not change authorization. |
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
| 5 | Pilot Agent access and local connection information | Implemented; validation below | Cover static and dynamic text, titles, and accessible names; preserve connected sessions and authorization; support English fallback. |
| 6 | Translate and integrate approved rows | Agent pilot, browser access/recovery, Agent approvals/transfers, connection/login controls, SSH route/profile editors, settings transfer/preference actions, user SSH tunnels and Agent Tunnel integrated | AI edits only target-language cells; validate keys and placeholders; review authorization and destructive-action wording. |
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
were not rerun for these backend/helper changes. The subsequent UI pilot is
validated separately below.

## Deferred UI work

Agent diagnostics, remaining settings panels, secondary windows,
and Desktop localization remain outside the completed workflows. Translation
starts only after the English source for the selected workflow is reviewed.
Extra screen diffing, render hints, key aliases, and byte-limit options remain optional optimizations.
Unverified UART end-to-end coverage is a qualification gap, not evidence that
UART is broken.

## Copy review and AI translation exchange

The adjacent `ui_copy_review.tsv` is the editable source for the pilot catalog and
future copy review; it does not inventory the entire product. `en` contains the
reviewed English source; `zh-TW` holds its translation when available.
`current_en` records the English review baseline, with
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

## Pilot implementation and translation handoff

The initial pilot contained 82 rows: 81 reviewed translations and one removal row.
It integrated 75 keys; six reviewed keys were reserved for the next phase.
An independent translator edited only the 63 new target cells. The integrator
checked unchanged metadata and placeholders, clarified that Settings changes
the default permission, and reviewed the translations before changing statuses.

Generate the checked-in browser catalog with
`python scripts/build_ui_messages.py`; use `--check` to reject a stale catalog.
The standard headless checks validate the table and generated output. CI also
runs `node tests/ui_i18n_smoke.cjs` for lookup, interpolation and safe DOM output.
English source rows require `source-approved` or `translation-reviewed`; target
text is exported only from `translation-reviewed` rows. Empty translations use
English. Removed rows are excluded. Runtime needs only the generated assets;
no new build tool or third-party localization dependency is required. Core
packaging includes the source table and both runtime scripts.

To outsource another translation batch, send the table and agreed terminology
above. Ask the translator to edit only target cells in the selected source-approved
rows and return the same UTF-8 TSV. Compare all other cells and the complete key
set against the sent copy, review the meaning, then update statuses and regenerate.
Never accept source or metadata changes merely because placeholder checks pass.

The local `uiLanguage` preference supports `en` and `zh-TW`; unknown values
fall back to English. Save applies on the next page opening. The active page
retains its locale and its existing terminal connections, permissions and tokens.
Machine prompts, protocol values and backend diagnostics are unchanged.

| Pilot check | Result |
| --- | --- |
| Complete Agent browser smoke suite | 51 cases passed, including the new Traditional Chinese authorization, connection-info and next-opening language case. |
| Active session preservation | Saving a language choice retained the socket, terminal session, permission and token; it emitted no authorization or connection mutation. A new page applied the saved language. |
| Table/exporter tests | Six cases passed; the checked-in catalog also passed `--check`. |
| Translation lookup tests | Six cases passed for fallback, literal interpolation, safe display attributes and browser loading. |
| Integrated key audit | All 75 keys have English and reviewed Traditional Chinese text. |
| Bilingual connection-dialog layout | All controls fit at 600, 800 and 1280 pixel viewport widths. |
| Core bundle selection | Three focused tests passed, including required catalog assets and source table. |
| Independent read-only review | No correctness or security findings in permission control, language persistence, interpolation or fallback. |

These checks ran with WSL Chromium and local test servers. Native Desktop
packaging and physical UART qualification were not part of this UI pilot.

## Browser access, recovery, approval and transfer implementation

The accepted [phase-two proposal](ui_copy_phase2_proposal.md) is implemented.
The authoritative table now contains 170 rows: 169 reviewed messages and one
decorative removal. All 169 exported keys are referenced by the integrated UI.
The companion proposal TSV remains a historical snapshot; do not export it to
the runtime catalog or append its duplicate keys to the authoritative table.

Browser authorization, the separate initial access page, recovery prompts and
manual authorization help now share reviewed English and Traditional Chinese
copy. Pending, rejected, reconnect and reset states use the same catalog.
Initial access retains its English HTML form when JavaScript or translation
assets are unavailable. Known recovery errors use typed error codes; unknown
server diagnostics retain their original messages. Credentials, auth routes,
recovery eligibility and public discovery scope are unchanged.

Agent approval distinguishes input from file copies. Reject handles one proposal;
Pause Agent retains its terminal scope. Transfer cancellation distinguishes a
pending request from an operator-confirmed stop and stays disabled after the
commit barrier. Dismiss actions hide entries without issuing file operations.
Canonical endpoints, paths, sizes, proposal bindings and raw diagnostic codes
remain intact.

Independent read-only implementation reviews found no control or authorization
regressions. A subsequent browser layout check found that existing single-line
ellipsis hid the end of unknown-publication warnings, despite the full strings
being present in the DOM. Only that typed error now enables wrapping in Agent
details and the transfer queue. The critic accepted this correction and clarified
that its earlier review established text content, not visual completeness.

| Check | Result |
| --- | --- |
| Complete Agent browser suite | 56 top-level cases passed, including English/Traditional Chinese access and recovery, exact input decisions, file-copy plans, stop states and dismissal. |
| Complete Agent backend suite | 169 cases passed, including browser grants, recovery eligibility, stale proposals and transfer commit/cancel boundaries. |
| Final warning-layout correction | Five focused approval/transfer cases passed after the CSS correction; both locales preserve full warning text at 1280 and 480 pixel widths. |
| Initial access fallback | Real token login passed with JavaScript disabled and with both translation assets blocked; invalid Agent-style token input remained rejected. |
| Browser URL validation | Native invalid-URL validation stays in place; parser/scheme/missing-grant branches preserve their behavior. A syntactically valid authorization URL only navigates, without claiming authorization succeeded. |
| Catalog and lookup | Six exporter tests and six lookup tests passed; generated catalog freshness and all 169 referenced bilingual keys verified. |
| Static verification | Python compilation and diff whitespace checks passed. |

The browser suite ran before the final warning-only layout correction; the five
focused cases above validate that final correction. Native Desktop packaging,
real platform authenticator prompts and physical UART were not requalified.
Core packaging already requires both runtime catalog files and the source table;
this phase adds no new runtime asset or dependency.

## Connection forms and SSH login implementation

The connection form, direct/history picker, saved-route summary, per-node SSH
login, direct browser-key controls and direct host-fingerprint controls now use
the same reviewed catalog. The table contains 274 rows: 273 bilingual messages
and the existing decorative removal. All exported keys have source references.
`Save session` now reads `Save connection profile`; this saves reusable settings,
not the terminal's execution state. The Local Shell tooltip identifies the
StandTerm host as the execution host. TCP port and UART port remain distinct.

SSH `authenticated`, `shell` and `complete` display `Authenticated`, `Opening
terminal` and `Complete`. Intermediate nodes can complete without opening their
own terminal. Schema defaults, manually edited fields, backend shell/serial option
labels, raw endpoints, key references, fingerprints and server diagnostics remain
unchanged. Dynamic modules take an optional translator; callers outside this
phase retain English fallback. The full route/profile editors, tunnel setup,
Desktop UI and general backend diagnostics remain deferred.

The independent adversarial review completed two bounded passes. It identified
implementation constraints rather than existing runtime defects; the final
source review found no material regression.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| SSH phase labels also validate incoming enum values | Medium guard | `standterm-ssh-login.js` labels and handle | Preserve all enum keys; translate only values | Accept | All 12 phase keys and target authenticated-to-shell mapping remain intact | Bilingual phase/prompt tests and real three-hop login |
| Optional translation must distinguish lookup parameters from fallback copy | Medium guard | `standterm-i18n.js` lookup contract and three SSH factories | Use a thin local fallback wrapper | Accept | Inject `translate(key, params)`; interpolate fallback once only when no key resolves | Missing/absent translator and literal-placeholder tests |
| Longer wording could clarify Saved routes | Optional wording | Route heading and surrounding SSH form | Consider a longer connection-route label | Keep the shorter label in the existing SSH context | Critic accepted the scoped wording | Desktop/narrow-width label fit checks |

The review added explicit fallback and enum coverage. It did not expand public
agentinfo or change permission policy, connection routing, default cancel focus,
or retry behavior. Full editor localization is deferred until its complete
editing/saving workflow receives a separate copy review. No policy question is
outstanding for this phase.

| Check | Result |
| --- | --- |
| Complete Agent browser suite | 56 top-level cases passed, including localized browser access, approvals, transfers, schema controls, credential redaction and host-key action binding. |
| SSH preparation browser suite | Nine cases passed, including atomic temporary keys, saved per-hop authentication, fingerprint management, background login and history retention. |
| SSH login browser suite | Five cases passed, including real three-hop password login, English/Traditional Chinese stale and background prompts, exact host-key/password replies, all phase enums and missing/absent translation fallback. |
| Connection localization browser suite | Three cases passed: bilingual exact SSH payloads, schema/edited values, raw shell/serial labels, key references, literal placeholders, and stale fingerprint-action binding. |
| Connection layout | English and Traditional Chinese form headings, actions and save-profile labels fit at 1280 and 480 pixel widths. |
| Catalog | Six exporter and six lookup tests passed; generated catalog freshness and all 273 referenced bilingual keys verified. |

Validation ran in WSL Chromium. Native Desktop, physical UART and complete
route/profile editor localization were not qualified by this phase. The next
candidate is a separate review of those editors' naming, saving and key-retention
copy before expanding their translation coverage.

## SSH route and profile editors

The route editor and Settings SSH profiles now share the reviewed catalog,
including inline node fields, browser-key controls, host identity, edit scope,
repair suggestions, deletion confirmations and save feedback. The table contains
373 rows: 372 referenced bilingual messages and the existing decorative removal.
`SSH Sessions` and `New session` now read `SSH profiles` and `New profile` so saved
connection settings are not confused with running terminals.

Prepare-mode **Done** returns an edited draft. Its **Save route on Connect**
checkbox requests persistence at Connect. Manage-mode **Save route** persists
immediately and changes only future connections. Existing repair/reorder guidance
incorrectly named Done in both modes; the copy now uses the actual mode's action.
The advanced reference/copy controls explain that they replace the selected
node's following route, and copied nodes retain their credential references.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Management repair guidance names a nonexistent Done button | Medium | Route editor mode-specific action and three hardcoded instructions | Resolve the action label from typed mode | Accept | Keep Save route and Done, inserting the correct label in repair/reorder/append guidance | Route regression and bilingual editor checks |
| Shared edits, route order and tail replacement have different scope | Medium guard | replaceDraftNode, moveCard and copyPath | State tail replacement and retained key references separately | Accept | Add adjacent reference/copy help; preserve shared-edit notice and entry-only reorder semantics | Shared-node, copy/reference and stored-node regressions |
| Cancel must not imply discarding an existing parent draft or its keys | Medium guard | Editor clones incoming draft and closes without saving | Keep Cancel scoped to this dialog | Accept | Preserve simple Cancel label and parent draft behavior | Preparation and inline-to-route cancellation tests |
| Deleting a profile does not always delete its keys | Medium guard | Owner-key branch versus independent credential records | Keep separate confirmation messages and test actual key outcomes | Modify | Preserve both confirmations; reuse existing legacy-key deletion test and add credential-retention coverage | Legacy-key lifecycle and bilingual deletion checks |
| Referenced legacy owner deletion lacks an explicit blocked-deletion regression in the reviewed files | Low, existing gap | requireUnreferencedSshKey and targeted test inventory | Add owner/receiver/history fixture | Defer | Runtime reference guard remains unchanged; add coverage when changing legacy key deletion or rebinding | Source review only for this branch |

Two bounded independent review passes found no remaining material regression.
The reviewer independently checked placeholders and missing-translation fallback.
Raw role markers, entry/node IDs, scope values, references, endpoints, credentials,
unknown model/backend errors and persistence decisions remain unchanged. Existing
model/storage diagnostics may remain English; this phase does not infer error
codes from diagnostic text. Settings outside SSH profiles, Desktop, tunnels and
secondary windows remain outside the completed localization scope.

| Check | Result |
| --- | --- |
| Route editor browser suite | Seven cases passed, including shared references, copy/reorder, cycle repair, migration and trust retry binding. |
| Profile context browser suite | Six cases passed, including changed terminal context, atomic save failure, inline promotion and stale storage revisions. |
| Preparation browser suite | Nine cases passed, including parent drafts, temporary keys, cancellation and background history. |
| Existing profile/key regressions | Two cases passed, including settings save semantics and actual legacy private-key deletion. |
| Bilingual editor suite | Four cases passed in both languages: prepare/cancel persistence boundaries, managed stale-save rejection, entry/all with copy/reference, exact delete/clear scope and independent credential retention. Cycle repair in both modes uses the actual completion label and does not call onDone before final confirmation. |
| Layout | English/Traditional Chinese action labels and consequence text fit at 1280 and 480 pixel widths. |
| Catalog | Six exporter and six lookup tests passed; all 372 exported keys are referenced and the generated catalog is current. |

Validation uses WSL Chromium. Native Desktop and physical UART were not part of
this phase. The next candidate is a separate copy review of settings import/export
and its replacement/key-retention consequences before expanding that workflow.

## Settings import and export

Settings import/export now uses reviewed English and Traditional Chinese copy.
The table contains 395 rows: 394 referenced bilingual messages and the existing
removal. The confirmation identifies the normalized input counts, supplied
preference/layout replacement, added SSH profiles, existing-first history limit,
key reselection and page reload. Reload stops this page's temporary SSH tunnels;
the copy does not promise uninterrupted terminal access or say all terminals end.

The review also corrected README's obsolete stable-ID merge and history-deduplication
claims. Imported profiles/nodes receive new IDs, while existing profiles and key
bindings remain intact. History appends after existing entries and keeps the first
six. Export excludes key material and references; imported browser-key routes
require key selection again. The export status reports that download started,
without claiming that the browser saved the file to disk.

A preference write can fail after SSH data is committed. A small error wrapper
now identifies this partial completion and preserves the original diagnostic as
plain text. It does not roll back, retry, reload, or change storage order. The
existing best-effort Agent panel position writer still suppresses its own storage
errors; this phase does not make the entire import atomic across storage systems.
Unknown JSON, Base64 and route-model errors remain original diagnostics.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Preference failure can follow a successful SSH commit | Medium, existing limitation | importBrowserSettingsText and savePrefs | Explain partial completion and avoid automatic retry advice | Accept with a stage-specific wrapper | Wrap only post-SSH browser writes; retain raw detail and original error timing | Injected preference quota failure preserves committed SSH data, keys and prior preference storage, with no reload or second import |
| Confirmation omits preference/layout replacement and reload | Medium | Import pipeline and normalized UI fields | Describe the effects before writes | Accept | Add replacement, reload and temporary-tunnel consequences | Exact bilingual confirmation, cancellation and real navigation checks |
| File counts do not guarantee retained history; profiles are added with new IDs | Medium | importState and history slice | Describe counts as input and state existing-first limit | Accept | Correct confirmation and README; retain algorithm | Import seven history entries into two existing entries and verify first-six order, plus new profile/node IDs |
| Excluding keys also removes imported key bindings | Low | exportState and importState | Explain key reselection without changing local keys | Accept | State excluded bindings and required selection | Real downloaded ZIP contains no key material; existing key records/links survive import |

Two bounded independent passes found no remaining material issue. The critic
verified placeholder consistency, literal diagnostic interpolation and the
successful/failed write order using the actual functions and catalog in memory.
Cross-storage rollback remains deferred unless atomic import is explicitly
required or observed failures justify a separate recovery design.

| Check | Result |
| --- | --- |
| New settings-transfer browser suite | Four cases passed in English and Traditional Chinese: actual download, cancel, successful merge/reload and validation/partial-failure boundaries. |
| Existing focused browser regressions | Two cases passed: settings/key lifecycle and language preference behavior. |
| Invalid input and fallbacks | JSON/model diagnostics stay raw; unsupported envelopes/payloads, archive size/checksum and oversized files reject before confirmation. Empty-message file-read/export failures use localized fallback. |
| Layout | On-page help, actions and partial-failure text fit at 1280 and 480 pixel widths in both languages, including long literal diagnostic text. Native browser confirmation chrome was not visually qualified. |
| Catalog and static checks | Six exporter and six lookup tests passed; catalog freshness, all 394 referenced bilingual keys, Python syntax and diff checks passed. |

The next small candidate is reviewing reset-to-defaults and general settings
navigation, especially what reset changes and what its reload stops. Desktop,
remaining settings panels and full runtime diagnostic localization remain outside
this completed workflow.

## Settings navigation and preference actions

Settings navigation, heading, close accessible name, preference actions and their
scope hints now use reviewed English and Traditional Chinese. The table contains
406 rows: 405 bilingual messages and the existing removal. The language-preview
hint is shorter; README retains the detailed coverage list. The appearance
preview note matches the renamed Save preferences button.

Save preferences reads the General and Appearance fields and applies terminal
appearance without reloading. Close only hides the modal; reopening repopulates
these fields from saved preferences. SSH profile actions, immediate history
preferences, imports and Server actions keep their own persistence rules.
Reset preferences immediately restores every PREF_DEFAULTS entry and reloads,
including language, history-saving preference and default Agent permission.
Changing the default permission does not itself grant Agent access. Reload
loses unsaved drafts and stops this page's temporary SSH tunnels. Saved SSH
profiles, history, keys and Agent panel position stay stored.

The footer explains both actions on every tab, with separate accessible
descriptions. Its hints stay outside the scrolling tab content. No handlers,
typed tab IDs, storage order, authorization rules or confirmation flows changed.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Save and Close do not commit or undo every settings operation | Medium | Preference handlers, independent SSH/Server actions and immediate history toggle | Name preference scope; avoid global save/cancel claims | Accept | Use Save preferences and Close settings; describe separate actions | Bilingual draft/close/save checks plus existing SSH profile save-semantics regression |
| Reset is broader than Save and discards unsaved drafts | Medium | PREF_DEFAULTS, reset handler and independent SSH key/profile storage | Distinguish all browser preferences, saved data and drafts | Accept | State immediate defaults/reload, draft loss and saved SSH retention | Real reload restores complete platform defaults; exact SSH/history/key and unrelated storage retention |
| Consequences must remain visible beside Reset | Low | Existing modal scrolling boundary and disconnect tunnel cleanup | Persistent hints, accessible descriptions and narrow/short-window checks | Accept | Nonshrinking footer with wrapping action row; explicitly scope tunnels to this page | Both locales, all five tabs, content scroll endpoints at 1280x800, 480x800 and 480x600 |
| Additional Reset confirmation | Optional UX policy | Reset already executes immediately | Alternative confirmation can reduce accidental activation | Defer | Preserve workflow and provide visible consequences | Reconsider if accidental resets are reported or confirmation is requested |

Two bounded independent review passes found no remaining material issue. The
second pass suggested naming this page's tunnels explicitly; that clarification
was adopted. The tests exercise actual preference storage and reload, not only
matching translated labels. Existing non-atomic storage behavior is unchanged.

| Check | Result |
| --- | --- |
| New preference browser suite | Three cases passed in both languages: draft/close/save and next-page locale, complete reset/reload boundaries, and navigation/footer layout. |
| Existing focused regressions | Three cases passed: SSH profile save semantics, key/settings transfer lifecycle, and language behavior with existing access. |
| Catalog | Six exporter and six lookup tests passed; all 405 bilingual keys are referenced and the generated catalog is current. |
| Static checks | Python syntax and diff whitespace checks passed. |

Validation uses WSL Chromium; native Desktop and operating-system dialogs were
not part of this phase. Remaining General/Appearance fields, Server controls and
Diagnostics content still have English copy. The next candidate is a separate
review of SSH tunnel setup/stop wording and target scope before localization.

## User SSH tunnel controls

This phase covers the browser's ordinary TCP tunnel dialog, separate from the
Agent Tunnel authorization workflow. Its copy names the listener side and the
side that reaches the target. Core host means where Core runs, including WSL;
SSH remote means the final SSH connection in the selected route. Remote rows
identify the requested listener, without claiming verification of the server's
actual interfaces. Traffic counters use the target as their reference point.

Request timeouts leave the outcome unconfirmed. Status polling may resume, but
start and stop operations are not automatically resent. A stop acknowledgment
does not prove that the SSH server has removed its listener: cancellation is
asynchronous, and cleanup_pending only tracks unfinished setup. Closing the
dialog keeps tunnels running; closing SSH or disconnecting the creating page,
including a reload, stops them.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Remote row can imply a verified loopback interface | Medium | renderSshTunnel; request_remote; remote peer rejection | Label the remote listener as requested | Accept | Preserve the actual-interface qualification beside the form and in the row | Real SSH non-loopback peer rejection; bilingual row/bind checks |
| Timeout and stop text can overstate the result | Medium | Shared request timer; asynchronous cancel_remote; snapshot cleanup_pending | Explain an unconfirmed timeout and requested stop | Accept | Preserve typed state, cleanup flag, request correlation and no mutation replay | Timeout/late acknowledgment and status rendering checks |
| Close can be mistaken for stopping a tunnel | Medium | Dialog close handler versus close_bridge/on_disconnect | State that closing the dialog keeps tunnels running | Accept | Keep separate Close and Stop actions without a new confirmation | Close/reopen retention and existing viewer-disconnect tests |
| Sent/received lacks a stable reference point | Low | relay_tcp(channel, target) and _progress | Use To target and From target | Accept | Keep byte units and raw counts; translate only their description | Both forwarding directions and real SSH byte-count tests |

The backend, viewer ownership, transport binding, request/revision checks and
loopback policy remain unchanged. Localized text never selects an operation,
terminal or tunnel. Server diagnostics and user-supplied names/addresses stay
literal. Additional interface inspection and Agent Tunnel localization remain
outside this phase.

The table contains 445 rows: 444 referenced bilingual messages and the existing
removal. Thirty-nine reviewed rows cover static labels, direction-dependent
help, status names and request feedback. Input labels use text spans so applying
translations preserves controls and their values. Unknown status values render
as Unknown; only typed starting/listening values enable Stop. Empty-list copy
avoids claiming that Agent Tunnel or another viewer has no tunnels.

Two bounded independent review passes found no remaining material issue. The
second pass verified literal interpolation and unknown-status fallbacks in
memory. Its optional README clarification was adopted: Stop ends forwarding
and connections without claiming confirmed remote listener removal. Additional
listener inspection stays deferred unless verified interface reporting becomes
a requirement; no policy choice blocks this copy change.

| Check | Result |
| --- | --- |
| Existing real SSH suite | All 12 tests passed, including both directions/byte counters, three-hop final transport, independent Agent/user tunnels, ownership, disconnect cleanup and non-loopback rejection. |
| Bilingual browser suite | Three cases in each language passed: existing stale-view/request/revision guards, exact direction payloads and typed states, and timeout with continued status polling but no automatic start replay. |
| Display boundaries | Raw names, errors and IPv6 endpoint values remain literal; direction changes retain form controls/values, and Close sends no Stop. |
| Layout | Both languages fit the 480x600 dialog with input/select bounds retained and no horizontal overflow. |
| Catalog and static checks | Six exporter and six lookup tests passed; all 444 bilingual keys are referenced, the generated catalog is current, Python syntax and diff checks passed. |

Validation uses WSL Chromium and local SSH servers. Native Desktop and a physical
remote host were not part of this phase. Agent Tunnel authorization/renewal and
remote verification copy are the next separate review candidate; general TCP
forwarding does not replace that workflow or grant API access.

## Agent Tunnel access and verification

Twenty-nine reviewed messages now cover Agent Tunnel setup, target permissions,
verification and lifecycle feedback. The table contains 474 rows: 473 referenced
bilingual messages and the existing removal. Existing connection/copy labels
are reused. The remote prompt, URL, SSH context fields and backend diagnostics
remain unchanged data; only their surrounding display text is localized.

On a new tunnel, Start installs helpers and skills in a private temporary SSH
directory and verifies the remote path. On an existing active tunnel, apply
updates grants without repeating those checks. Valid tokens keep their expiry;
invalid grants can be replaced. The UI therefore says Preparing Agent access
and labels verified_at as the last verification, using the selected UI locale.

Check verifies the loopback-only remote listener, helper bundle and expected
Core instance. Failure stops this tunnel and revokes its grants. This consequence
is visible beside the controls and attached to Check as an accessible
description. Readiness does not prove an authenticated Agent request: activity
and zero-grant states remain separate. Stop revokes this tunnel's access before
asynchronous cleanup; it does not revoke local agents or guarantee deletion of
all remote files. Close leaves the tunnel running.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Apply does not always verify or extend token expiry | Medium | on_agent_tunnel and update_agent_tunnel_targets | Remove verification promise; explain renewal scope | Accept | Preparing message and valid-token expiry hint; retain Start / Renew Access label | Actual apply preserves valid tokens, expiry and verified_at; replaces only expired grant without executing verification commands |
| Check failure has a revocation consequence | Medium | Runtime verify and handler exception cleanup | State failure stops/revokes and timestamp is historical | Accept | Visible Check hint and localized last-verification label | Existing missing-listener and activity tests; bilingual failed-check controls and timestamps |
| Stop and remote cleanup have different guarantees | Medium | AgentTunnel.close and best-effort cleanup | Scope revocation to this tunnel; avoid complete-deletion claims | Accept | Scoped cleanup message and lifecycle hint; retain Close behavior | Slow cleanup revokes immediately; browser stopped/pending states and Close without Stop |
| Target list is a status snapshot and ready can have no grants | Low | renderAgentTunnelTargets and backend panel-target synchronization | Explain refresh and separate readiness from Agent activity | Accept | Localized known permissions, raw unknown mode fallback, remote-access inclusion label | Bilingual mode display preserves actual permissions, zero-grant readiness and raw data; existing enrollment/ownership tests |
| Copy prompt terminology | Editorial | Previously agreed glossary and shared connection labels | Alternative translation for prompt | Modify | Reuse the established connection-instructions wording | Existing raw prompt and clipboard checks in both locales |

Protocol values, request/connection/carrier guards, public discovery scope,
authorization defaults and backend behavior remain unchanged. Separate local
token creation is not required. Later enabled tabs still follow the same
viewer's Agent Panel permissions, and Refresh does not silently renew access.

Two bounded independent review passes found no remaining material issue. The
review changed the apply/verification wording and added explicit Check-failure
and Stop consequences. It also verified literal interpolation, English fallback
and all 29 new key references in memory. Keeping backend diagnostics raw and
the target list as a refreshed snapshot is a deliberate scope decision; no
automatic retry, broader discovery or continuous synchronization was added.

| Check | Result |
| --- | --- |
| Existing Agent Tunnel backend suite | All 26 tests passed, including real SSH, grant ownership/enrollment, target publication, loopback verification, failed checks, revocation and slow cleanup. |
| Renewal boundary regression | One additional real-SSH case passed: valid tokens/expiry and verification time persist across apply; only the expired grant is replaced. |
| Bilingual browser suite | Three cases in each language passed, retaining focus/clipboard checks and late-carrier/disconnect protection, and adding permission display, zero grants, raw data, stopped/cleanup and Close boundaries. |
| Layout | Both languages fit the 480x600 dialog without horizontal overflow; displayed controls remain inside it. |
| Catalog and static checks | Six exporter and six lookup tests passed; all 473 keys are referenced, the generated catalog is current, Python syntax and diff checks passed. |

Validation uses WSL Chromium and local SSH fixtures; native Desktop and a
physical remote host were not exercised. Typed backend error localization and
continuous remote health monitoring remain outside scope. The next candidate
is a compact review of remaining General/Appearance settings labels before
considering broader Server, Diagnostics or Desktop coverage.
