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
- [ ] Visitor/operator-facing text is Traditional Chinese, or translated in `i18n/zh_TW.po`
- [ ] No API keys, passwords or customer conversation content in code, tests, logs, screenshots or config (only `*.example` templates)
- [ ] README.md and README_zh-TW.md updated if configuration or behaviour changed
- [ ] Commit message says in one line what changed and why
