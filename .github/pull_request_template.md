## What

<!-- One or two sentences: what changes for visitors, operators or administrators. -->

## Why

<!-- The problem this solves (issue id / link, e.g. P1-4), and why this approach. -->

## How tested

<!-- Paste real evidence, not "tested locally". -->

- [ ] `Odoo tests` workflow is green (number of im_livechat_ai tests started: ___)
- [ ] New or changed behaviour has an automated test
- [ ] Manual check on a test database (steps and result):
- [ ] Upgrade from the previous version checked (`-u im_livechat_ai` on a copy with existing data), if models, data or migrations changed

## Checklist

- [ ] `__manifest__.py` version bumped when models, views, data or migrations changed (the platform upgrade gate only upgrades on a version change)
- [ ] Every new string has an English source and its zh_TW in `i18n/zh_TW.po` (export `i18n/im_livechat_ai.pot`), with the words of the contract glossary (docs/CONTRACT.md 3.11), no banned variant and no new word for an existing concept
- [ ] No API keys, passwords or customer conversation content in code, tests, logs, screenshots or config (only `*.example` templates)
- [ ] README.md and README_zh-TW.md updated if configuration or behaviour changed
- [ ] Commit message says in one line what changed and why

## Contract review checklist (docs/CONTRACT.md section 4)

- [ ] Does the change touch a contract area (connection or assistant fields, roles, the queue or runner, the wire format, errors, logs and usage, neutralize, the glossary)? If yes, the contract gets a new version and Discuss AI and Project AI get a tracking item.
- [ ] `im_livechat_ai/tests/contract/` equals the package of the contract version the README states (`cd im_livechat_ai/tests/contract && sha256sum -c SHA256SUMS`)
- [ ] `test_contract` is green in the sandbox on the production core 18.0.20250807, and the GitHub `Odoo tests` workflow is green
- [ ] Every new string has an English source and its zh_TW from the glossary (3.11), with no banned variant
- [ ] No key, header, upstream body, exception text or secret URL in logs, call logs, job errors or notifications
- [ ] `data/neutralize.sql` covers every new URL, key or push-key column, with a guard
- [ ] Upgrade rules (3.13): removed fields keep their columns for one version, noupdate data only through migrations, uninstall effects, views that use removed fields
- [ ] README.md, README_zh-TW.md (3.12) and `docs/PROTOCOL.md` are updated
- [ ] New record text sent to the model: who can write it? Text that customers or visitors write is labelled as data (3.14)
- [ ] A change that can stop an assistant answering ships with its log, inventory query, README note and test (3.13)
- [ ] The message trigger re-raises PostgreSQL concurrency errors
