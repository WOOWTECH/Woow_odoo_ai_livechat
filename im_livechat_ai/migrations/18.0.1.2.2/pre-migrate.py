"""Recheck the retired Settings view for databases already recorded at 1.2.1.

Odoo skips the 1.2.1 migration directory for those databases. Reuse the exact
guarded cleanup from that directory so a residual owned view is removed safely
before another module validates the Settings form. For an older database both
directories run; the second call sees no target and changes nothing.
"""

import importlib.util
from pathlib import Path


def migrate(cr, version):
    if not version:
        return
    source = Path(__file__).resolve().parents[1] / '18.0.1.2.1' / 'pre-migrate.py'
    spec = importlib.util.spec_from_file_location('im_livechat_ai_guarded_settings_view', source)
    if spec is None or spec.loader is None:
        raise RuntimeError('im_livechat_ai: guarded Settings migration unavailable')
    guarded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guarded)
    guarded.migrate(cr, version)
