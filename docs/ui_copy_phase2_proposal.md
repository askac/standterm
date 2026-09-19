# Browser access and Agent approval copy proposal

Status: accepted proposal snapshot. The browser access/recovery and Agent
approval/transfer messages are now integrated through `ui_copy_review.tsv`.
Authorization, recovery eligibility and transfer control rules are unchanged. The companion `ui_copy_phase2_proposal.tsv`
contains 40 candidate rows: 39 proposed messages and one decorative removal.
This is a focused decision set, not a claim of complete string coverage.

## Decision and boundaries

Use short action labels with precise adjacent context. Distinguish credentials,
authorization, approval, pending work, confirmed results, and uncertain results.
The next implementation should use the existing translation-table workflow and
keep the protocol, authorization rules and entry points stable.

| Concept | English | Traditional Chinese | Boundary |
| --- | --- | --- | --- |
| Browser authorization | Authorize browser | 授權瀏覽器 | Uses a browser authorization URL containing a one-time `authorize` grant. |
| Initial access or expired-session recovery | Use access token | 使用存取權杖 | Uses the current launcher's access token or full Access URL, not an Agent token. |
| Device recovery | Verify with device | 使用裝置驗證 | Requires a credential registered for the hostname and enabled for a still-valid session. A restart requires the current launcher token. |
| Grant terminal access | Authorize agent | 授權 Agent | Existing terminology; independent of browser access. |
| Approve one input proposal | Approve input | 核准輸入 | Does not promise command execution or bypass subsequent validation. |
| Approve one copy proposal | Approve copy | 核准複製 | Keep exact endpoints, paths, size, and conflict consequences visible. |
| Reject one proposal | Reject | 拒絕 | Same `agent_action_reject` path for input and copy; does not revoke access. |
| Pause terminal Agent access | Pause Agent | 暫停 Agent | Different scope from rejecting a proposal or stopping one transfer. |
| Request transfer cancellation | Stop / Stopping… | 停止／正在停止… | A request is not confirmation; cancellation remains unavailable after the commit barrier. |
| Confirmed operator cancellation | Stopped | 已停止 | Select only from `file_copy_cancelled_by_operator`; do not infer from disconnection. |
| Hide a finished transfer entry | Dismiss | 隱藏此筆 | Does not delete files or cancel work. |

Prefer `Restore StandTerm access` over `Restore browser access` so the recovery
dialog cannot be mistaken for the separate browser-authorization gate. Remove
`YOU SHALL NOT PASS!!`, retain the authorization state and launcher identity,
and replace metaphorical progress text with the actual operation.

Do not use `Execute` for arbitrary input, `Cancel` for every negative action,
or `Failed` alone for an unconfirmed destination update. The proposed unknown
result warning must appear in both Agent details and the transfer queue, with
the route and diagnostic code preserved. It must not introduce automatic retries.

The shorter replacement warning retains the existing destination file size.
Omitting `atomically` from visible copy does not change the backend guarantee.
Keep-both text refers to the exact backend-selected destination already shown.

## Implementation order and source evidence

| Step | Scope | Current implementation and test evidence |
| --- | --- | --- |
| 1 | Browser authorization gate and reconnect notice | `templates/index.html:1334`, `:5756`, `:7824`, `:8499`; browser tests `test_browser_authorization_gate_hides_connection_controls`, `test_server_unavailable_waits_for_reconnect`, `test_retry_now_resubscribes_after_socket_disconnect`. |
| 2 | Initial access page and live-session recovery | Separate server-rendered page in `app.py:5626`, recovery dialog in `templates/index.html:1649`; `session_recovery.py:510` filters hostname and bound credentials; `app.py:6462` validates the bound session. Browser tests cover initial access, invalid-session reconnect and device recovery; backend test `test_session_recovery_unauthenticated_options_offer_only_armed_credentials` covers eligibility. |
| 3 | Input and copy approval | `templates/index.html:3842` renders proposals; `:8774` and `:8787` send typed approval/rejection payloads. Browser tests cover stale proposals, canonical copy plans, single-shot decisions and long-path layouts. |
| 4 | Transfer result wording | `templates/index.html:3656` currently warns in details, while `:9154` renders queue status. Backend unknown-publication fixture in `tests/agent_backend_smoke.py:5058` distinguishes uncertain publication from safe cancellation. |

Initial access is not rendered by the main template. Reusing the current main
page translator alone will not cover it. During implementation, verify catalog
availability and saved-language handling on that separate page; keep the
existing no-JavaScript login form and English fallback. Do not change auth
exemptions or grant scope just to load translated text.

After these decisions are accepted, inventory companion progress, validation,
help, accessible-name and reset text before marking either workflow fully
translated. The table already includes key retry and token-checking transitions;
it does not enumerate every backend diagnostic or manual authorization help step.
Use structured error codes for known diagnostic translations and retain the
original message for unknown errors. Never classify errors by English wording.

## Independent adversarial review and adjudication

The read-only critic reviewed source and tests with a bounded investigation.
There were no High or Critical findings. The integrator verified the material
citations and made the following decisions; the critic reported no remaining
substantive disagreement after the response.

| Finding | Severity | Evidence | Critic remedy | Main response | Resolution | Validation |
| --- | --- | --- | --- | --- | --- | --- |
| Unknown publication remains a generic failure in the queue | Medium | `templates/index.html:3656`, `:9166`; browser test currently checks details at `tests/agent_browser_smoke.py:2816` | Show the warning on both surfaces using the existing error enum; retain route and code | Accept; table explicitly names both surfaces | Included in proposal; runtime unchanged | Add one unknown-result fixture that asserts details and queue; verify no retry is emitted. |
| Device recovery wording omits hostname and live-session binding | Medium | `session_recovery.py:510-530`, `app.py:6462` | Keep the eligibility and restart boundary beside the short device button | Accept; shared hint now includes registration, enabled recovery and a still-valid session | Included in proposal | Exercise registered-but-unbound, expired session, different hostname and restart failure paths; preserve access-token fallback. |
| Dynamic/reset text can restore old terminology | Low | `templates/index.html:5756`, `:7829-7832`, `:11843-11868`; `app.py:5813` | Include reconnect, token-checking and device-verification transitions | Accept; added five rows and both static/dynamic source references | Included in proposal | Verify pending, failure and reset states in English and Traditional Chinese. |
| Recovery title can be confused with browser authorization | Optional wording | Distinct `/login` and browser `authorize` paths | Prefer Restore StandTerm access | Modify the initial title proposal accordingly | Included in proposal | Review the two gates together; neither should suggest that an Agent token can unlock the browser. |

Preserved despite possible shortening: credential distinctions, the restart
restriction, canonical paths, replacement consequences and the check-before-retry
warning. Preserved despite possible expansion: short buttons and no visible
atomic-publication jargon. Full platform-recovery settings and generic Files
window localization remain deferred until their respective workflows are scoped.
No unresolved authorization-policy choice remains in this proposal.

## Translation handoff and validation

This proposal file is not the runtime catalog. Reused keys are intentional:
some candidates already exist in `ui_copy_review.tsv`; merge by key after review,
never append duplicate rows. Keep approved runtime rows unchanged until that
merge is deliberate. For a translation handoff, approve English first, then
allow edits only to target-language cells. Validate the returned key set and
all non-target fields against the sent copy before reviewing the translations.

At proposal time, the table validator accepted all 40 candidate rows and
exported none because they remained proposed or marked for removal. This file
retains those historical statuses; the authoritative table records the reviewed
implementation rows. See `agent_ui_review_plan.md` for implementation validation. Implementation validation covers the related browser and backend cases,
both languages at narrow widths, and the initial access form with JavaScript disabled.

The accepted rows were merged by key, with companion progress, validation,
manual-authorization help and accessible names added during implementation.
Further UI expansion remains a separate scoped task.
