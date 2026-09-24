# UI locale expansion draft

This draft compares the confirmed eight-language expansion for the Desktop surface and three Core connection strings: `en`, `zh-TW`, `zh-CN`, `ja`, `ko`, `de`, `fr`, and `es`.

Use `ui_locale_expansion.tsv` as an exchange table for external AI review. Keep `key` stable, copy source English and Traditional Chinese exactly from the source TSVs, preserve product tokens such as Agent, Core, and StandTerm, and preserve every placeholder if rows with interpolation are added. Return proposed edits in the same TSV shape, with `status` changed only after human review; this file is a draft and is not runtime-approved.

Tentative glossary: preserve the `Agent`, `Core`, and `StandTerm` tokens; `Prompt` is translatable UI terminology, not an invariant product token. Menu actions should stay concise and retain their ellipsis style. The source catalogs contain approximately 799 Core/UI review rows and 305 Desktop review rows; the current Desktop review has 293 translation-reviewed rows and 12 removed rows (counts are review coverage, not a promise of runtime coverage).

Main risks are language-specific plural and gender rules, interpolation and placeholder ordering, and labels that expand enough to affect menu width, truncation, keyboard access, or native menu layout. External review should also check typography, punctuation, and whether product names or UI labels should remain self-named.
