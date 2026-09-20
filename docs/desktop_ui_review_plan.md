# Desktop copy review and localization plan

Review date: 2026-09-20. This phase inventories Desktop copy, reviews behavior
and orders implementation. It does not change runtime behavior or enable a
Desktop language setting. Browser acceptance is recorded separately in
[browser_ui_acceptance.md](browser_ui_acceptance.md).

## Recommended implementation order

| Order | Deliverable | Relative effort | Completion evidence |
| --- | --- | --- | --- |
| 0 | Clarify toolbar action feedback before localization. A resolved `false` currently has no notice; a rejected invocation says to retry despite an uncertain result. | Small | Reproduce both paths; show unavailable feedback for explicit rejection and unknown-result feedback for an exception. Invoke once, with no automatic replay. Preserve successful action notices. |
| 1 | Add a Desktop-owned language preference and catalog; pilot custom menus, toolbar labels and Agent help. | Medium | English default/fallback, `en` and `zh-TW`, malformed preference fallback, next-launch application, translated title/ARIA labels without losing SVGs, fixed command IDs, focus/origin guards and explicit packaged asset inventory. |
| 2 | Review and localize Capture, Browser Access and Diagnostics. Resolve the recording save-failure exit policy before the Capture portion. | Medium | Typed recording states, partial-file paths, cancel/default buttons, first-folder seeding, sensitive clipboard feedback and escaped diagnostic fields retain their contracts. Add combined save-failure plus close/quit coverage. |
| 3 | Localize setup, Core source selection, startup failure and recoverable environment cleanup. | Medium to large | Both languages work before Core is available. Cancellation waits for owned installers; stale confirmations do nothing; source switching, restart/session closure, retained files and recovery moves remain explicit. |
| 4 | Complete Windows and macOS native acceptance and packaged asset checks. | Platform-dependent | Menus, native dialogs, narrow layouts, keyboard/ARIA labels, clipboard, setup and recovery are checked on each OS. Verify staged and packaged Desktop catalogs independently of the selected Core version. |

The smallest next implementation is order 0, as a separate behavioral fix.
Orders 1–3 should remain separate reviewable changes. First-run setup can move
ahead of order 2 if onboarding becomes the priority; it is not required to prove
the small localization pilot.

## Difficulty and design choices

Copy extraction is straightforward. Most work lies in several display contexts:
native Electron menus/dialogs, a restricted toolbar renderer, setup windows and
scriptless diagnostics. A complete Desktop rollout has moderate implementation
cost and broader acceptance cost than the toolbar pilot. The estimates above
are relative scope assessments, not measured delivery times.

Recommend storing the Desktop language in the existing profile's `userData`,
with English as default and only `en` / `zh-TW` initially. Apply a change on the
next launch; changing language should not itself restart StandTerm or stop a
recording. Keep the existing Core browser preference independent. This covers
setup and recovery before Core starts, at the cost of two language preferences.
It is a proposed product choice, not a security requirement. A validated
two-value advisory preference from Core is also feasible, but needs startup,
origin and older-Core fallback rules. Automatic OS-language selection is deferred.

Ship the Desktop catalog with the shell. Do not depend on the selected bundled
or Git Core supplying compatible renderer scripts. Reuse the existing TSV
schema and validation rules; extend generator support minimally when integration
starts. Do not add the Desktop rows to the Core runtime catalog.

Keep these implementation boundaries:

- `UI_ACTIONS` keys, `ui-*` menu IDs, toolbar datasets, IPC action IDs and
  terminal IDs remain structural values. Localize labels only.
- Preserve sender, frame, focus and origin validation. Add only exact bundled
  catalog/helper files to the toolbar asset allowlist, if needed; retain CSP.
- Include new assets in both `stage-windows.cjs` and `electron-builder.cjs`.
  A working source checkout does not prove the installer includes them.
- Update toolbar SVG-button attributes without replacing their child content.
  Native role labels remain platform-owned; review their language alongside
  custom labels during native acceptance rather than promising uniformity.
- Keep diagnostics scriptless and HTML-escaped. In setup, interpolate translated
  text with JSON serialization and `textContent`, following the existing
  progress updater instead of embedding translation text inside JavaScript literals.
- Retain raw diagnostic errors where currently shown, paths and identifiers.
  Browser Access deliberately uses sanitized errors; do not expose its caught
  errors, authorization URLs or tokens through messages or logs.

## Copy and translation exchange

[desktop_ui_copy_review.tsv](desktop_ui_copy_review.tsv) is a prioritized seed
inventory, not a claim that every Desktop string has been extracted. It uses
the same nine columns as the browser table. All initial rows are `proposed`;
`zh-TW` is empty. Some `current_en` cells are exact fragments or normalize
dynamic values to named placeholders; `context` identifies these cases.

Approve the English behavior and terminology before requesting translations.
An external translation AI should return the same keys/order/columns, fill only
`zh-TW`, preserve placeholders and literal identifiers, and keep `status`
unchanged. Human/source review promotes rows to `translation-reviewed`; a
translation response alone is not runtime approval. Use literal `\n` inside
cells, not physical newlines. Do not translate paths, URLs, error codes, Core,
Agent, PNG, WebM or stable action names.

Avoid abbreviating consequences merely to shorten dialogs. Keep restart/session
closure, retained partial files, private clipboard content and the distinction
between opening a link and completing authorization. Remove repetition where
nearby controls already establish the subject. Translate whole messages rather
than concatenating English sentence fragments; close and quit prompts may need
separate keys instead of a grammatical `{action}` fragment.

Existing agreed terms remain in
[agent_ui_review_plan.md](agent_ui_review_plan.md#agreed-terminology). The
following Desktop additions are proposals to settle before translation:

| Concept | English | Proposed Traditional Chinese | Boundary |
| --- | --- | --- | --- |
| Image of the terminal view | Screenshot | 螢幕截圖 | A PNG of the Core view; not CLI output capture. |
| Silent video of the terminal view | Recording | 錄影 | WebM without audio; not a transcript or terminal log. |
| Suspend video recording | Pause recording | 暫停錄影 | Separate from Pause Agent and terminal input control. |
| Stop first-run preparation | Cancel setup | 取消準備 | Stops owned setup processes; prepared files remain. |
| Return to installed Core source | Restore bundled Core | 還原隨附 Core | Not a preferences reset, session recovery or user-data rollback. |
| Recoverable environment move | Move to recovery | 移至復原位置 | No disk space is freed; this is not deletion. |
| OS browser launch callback succeeded | Authorization link opened | 已開啟授權連結 | Does not establish that browser authorization completed. |

## Independent adversarial review and adjudication

A read-only critic reviewed the pilot and then the Capture/setup/recovery
flows. The main reviewer checked the material source claims and returned the
following adjudication. The critic accepted the corrections in the bounded
rebuttal; no material factual disagreement remains. Policy choices remain
proposals, not implementation authorization.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Rejected toolbar invocation encourages retry despite an uncertain outcome | Low | `toolbar.js` click handler; `toolbar-notice.test.cjs` rejection case | Clarify result and avoid replay | Use “Could not confirm the action result. Check the current state.” | Modify; order 0 | Add assertion that invocation occurs once and the unknown-result notice appears. |
| Explicit toolbar rejection has no notice | Low; main-review addition | `toolbar.cjs` handler returns `false`; renderer only catches exceptions | Complement exception handling | Handle exactly `false`; do not infer completion from truthiness or message content | Accept; order 0 | Cover explicit rejection, exception and successful operation separately. |
| Stop/save failure still permits close or quit | Medium | `capture.cjs:confirmStop` awaits `stop()` then returns true; `main.cjs` close and before-quit handlers | Do not promise a successful save before leaving | Current copy proposal says “attempt to save”; retaining the window on failure is a separate behavior decision | Policy before order 2 | Existing capture smoke checks partial output and successful confirmation separately; combined failure plus exit coverage is missing. |
| Capture folder hint sounds global | Low | `capture.cjs:chooseDirectory`; `capture-settings.cjs:set` | Describe per-format preference | Preserve first-selection seeding of the other unset format; do not claim preferences are completely independent | Modify; order 2 | Cover initial seeding and later independent PNG/WebM changes. |
| Localization could alter command or renderer boundaries | Integration constraint | `ui-commands.cjs:UI_ACTIONS`; `toolbar.cjs` sender and asset guards | Preserve structural commands and strict assets | Accept as invariants, not findings of a current bypass | Accept; order 1 | Existing guard tests plus explicit asset and localized-label cases. |
| Independent Desktop locale adds a second preference | Product tradeoff | Pre-Core setup/recovery; independently selectable Core source | Consider shared/advisory locale | Recommend independence for startup coverage; validated advisory locale is a feasible alternative | Policy; recommended for order 1 | Test missing/malformed preference and unavailable/older Core. |
| Setup/recovery wording can overstate rollback or cleanup | Integration constraint | `setup.cjs:requestSetupCancel/cleanupManagedVenvs`; `core-source.cjs:confirm` | Keep cancellation ownership, retained data and no-space-freed consequence | Preserve existing behavior and explicit warnings | Accept; order 3 | Cancellation, stale confirmation, retained files and recovery tests remain required. |
| Translate setup ahead of capture for first-run users | Optional ordering | Setup precedes the main window | Prioritize onboarding if needed | Current priority is completing the small Desktop pilot first | Defer until onboarding priority changes | Does not block order 0 or the pilot. |

The review added an explicit recording-exit policy checkpoint and moved toolbar
feedback ahead of localization. It preserved the current authorization model,
limited public Agent Info, per-tab permission instructions, exact renderer
allowlists and recoverable cleanup. No discovery expansion is needed for
Desktop localization.

## Evidence and acceptance limits

The review baseline passed five Node test-file entries:
`agent-menu.test.cjs`, `ui-commands.test.cjs`, `toolbar.test.cjs`,
`toolbar-notice.test.cjs`, and `diagnostics.test.cjs`. It ran with the installed
Electron binary in Node mode, Node 24.20.0, satisfying Desktop's declared Node
minimum. These are mocked/unit checks, not five native GUI scenarios. Source
inspection of Capture/setup/recovery does not imply their smoke suites ran in
this review.

The review table is checked using `build_ui_messages.build_catalog` for schema,
keys and placeholders, plus an assertion that all rows are proposed and all
Traditional Chinese cells remain empty. No runtime catalog is generated.
Windows/macOS native localization, installer lifecycle and packaged acceptance
remain future work. This plan does not qualify or publish a release.
