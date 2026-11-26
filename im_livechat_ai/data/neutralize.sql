-- Live Chat AI (im_livechat_ai): a neutralized copy never calls a real AI backend.
-- odoo neutralize runs every installed module's file in one cursor and stops at the first error, so each table
-- and column is checked first: a database still on 18.0.1.x (a production copy neutralized with a newer image
-- before -u), or one whose install was interrupted, can still be neutralized. The channels' "Answer With AI"
-- switch stays as it is: inactive connections without URL and key stop every call.
DO $$
BEGIN
    IF to_regclass('im_livechat_ai_connection') IS NOT NULL THEN
        UPDATE im_livechat_ai_connection
           SET endpoint_url = NULL,
               customer_safe = (backend_type = 'openai'),
               active = FALSE;
        IF EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = 'im_livechat_ai_connection'
                      AND column_name = 'api_key_secret') THEN
            UPDATE im_livechat_ai_connection SET api_key_secret = NULL WHERE api_key_secret IS NOT NULL;
        END IF;
    END IF;
    -- 18.0.1.x backend columns of the Live Chat channels (kept one version on upgraded databases)
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'im_livechat_channel'
                  AND column_name = 'ai_api_token_secret') THEN
        UPDATE im_livechat_channel SET ai_api_token_secret = NULL WHERE ai_api_token_secret IS NOT NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'im_livechat_channel'
                  AND column_name = 'ai_endpoint_url') THEN
        UPDATE im_livechat_channel SET ai_endpoint_url = NULL WHERE ai_endpoint_url IS NOT NULL;
    END IF;
END $$;
