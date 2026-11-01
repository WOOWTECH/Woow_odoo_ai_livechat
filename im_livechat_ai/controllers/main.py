# -*- coding: utf-8 -*-
"""Signed download of visitor attachments for the AI backends.

``GET /im_livechat_ai/attachment/<id>/<expires>.<hex HMAC-SHA256(database.secret,
"im_livechat_ai|<id>|<checksum>|<expires>")>``, valid one hour (see
models/discuss_channel.py ``make_token``). Only attachments of messages in a
live chat session (a discuss.channel with livechat_channel_id) are served,
always as a download; anything else is 404. Same shape as woow_ai_contact's
/woow_ai/attachment route.
"""

from werkzeug.exceptions import NotFound

from odoo import SUPERUSER_ID, http
from odoo.http import request

from ..models.discuss_channel import ATTACHMENT_ROUTE, check_token


class LivechatAiAttachmentController(http.Controller):

    @http.route(ATTACHMENT_ROUTE + '/<int:attachment_id>/<string:token>', type='http', auth='public',
                methods=['GET'], readonly=True, save_session=False)
    def im_livechat_ai_attachment(self, attachment_id, token):
        env = request.env(user=SUPERUSER_ID, su=True)
        attachment = env['ir.attachment'].browse(attachment_id).exists()
        if not attachment or attachment.type != 'binary':
            raise NotFound()
        secret = env['discuss.channel']._ai_attachment_secret()
        if not check_token(secret, attachment.id, attachment.checksum, token):
            raise NotFound()
        messages = env['mail.message'].search([
            ('attachment_ids', 'in', attachment.ids), ('model', '=', 'discuss.channel'),
        ])
        sessions = env['discuss.channel'].browse(set(messages.mapped('res_id'))).exists()
        if not sessions.filtered(lambda s: s.channel_type == 'livechat' and s.livechat_channel_id):
            raise NotFound()
        stream = env['ir.binary']._get_stream_from(attachment, 'raw')
        response = stream.get_response(as_attachment=True)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'private, no-store'
        return response
