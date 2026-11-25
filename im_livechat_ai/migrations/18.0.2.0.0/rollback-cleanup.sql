-- Live Chat AI 18.0.2.0.0: cleanup after a rollback to 18.0.1.3.5. Run it by hand, once, after the rollback
-- (`-u im_livechat_ai` with the 18.0.1.3.5 code) succeeded; Odoo never runs this file (only *.py files of a
-- migrations folder are scripts). README.md and README_zh-TW.md show the same block.
--
-- Odoo keeps the tables of the models the rollback removed. Emptying them removes the AI connections' keys, frees
-- the bot partners for a later re-upgrade and keeps the sequences. The monthly AI Usage of the rolled-back period
-- is deleted first: its rows point to the assistants and the connections (ON DELETE RESTRICT).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ir_model WHERE model IN ('im_livechat.ai.connection', 'im_livechat.ai.assistant')) THEN
    IF to_regclass('im_livechat_ai_usage_month') IS NOT NULL THEN DELETE FROM im_livechat_ai_usage_month; END IF;
    IF to_regclass('im_livechat_ai_assistant') IS NOT NULL THEN DELETE FROM im_livechat_ai_assistant; END IF;
    IF to_regclass('im_livechat_ai_connection') IS NOT NULL THEN DELETE FROM im_livechat_ai_connection; END IF;
    DELETE FROM ir_attachment WHERE res_model IN ('im_livechat.ai.assistant', 'im_livechat.ai.connection');
  END IF;
END $$;
