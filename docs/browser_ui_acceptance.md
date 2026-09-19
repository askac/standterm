# Browser localization acceptance

## Scope

The completion target is the English and Traditional Chinese (Taiwan) browser
interface. It includes the initial access page, session recovery, connection and
SSH editors, Agent access and approvals, settings, tunnels, diagnostics controls,
paste review, Files, and terminal popup/PiP windows. Native Desktop menus, setup
and native windows are a separate phase. Language changes still take effect on
the next page open; saving preferences does not disconnect the current page.

The editable translation source is [ui_copy_review.tsv](ui_copy_review.tsv).
It contains 796 bilingual messages and one removal row. Stable keys, English
source, context and placeholder constraints accompany each target-language cell
so translation can be delegated without changing protocol or UI behavior.

## Display and data boundary

| Surface | Translated | Retained verbatim |
| --- | --- | --- |
| Connections, SSH routes and settings | Product labels, actions, authored instructions and known state labels | Hosts, usernames, paths, key references, fingerprints, plugin labels/options, setting keys and values |
| Agent and transfer panels | Permission labels, action controls, result guidance and diagnostic headings | Tokens, copyable prompts/commands, URLs, binding IDs, action/privacy enums and error codes |
| Diagnostics | Navigation, counts, Clear, Copy and clipboard result | Export headers, timestamps, event names, IDs and sanitized JSON details |
| Files | Navigation, actions, confirmation, progress and result guidance | Filenames, endpoint paths, byte values, conventional units and backend messages |
| Errors | Authored workflow guidance and structured known-state fallbacks | Unrecognized backend/plugin errors, SSH model/key/signing validation diagnostics and browser-provided errors |
| Appearance | Preference labels and palette descriptions | Named themes, font-family strings, sample commands and color values |
| Access fallback | Normal browser login and recovery controls | Usable English fallback when JavaScript or localization assets are unavailable |

Raw technical errors can therefore still appear in English. This acceptance
does not claim that arbitrary plugin output, operating-system passkey dialogs or
terminal applications are translated. Future diagnostic localization should use
typed reasons, without matching English error text.

## Behavior checked

| Workflow | Acceptance contract |
| --- | --- |
| Access URL | Fetch only after Copy or confirmed Show; preserve the exact URL. Reveal hides after 30 seconds without expiring the token. Clipboard success follows completion; rejection/fallback failure does not report success or reveal automatically. |
| Device recovery | Keep credential bytes, RP and ceremony binding. Registration, enabling a live session and revoking all registrations for the hostname remain distinct. Revocation does not claim to delete OS passkeys. Action errors survive status refresh; live session bindings remain in memory and are not persisted as tokens. |
| Paste and Agent targeting | Preserve ESC handling, bracketed paste, line endings, review thresholds, focus and terminal guards. Review displays the target and line count without a false byte-count claim. Translated labels never select an action or terminal. |
| Files | Preserve direct endpoint scope, filenames, sort keys, conflict choices, request IDs and two-step permanent deletion. Rename validity uses a stable key/null result. Download dispatch is labeled started. Finalizing cannot be cancelled; unknown publication outcomes instruct inspection before retry. |
| Secondary windows | Apply the selected language safely to newly created DOM and preserve terminal input, Files transitions and return-to-main behavior. Translation uses text/allowlisted attributes, not inserted HTML. |
| Layout | Exercise both languages at 480x600 for remaining dialogs and child-window controls, alongside existing preference/editor layout coverage. |

## Reproduction

Use the repository's configured WSL Python environment and installed Playwright
Chromium. These suites use local fixtures rather than production credentials:

```bash
python tests/browser_completion_i18n_smoke.py
python tests/browser_completion_i18n_smoke.py --files
python tests/agent_browser_smoke.py
python tests/browser_popout_smoke.py
python tests/agent_backend_smoke.py
python scripts/build_ui_messages.py --check
python tests/ui_messages_smoke.py
node tests/ui_i18n_smoke.cjs
```

The existing connection, SSH editor, settings transfer/preferences/appearance,
Server settings, SSH tunnel and Agent Tunnel bilingual suites provide the other
workflow regressions. See the completion ledger in
[the review plan](agent_ui_review_plan.md#browser-completion-and-acceptance) for
results and limitations.
