# Desktop copy review and localization plan

Review date: 2026-09-20. The initial review inventories Desktop copy, reviews
behavior and orders implementation. Completed implementation steps are recorded
below. The Desktop language pilot is available; broader translation and native
acceptance remain separate. Browser acceptance is recorded in
[browser_ui_acceptance.md](browser_ui_acceptance.md).

## Recommended implementation order

| Order | Deliverable | Relative effort | Completion evidence |
| --- | --- | --- | --- |
| 0 — Complete | Clarify toolbar action feedback before localization. A resolved `false` now shows unavailable feedback; a rejected invocation reports an uncertain result without suggesting retry. | Small | Both original failures reproduced before the fix; renderer and command-guard checks passed. Each click invokes once, with no automatic replay or invented completion notice. |
| 1 — Complete | Add a Desktop-owned language preference and catalog; pilot custom menus, toolbar labels and Agent help. | Medium | English default/fallback, `en` and `zh-TW`, malformed preference fallback, next-launch application, translated title/ARIA labels without losing SVGs, fixed command IDs, focus/origin guards and staging inclusion verified. Native acceptance remains order 4. |
| 2 — Complete | Localize Browser Access, Diagnostics, About, external-browser confirmations and Capture; retain the window when recording save fails during close/quit. | Medium | Sensitive clipboard feedback, fixed authorization actions, escaped diagnostic fields, literal event JSON, typed Capture state, folder settings and combined save-failure plus close/quit coverage verified. |
| 3 — Complete | Localize setup, Core source selection/recovery, startup error wrappers and per-mode environment cleanup confirmations. | Medium to large | Both languages work before Core is available. Cancellation waits for owned installers; stale confirmations do nothing; source switching, restart/session closure, retained files and recovery moves remain explicit. Installer-wide dialogs remain in order 3b. |
| 3b — Complete | Localize port selection, Files download feedback, installer-wide summary/error dialogs and native paste confirmation. | Small to medium | Preserve structured outcomes/counts, numeric actions and clipboard guards. Installer-wide notices use the common effective language of relevant modes, otherwise English; no shared preference is written. |
| 4 — Partial; Windows candidate produced | Complete Windows and macOS native acceptance and packaged asset checks. | Platform-dependent | Windows native rendering, 165 Windows unit tests, exact extracted-installer payload and packaged Windows/WSL smoke passed. Native OS dialogs, install/upgrade/uninstall and macOS remain; see the acceptance report. |

Order 4 produced a Windows evaluation candidate; results, source/artifact hashes
and remaining native acceptance are recorded in
[desktop_ui_acceptance.md](desktop_ui_acceptance.md).
Raw diagnostic errors, errors before profile selection, legacy shortcut setup
and external installer UI are outside the current Desktop catalog coverage.
The operator chose to retain the window and show the error and unfinished-file
location when recording save fails during close/quit. Orders 1–3 remain separate
reviewable changes; native OS acceptance remains order 4.

## Difficulty and design choices

Copy extraction is straightforward. Most work lies in several display contexts:
native Electron menus/dialogs, a restricted toolbar renderer, setup windows and
scriptless diagnostics. A complete Desktop rollout has moderate implementation
cost and broader acceptance cost than the toolbar pilot. The estimates above
are relative scope assessments, not measured delivery times.

The pilot stores the Desktop language in `language.json` under the existing profile's `userData`,
with English as default and only `en` / `zh-TW` initially. Apply a change on the
next launch; changing language should not itself restart StandTerm or stop a
recording. Keep the existing Core browser preference independent. This covers
setup and recovery before Core starts, at the cost of two language preferences.
It is a product choice, not a security requirement. A validated
two-value advisory preference from Core is also feasible, but needs startup,
origin and older-Core fallback rules. Automatic OS-language selection is deferred.

Ship the Desktop catalog with the shell. Do not depend on the selected bundled
or Git Core supplying compatible renderer scripts. Reuse the existing TSV
schema and validation rules. `build_ui_messages.py --desktop` produces
`desktop/messages.js`; the default command still produces only Core's catalog.
The headless smoke runner checks both targets. Do not add the Desktop rows to
the Core runtime catalog.

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
the same nine columns as the browser table. After the remaining shell notices,
301 rows are `translation-reviewed` with English and Traditional Chinese text.
Four retired Capture/setup fragments or renamed messages are marked `remove`;
no seed rows remain `proposed`. This does not claim translation of raw errors or
the external installer UI. Some `current_en` cells are exact fragments or normalize
dynamic values to named placeholders; `context` identifies these cases.

Approve the English behavior and terminology before requesting translations of
newly inventoried rows. Keep already reviewed rows unchanged.
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
following Desktop additions are used by the reviewed messages:

| Concept | English | Traditional Chinese | Boundary |
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
| Stop/save failure still permits close or quit | Medium | Original `capture.cjs:confirmStop` awaited `stop()` then returned true | Retain the window when saving fails | Operator approved retaining the window and showing error/file locations | Resolved in Capture batch below | Combined failure, close/quit and concurrent-dialog coverage now passes. |
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

## Pilot review and evidence

The independent implementation review accepted the preference lifetime, numeric
dialog responses, typed commands and exact renderer asset boundaries. Its only
copy finding changed the language dialog from “Some Desktop dialogs remain in
English” to “Some Desktop text remains in English”: recording status and some
menus are also outside this pilot. The focused second pass found no material
remaining gap in the new preference, DOM and command tests.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Partial coverage disclosure mentions dialogs only | Low | `toolbar.js` preserves raw capture status; View retains capture menu labels | Say some Desktop text remains English | Applied to both languages | Accept | Reviewed TSV and regenerated catalog. |
| Save can finish before its notification fails | Validation boundary | `language.cjs:choose` persists before notification | Verify saved choice survives notification failure | Keep uncertain-result wording and the stored selection | Accept | Lost-notification test plus reopening the chooser passed. |
| Labels must not change action or target dispatch | Correctness boundary | `ui-commands.cjs` action and snapshot target | Test both locales with unchanged dispatch values | Retained existing guards | Accept | Typed command/terminal ID, invalid display-label dispatch and focus tests passed. |

The pilot preserved Core's independent language preference and recording-exit
behavior. The later Capture batch changes the exit policy with operator approval.
Native OS qualification is still order 4.

## Browser Access and Diagnostics review

The second implementation batch adds 76 reviewed bilingual messages. Browser
authorization notices describe opening a link, without claiming the browser
completed authorization. Copying a token/access URL still makes no grant request;
creating an authorization link still requires the existing confirmation. The
warning retains sensitive URL handling, browser history and replacement of the
previous unused link. Exceptions still produce a sanitized notice.

Diagnostics localizes menus, runtime field labels/states and explanatory text.
Version strings, paths, bundle hashes, instance IDs, backend origins and event
JSON remain literal data. The diagnostic HTML still has no scripts; every
translated string and displayed value is escaped at the final HTML boundary.
About and external-browser confirmations share these display conventions.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| “Log writable” implies a live filesystem check | Low | `createDiagnostics.available` starts true and tracks write outcomes | Describe write status, not present writability | Use “Log write status” with “No write error reported” / “Write failed” | Accept | Existing log-failure tests and localized display reviewed; flag semantics unchanged. |
| An opened authorization link does not prove completed authorization | Correctness boundary | `createBrowserAccess.run` awaits the OS open callback | Keep link-open feedback distinct from authorization | Applied reviewed copy; no request or permission changes | Accept | Both locales exercise four actions, exact payloads/request counts, cancellation and sanitized exceptions. |
| Translation must not introduce markup or change diagnostic exports | Correctness boundary | `statusHtml` and structured logger | Escape all display content; retain raw event JSON and CSP | No event translation layer or renderer scripts added | Accept | Malicious translation/data tests, isolated window callbacks and real browser DOM checks passed. |

The critic's focused second pass found no remaining material issue. It also
checked that Browser Access, DevTools and external-browser confirmations retain
`response === 1`, default/cancel index 0 and the original sensitive-data boundaries.
Native dialog layout and interaction are still part of order 4.

## Capture review and evidence

The operator-approved contract cancels the current close/quit when recording
save fails or cannot be confirmed. Desktop restores/shows the window and displays
a persistent error dialog with the original error and available unfinished-file
and requested-destination paths. It does not automatically retry. Once recording
is inactive and the dialog is dismissed, a later explicit close/quit is allowed;
this is not a permanent exit lock. A canceled recording folder chooser is not a
save failure.

The batch adds 44 reviewed bilingual messages for Capture settings, menus,
recording state, notifications and whole close/quit prompts. The old grammatical
`{action}` fragment is retired. Action IDs and numeric dialog responses remain
structural; paths and lower-level recorder/file errors remain literal data.
Folder selection still seeds the other format only when that preference is unset.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Startup/background failure can clear the job before confirmation observes it | High | `begin`, `stop`, `finish` and pending confirmation lifetime | Preserve the original job outcome across awaits | Save structured results on the job and propagate startup failure; reject stale replacement jobs | Accept | Startup failure, background completion, canceled chooser and stale/duplicate confirmation tests pass. |
| A notification exception can obscure a saved result or reach generic process exit | High | Publication precedes notification; before-quit generic exception path exits | Separate file outcome from notification; reject uncertain quit | Store result before notification, treat notification as best effort and make quit rejection keep the app running | Accept | Notification/dialog rejection, single publication and actual main shutdown callback tests pass. |
| Final and partial paths can both exist after partial-link cleanup fails | Medium | Hard-link publication precedes partial unlink | Avoid claiming the destination is absent | Show a requested destination without asserting save completion; preserve both files | Accept | Real temporary-file test verifies both paths retain bytes after an injected unlink failure. |

The critic's focused second pass found no remaining material race or premature
exit issue. Main review also blocks repeated close events while the failure
dialog is pending. Native dialog readability remains unqualified.

Capture completed on 2026-09-20:

- All 133 Desktop unit tests passed under Electron's Node 24.20.0 runtime.
- Seven catalog regression tests and both catalog freshness checks passed.
- The real toolbar DOM passed in both locales at 640px, including active/paused
  labels, pause/resume ARIA and visible capture controls. The paused English case
  initially placed the stop button about 29px outside the viewport; reducing the
  existing compact button padding fixes this without shortening state text.
- Native IPC is mocked in the DOM test. No native OS dialog, encoder smoke or
  installer acceptance was run for this batch. Capture permissions, recorder
  isolation and automatic finalization on hide/minimize/navigation are unchanged.

## Setup and Core source review and evidence

The batch adds 99 reviewed bilingual messages. Python prerequisites, environment
creation consent, preparation/cancellation progress, per-mode cleanup, Core source
management and recovery use the Desktop catalog before Core is available. Stable
setup error codes select translated explanations; unknown codes retain the
platform-help fallback. Original technical errors, paths, distribution names and
commit identifiers remain data. The replaced macOS help constant was removed;
interpreter discovery and architecture checks are unchanged.

Normal Desktop calls pass the language captured at launch, so a newly saved
preference does not change later dialogs until restart. Installer preparation
and cleanup read the selected Windows/WSL mode profile directly; the maintenance
profile does not override either mode. Installer-wide summary/error dialogs stay
English for now. Their common-language policy is separate from per-mode setup.
No OS-language inference, preference migration or installer lifecycle change was
introduced.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Shared setup page promises not to modify a Git checkout while Git update uses that page | Medium; preexisting copy defect | `setup.html`, `manageCore`, `runPreparation` | Remove the shared Git assertion | Keep the narrower bundled-setup promise in its action-specific consent; remove it from shared progress copy | Accept | Actual page and Git/copy progress scripts checked in both locales. |
| Maintenance exits before normal Desktop language initialization | Medium; integration requirement | `main.cjs` maintenance branch and `installer.cjs` setup/cleanup callbacks | Wire per-mode locale or explicitly defer installer UI | Read the relevant mode profile in setup/cleanup defaults; pass launch language explicitly for normal Desktop | Modify | Both Windows and WSL tests use a conflicting maintenance preference; installer ownership tests remain passing. |
| Existing setup tests do not execute injected DOM updates | Medium; validation gap | `setup.test.cjs` original no-op renderer | Add bilingual DOM execution with literal interpolation | Capture actual initialization/progress/cancel scripts and execute them against actual HTML in Chromium | Accept | Six locale/platform cases passed, including malicious distribution text, ARIA and 700px layout. |
| English-only button mocks do not prove localized routing | Low; validation gap | Setup and Core manager dialog fixtures | Test numeric action effects in both languages | Preserve numeric decisions and fixed action IDs; add bilingual consent, cleanup, cancellation, retry/recover and label-collision cases | Accept | Both-language action tests and retained typed error codes passed. |
| Inherited progress-map properties can display unknown stages | Low; preexisting hardening | Original `labels[stage]` lookup | Use an own-property or supported-stage check when translating | Limit display updates to five existing stage IDs | Accept | Unknown `__proto__`, `constructor` and markup-like stages leave the display unchanged; cancellation suppresses later progress. |

The focused second review found no remaining material correctness issue. The
review changed shared copy, maintenance locale wiring and validation coverage;
it preserved process ownership, stale-confirmation guards, no-space-freed cleanup,
restart/session closure, reauthorization warnings and raw error details. Native
dialog and installer-wide language qualification remain deferred until their
explicit acceptance/implementation stages. No behavioral policy was reopened.

Setup/Core source completed on 2026-09-20:

- All 145 Desktop unit tests passed under Electron's Node 24.20.0 runtime.
- Seven catalog regression tests and both generated catalog freshness checks
  passed. The table contains 269 reviewed messages and four retired rows.
- The real setup HTML and injected scripts passed six Chromium cases: both
  locales on Windows, macOS and WSL display branches. Progress/cancel text,
  language/title, ARIA, literal data, no injection, no external requests and
  700x500 layout passed. Native dialogs and setup processes are mocked.
- No renderer scripts/assets or permissions were added; the existing setup URL
  allowlist and CSP are unchanged. No native GUI, real dependency installation,
  installer build or packaged acceptance is claimed for this batch.

## Remaining shell notices review and evidence

The batch adds 32 reviewed bilingual messages for port selection, download
results, installer-wide summaries/errors and the native context-paste dialog.
Port prompts use whole messages selected by typed reasons, preserving cancel,
use-once and remember decisions. Saving still follows host checks, authenticated
verification and consent. The saved-port warning now states only that the port
passed verification but its setting could not be saved; it does not promise that
later startup steps succeed.

Installer-wide notices capture the common effective language of the relevant
profiles at entry: selected modes for preparation, Windows and WSL for uninstall.
Missing/invalid preferences retain the existing English fallback, and mixed
effective locales use English. No preference is written or migrated; per-mode
preparation and cleanup keep their own language. The operator selected this
policy: use the common setting, otherwise English, without a new preference.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Interrupted or pathless download result does not establish a connection failure | Medium | `floating-windows.cjs` reports state/path only; `main.cjs` requires a completed state and path | Use neutral outcome wording | Report unconfirmed completion and ask the operator to inspect the download destination before downloading again | Accept | Both languages cover completed/pathless/interrupted outcomes; reveal requires completed plus response 1; no automatic replay. |
| Retained count mixes per-item results and whole-mode retention | Medium | `installer.cjs` aggregation and `cleanupManagedVenvs` top-level retained result | Name the count as results, not environments | Preserve aggregation and explain the mixed units; count unknown modes separately | Accept | Mixed item/mode results and cleanup exceptions retain exact numeric totals. |
| Generic installer failure can occur after completed recovery moves or shortcut changes | Medium | Uninstall cleanup/report precede shortcut updates | Acknowledge partial changes without implying rollback | Explain that completed changes remain and recovery locations should be checked before retry | Accept | Injected shortcut failure after two confirmed moves retains original error and failure exit code. |
| Cleanup scope wording implies both environments were successfully checked | Low | Missing settings/interpreter can retain a mode before inventory | Describe the permitted scope only | State that cleanup is limited to Windows and the configured WSL distribution | Accept | Copy review; mode iteration and owned installer lifecycle unchanged. |
| Notification rejection can still abort startup despite a saved-port notice | Preexisting behavior boundary | `startWithPort` awaits `notify` | Avoid silently changing notification behavior during localization | Remove the future-continuation promise; defer any best-effort notification change to a separate behavior patch | Modify | Existing startup flow retained; verification and persistence-failure ordering tested. |
| Files is the source UI, not the local download destination | Low; focused second pass | Download completion reports a native path | Direct the operator to the destination | Applied in both languages | Accept | Final table/callback review. |
| Paste delivery can precede a rejected acknowledgment | Main-review addition after independent review | `context-paste.cjs` awaits `completeContextPaste` before generic catch | Avoid encouraging a second paste when the outcome is unknown | Use an unconfirmed-result notice and tell the operator to inspect the terminal | Accept | One clipboard read and one delivery attempt despite lost acknowledgment; both-language cancel/stale-target tests pass. |

The independent two-round review of port/download/installer changes found no
remaining material correctness, authorization or lifetime issue. The final
context-paste inventory addition was reviewed by the main agent and covered by
the final suite; it was not a third independent review round. Existing clipboard
permission denial, focus/frame/navigation/target guards, silent canceled downloads,
typed action dispatch, owner watching and shortcut publication order remain.

Order 3b completed on 2026-09-20:

- All 165 Desktop unit tests passed under Electron's Node 24.20.0 runtime,
  including actual main-process callbacks and the installer coordinator in VM
  fixtures. Native dialogs and installer subprocesses are mocked.
- Seven catalog regression tests and both catalog freshness checks passed;
  the catalog contains 301 reviewed messages and four retired rows.
- No renderer HTML/CSS/assets changed in this batch, so the earlier DOM results
  remain separate evidence; no new browser or native GUI acceptance is claimed.
- Native dialog readability, Windows installer behavior and packaged acceptance
  remain order 4. This batch does not build or publish a release.

## Evidence and acceptance limits

Browser Access/Diagnostics completed on 2026-09-20:

- All 115 Desktop unit tests passed under Electron's Node 24.20.0 runtime.
- Seven catalog regression tests and both catalog freshness checks passed.
- The actual diagnostics HTML passed headless Chromium checks in both locales
  at 640px: translated headings/labels, raw paths and JSON, empty-state copy,
  escaped markup, no external requests and no horizontal overflow.
- No native OS GUI or installer acceptance is claimed. The new messages use
  the catalog already included by the existing staging and builder rules.

Order 1 completed on 2026-09-20 with 50 reviewed bilingual messages:

- All 110 Desktop unit tests passed under Electron's Node 24.20.0 runtime.
- Seven catalog regression tests passed, including independent Desktop output
  and stale detection without changing Core output. Both generated catalogs
  passed their freshness checks.
- The real toolbar DOM passed in English and Traditional Chinese at 640px:
  labels, ARIA, SVG preservation, typed recording state, literal notices,
  failure feedback without replay and unknown-locale fallback. Native IPC was
  mocked in this headless Chromium check.
- The actual staging script copied the new catalog/helper/preferences module.
  Their bytes matched the source; the staged catalog loaded independently, and
  the builder's inclusion rules covered all three files. No installer was built.

The restricted sandbox initially blocked Chromium startup and a Git subprocess
used by the full Desktop suite. The same checks passed after running outside
that process restriction. This was an environment failure, not a product fix.

Order 0 completed on 2026-09-20. Before the renderer change, regression tests
reproduced both the missing explicit-rejection notice and the misleading retry
notice. After the change, all seven renderer notice tests passed, covering
action and menu dispatch, exactly one invocation per click, strict `false`
handling, existing operation feedback and notice expiry. The toolbar and UI
command guard test files also passed under Electron's Node 24.20.0 runtime.
The independent critic checked the bounded patch and test coverage. No native
GUI acceptance or language rollout is claimed for this step.

The review baseline passed five Node test-file entries:
`agent-menu.test.cjs`, `ui-commands.test.cjs`, `toolbar.test.cjs`,
`toolbar-notice.test.cjs`, and `diagnostics.test.cjs`. It ran with the installed
Electron binary in Node mode, Node 24.20.0, satisfying Desktop's declared Node
minimum. These are mocked/unit checks, not five native GUI scenarios. Source
inspection of Capture/setup/recovery does not imply their smoke suites ran in
this review.

The review table is checked using `build_ui_messages.build_catalog` for schema,
keys, placeholders and review gates. Only the 301 reviewed rows enter the
Desktop runtime catalog; retired rows stay out of it.
Windows automated native rendering and packaged acceptance are recorded in the
acceptance report. Native dialog/installer lifecycle and macOS qualification
remain future work. This plan does not qualify or publish a release.
