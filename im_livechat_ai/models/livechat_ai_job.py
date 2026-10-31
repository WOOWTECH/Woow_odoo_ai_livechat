# -*- coding: utf-8 -*-
"""Durable AI reply jobs for live chat sessions (package C: P1-4..P1-7).

Flow:
  visitor message_post
    -> discuss.channel._notify_thread creates an im_livechat.ai.job in the
       SAME transaction as the message (state pending)
    -> after COMMIT (cr.postcommit) a daemon thread starts, waits the
       debounce delay and runs the job with its own cursor
    -> the job runs under a per-session PostgreSQL advisory lock, so only
       one job per discuss.channel is processed at a time, across all
       prefork workers and the cron process
    -> before calling the LLM and again before posting, a job that has a
       newer job in the same session is marked skipped; the newest job
       answers with the full conversation as context
    -> when the lock holder finishes it runs the newest pending job of the
       session (the jobs whose own thread found the lock busy)
  Human handoff (package D, P1-2): an operator's post pauses the session
  (discuss.channel.ai_paused_until) so no job is created; a visitor asking
  for a human creates a 'handoff' job that posts a fixed notice (no LLM)
  and notifies the operators. Reply jobs check the pause before calling the
  LLM and again before posting.
  A cron (every 2 minutes) hands jobs left pending/running after a crash or
  restart to recovery threads (one per session), at most MAX_JOB_RUNS runs
  per job, and never answers a job when the bot already replied after its
  trigger message.
"""

import logging
import threading
import time
import zlib
from datetime import timedelta

from markupsafe import Markup
from psycopg2.extensions import ISOLATION_LEVEL_READ_COMMITTED

from odoo import api, fields, models, SUPERUSER_ID
from odoo.modules.registry import Registry

from .im_livechat_channel import DEFAULT_ERROR_MESSAGE, DEFAULT_HANDOFF_MESSAGE

_logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.5  # rapid visitor messages within this window get one reply
MAX_JOB_RUNS = 3  # processing runs per job (each run makes up to 6 LLM calls)
STALE_PENDING_SECONDS = 30  # pending this long past run_after: the thread was lost
STALE_RUNNING_SECONDS = 60  # running and unlocked this long: the process died
JOB_MAX_AGE_MINUTES = 30  # older unfinished jobs are given up (visitor likely gone)
CRON_SEARCH_LIMIT = 200
CRON_BATCH_LIMIT = 10  # sessions handed to recovery threads per cron run
CHAIN_LIMIT = 20  # jobs one thread runs back to back for the same session
MAX_JOB_ERROR_LENGTH = 300

# First key of the two-int advisory lock; the second is the discuss.channel id.
LOCK_NAMESPACE = zlib.crc32(b'im_livechat_ai.job') & 0x7FFFFFFF


def _job_cursor(db_name):
    """New cursor for job processing.

    READ COMMITTED (instead of Odoo's REPEATABLE READ) so posting the reply
    does not hit serialization failures when the visitor's request updates
    the same channel/member rows concurrently, and so every statement sees
    the jobs and messages committed by other workers. Odoo resets the level
    when the pooled connection is borrowed again.
    """
    cr = Registry(db_name).cursor()
    cr.connection.set_isolation_level(ISOLATION_LEVEL_READ_COMMITTED)
    return cr


def _job_worker(db_name, job_id, wait=True):
    """Run a job and then the session's newest pending job, in this thread.

    Used by the post-commit thread (wait=True: debounce first) and by the
    recovery cron (wait=False). Never raises.
    """
    threading.current_thread().dbname = db_name
    first = True
    try:
        for _i in range(CHAIN_LIMIT):
            if wait:
                if first:
                    delay = DEBOUNCE_SECONDS
                else:
                    with _job_cursor(db_name) as cr:
                        env = api.Environment(cr, SUPERUSER_ID, {})
                        delay = env['im_livechat.ai.job']._seconds_until_due(job_id)
                if delay > 0:
                    time.sleep(min(delay, DEBOUNCE_SECONDS))
            with _job_cursor(db_name) as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                Job = env['im_livechat.ai.job']
                session_id, ran = Job.browse(job_id)._run_job(commit=True)
                if not ran:
                    # Lock busy: the holder runs the newest pending job when
                    # it is done; the cron is the safety net.
                    return
                next_id = Job._next_pending_job_id(session_id)
            if not next_id or next_id == job_id:
                return
            job_id, first, wait = next_id, False, True
    except Exception:  # noqa: BLE001 - a background thread must never die loudly
        _logger.exception("Live chat AI job worker failed (job %s)", job_id)


def _recovery_worker(db_name, job_ids):
    """Recovery thread started by the cron: the session's stale jobs in order."""
    for job_id in job_ids:
        _job_worker(db_name, job_id, wait=False)


class LivechatAiJob(models.Model):
    """One AI reply to produce for one visitor message (the reply queue)."""
    _name = 'im_livechat.ai.job'
    _description = 'Live Chat AI Reply Job'
    _order = 'id desc'

    discuss_channel_id = fields.Many2one(
        comodel_name='discuss.channel',
        string='Chat Session',
        required=True,
        ondelete='cascade',
        index=True,
        readonly=True,
    )
    livechat_channel_id = fields.Many2one(
        comodel_name='im_livechat.channel',
        string='Livechat Channel',
        ondelete='cascade',
        index=True,
        readonly=True,
    )
    kind = fields.Selection(
        selection=[
            ('reply', 'AI Reply'),
            ('handoff', 'Human Handoff'),
        ],
        string='Type',
        default='reply',
        required=True,
        readonly=True,
        help='AI Reply: answer with the LLM. Human Handoff: the visitor asked '
             'for a human; send the handoff message and notify the operators.',
    )
    message_id = fields.Many2one(
        comodel_name='mail.message',
        string='Trigger Message',
        ondelete='set null',
        readonly=True,
        help='The visitor message that created this job',
    )
    reply_message_id = fields.Many2one(
        comodel_name='mail.message',
        string='Reply Message',
        ondelete='set null',
        readonly=True,
        help='The AI reply (or error message) posted for this job',
    )
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('running', 'Running'),
            ('done', 'Done'),
            ('failed', 'Failed'),
            ('skipped', 'Skipped'),
        ],
        string='Status',
        default='pending',
        required=True,
        index=True,
        readonly=True,
    )
    attempts = fields.Integer(
        string='Runs',
        default=0,
        readonly=True,
        help='How many times processing of this job started (at most 3)',
    )
    llm_calls = fields.Integer(
        string='LLM Calls',
        default=0,
        readonly=True,
        help='LLM API calls made in the last run (one per product lookup round '
             'plus the answer, and at most 2 retries)',
    )
    run_after = fields.Datetime(
        string='Run After',
        readonly=True,
        help='Earliest processing time (debounce of rapid messages)',
    )
    started_at = fields.Datetime(string='Started', readonly=True)
    finished_at = fields.Datetime(string='Finished', readonly=True)
    error = fields.Char(string='Error', readonly=True)

    @api.depends('discuss_channel_id')
    def _compute_display_name(self):
        for job in self:
            job.display_name = f'{job.discuss_channel_id.display_name or "?"} #{job.id}'

    # --- Creation (visitor transaction) ---

    @api.model
    def _create_for_message(self, discuss_channel, message, kind='reply'):
        """Create the job for a visitor message and start it after commit.

        Runs in the visitor's transaction, so the job exists if and only if
        the message was committed.
        """
        job = self.sudo().create({
            'discuss_channel_id': discuss_channel.id,
            'livechat_channel_id': discuss_channel.livechat_channel_id.id,
            'message_id': message.id,
            'kind': kind,
            'run_after': fields.Datetime.now() + timedelta(seconds=DEBOUNCE_SECONDS),
        })
        job._start_after_commit()
        return job

    def _start_after_commit(self):
        """Start one worker thread per job once the transaction is committed."""
        db_name = self.env.cr.dbname
        job_ids = list(self.ids)

        def start_ai_job_threads():
            for job_id in job_ids:
                try:
                    threading.Thread(
                        target=_job_worker,
                        args=(db_name, job_id),
                        name=f'im_livechat_ai.job.{job_id}',
                        daemon=True,
                    ).start()
                except Exception:  # noqa: BLE001 - the cron recovers the job
                    _logger.exception("Could not start live chat AI job %s", job_id)

        start_ai_job_threads.im_livechat_ai_job_ids = job_ids
        self.env.cr.postcommit.add(start_ai_job_threads)

    # --- Per-session lock ---

    @api.model
    def _try_lock_session(self, session_id):
        """Session-level advisory lock: held across the commits of one run,
        released explicitly, and by PostgreSQL if the process dies."""
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_NAMESPACE, session_id))
        return self.env.cr.fetchone()[0]

    @api.model
    def _unlock_session(self, session_id):
        self.env.cr.execute("SELECT pg_advisory_unlock(%s, %s)", (LOCK_NAMESPACE, session_id))

    # --- Processing ---

    @api.model
    def _seconds_until_due(self, job_id):
        job = self.browse(job_id).exists()
        if not job or not job.run_after:
            return 0
        return (job.run_after - fields.Datetime.now()).total_seconds()

    @api.model
    def _next_pending_job_id(self, session_id):
        """Newest pending job of a session (older ones are superseded by it)."""
        job = self.search([
            ('discuss_channel_id', '=', session_id),
            ('state', '=', 'pending'),
        ], order='id desc', limit=1)
        return job.id

    def _run_job(self, commit=True):
        """Process this job if no other job of its session is being processed.

        Args:
            commit (bool): commit after each step (worker thread / cron, on a
                cursor from _job_cursor). False in tests: steps only flush.

        Returns:
            tuple: (discuss.channel id or None, whether the lock was obtained)
        """
        self.ensure_one()
        cr = self.env.cr
        cr.execute("SELECT discuss_channel_id FROM im_livechat_ai_job WHERE id = %s", (self.id,))
        row = cr.fetchone()
        if not row:
            return None, False
        session_id = row[0]
        if not self._try_lock_session(session_id):
            _logger.info("AI job %s: session %s is busy, job left pending", self.id, session_id)
            return session_id, False

        def commit_step():
            if commit:
                cr.commit()
            else:
                self.env.flush_all()
            self.env.invalidate_all()

        try:
            self.env.invalidate_all()
            self._process_locked(commit_step)
        except Exception as e:
            if not commit:
                raise
            _logger.exception("AI job %s failed unexpectedly; the cron will retry it", self.id)
            cr.rollback()
            self.env.invalidate_all()
            try:
                if self.exists() and self.state in ('pending', 'running'):
                    self.write({'error': f'{type(e).__name__}: {e}'[:MAX_JOB_ERROR_LENGTH]})
                    cr.commit()
            except Exception:  # noqa: BLE001
                cr.rollback()
        finally:
            try:
                self._unlock_session(session_id)
                if commit:
                    cr.commit()
            except Exception:  # noqa: BLE001 - never return a pooled connection holding the lock
                _logger.exception("AI job %s: could not release the session lock", self.id)
                cr.rollback()
                cr.execute("SELECT pg_advisory_unlock_all()")
        return session_id, True

    def _process_locked(self, commit_step):
        """Body of a job run; the caller holds the session lock."""
        self.ensure_one()
        if not self.exists() or self.state not in ('pending', 'running'):
            return
        now = fields.Datetime.now()
        if self.create_date and self.create_date < now - timedelta(minutes=JOB_MAX_AGE_MINUTES):
            self._finish('failed', f'超過 {JOB_MAX_AGE_MINUTES} 分鐘仍未處理，已放棄')
            self._notify_failure('訪客的訊息超過 %s 分鐘未能由 AI 處理' % JOB_MAX_AGE_MINUTES,
                                 visitor_told=False)
            commit_step()
            return
        if self.attempts >= MAX_JOB_RUNS:
            # Previous runs died (worker killed, crash) before finishing.
            self._give_up(f'已嘗試處理 {self.attempts} 次仍未完成')
            commit_step()
            return

        # Claim: the run counter is durable before any LLM call.
        self.write({
            'state': 'running',
            'attempts': self.attempts + 1,
            'started_at': now,
            'llm_calls': 0,
            'error': False,
        })
        commit_step()

        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        if self.kind == 'handoff':
            self._process_handoff(commit_step)
            return
        if not channel or not channel.ai_enabled or not channel.ai_model or not channel._llm_is_configured():
            self._finish('skipped', 'AI 已關閉或 LiteLLM 未設定')
            commit_step()
            return
        if self._has_newer_job():
            self._finish('skipped', '有更新的訪客訊息，由最新的工作一併回覆')
            commit_step()
            return
        if self._already_answered():
            self._finish('done', '觸發訊息之後已有 AI 回覆，不重複回答')
            self._supersede_older()
            commit_step()
            return
        if session._ai_is_paused():
            self._finish('skipped', '真人客服處理中，AI 暫停回覆')
            self._supersede_older()
            commit_step()
            return

        messages = session._build_llm_messages(channel)
        if not any(m.get('role') == 'user' for m in messages):
            self._finish('skipped', '對話中沒有訪客訊息')
            commit_step()
            return

        # No transaction stays open during the HTTP calls and retry waits.
        commit_step()
        result = channel._generate_ai_reply(session, messages, before_call=commit_step)
        self.write({'llm_calls': result['calls']})

        if self._has_newer_job():
            # A newer message arrived while the LLM was answering: drop this
            # answer, the newest job answers with the full context.
            self._finish('skipped', '回覆前有新的訪客訊息，由最新的工作一併回覆')
            commit_step()
            return
        if session._ai_is_paused():
            # An operator took over while the LLM was answering.
            self._finish('skipped', '回覆前真人客服已接手，AI 暫停回覆')
            self._supersede_older()
            commit_step()
            return
        if result['reply']:
            reply = channel._create_bot_message(self.env, session, result['reply'])
            self._finish('done', False, reply_message=reply)
        else:
            self._give_up(result['error'] or 'AI 沒有回覆')
        self._supersede_older()
        commit_step()

    def _process_handoff(self, commit_step):
        """Visitor asked for a human: send the handoff message once, then
        notify the operators (the session was paused by the visitor post)."""
        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        if not channel or not channel.ai_enabled:
            self._finish('skipped', 'AI 已關閉')
            commit_step()
            return
        if self._already_answered():
            self._finish('done', '觸發訊息之後已有 AI 回覆，不重複回答')
            self._supersede_older()
            commit_step()
            return
        message = channel._create_bot_message(
            self.env, session, channel.ai_handoff_message or DEFAULT_HANDOFF_MESSAGE,
        )
        self._finish('done', False, reply_message=message)
        self._supersede_older()
        commit_step()
        minutes = channel._ai_pause_minutes()
        text = '訪客在對話「%s」要求真人客服，AI 已暫停回覆 %s 分鐘，請盡快接手。' % (
            session.display_name, minutes)
        self._notify_operators(text, 'AI 客服：訪客要求真人客服', notification_type='warning')
        commit_step()

    def _finish(self, state, error, reply_message=None):
        vals = {
            'state': state,
            'finished_at': fields.Datetime.now(),
            'error': (error or '')[:MAX_JOB_ERROR_LENGTH] or False,
        }
        if reply_message:
            vals['reply_message_id'] = reply_message.id
        self.write(vals)

    def _give_up(self, error):
        """Final failure: tell the visitor (Chinese) and the operators."""
        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        message = channel._create_bot_message(
            self.env, session, channel.ai_error_message or DEFAULT_ERROR_MESSAGE,
        )
        self._finish('failed', error, reply_message=message)
        self._notify_failure(error, visitor_told=True)

    def _has_newer_job(self):
        return bool(self.search_count([
            ('discuss_channel_id', '=', self.discuss_channel_id.id),
            ('id', '>', self.id),
        ], limit=1))

    def _bot_partner_ids(self):
        return list(self.env['im_livechat.channel']._ai_bot_partner_ids(self.livechat_channel_id))

    def _already_answered(self):
        """True when the AI bot posted in the session after the trigger message
        (e.g. the reply was committed but the process died afterwards)."""
        domain = [
            ('model', '=', 'discuss.channel'),
            ('res_id', '=', self.discuss_channel_id.id),
            ('author_id', 'in', self._bot_partner_ids()),
            ('message_type', '=', 'comment'),
        ]
        if self.message_id:
            domain.append(('id', '>', self.message_id.id))
        else:
            domain.append(('create_date', '>=', self.create_date))
        return bool(self.env['mail.message'].search_count(domain, limit=1))

    def _supersede_older(self):
        """Older unfinished jobs of the session are covered by this answer.
        Safe: the session lock is held, so none of them is being processed."""
        older = self.search([
            ('discuss_channel_id', '=', self.discuss_channel_id.id),
            ('id', '<', self.id),
            ('state', 'in', ('pending', 'running')),
        ])
        if older:
            older.write({
                'state': 'skipped',
                'finished_at': fields.Datetime.now(),
                'error': '已由較新的工作一併回覆',
            })

    # --- Operator notification ---

    def _failure_recipients(self):
        """Internal users to warn: the session operator and internal members,
        else the channel operators, else the Live Chat managers."""
        excluded = set(self._bot_partner_ids())
        odoobot = self.env.ref('base.partner_root', raise_if_not_found=False)
        if odoobot:
            excluded.add(odoobot.id)

        def internal(partners):
            return partners.filtered(
                lambda p: p.id not in excluded and any(not u.share for u in p.user_ids)
            )

        session = self.discuss_channel_id
        partners = internal(session.livechat_operator_id | session.channel_member_ids.partner_id)
        if not partners:
            partners = internal(self.livechat_channel_id.user_ids.partner_id)
        if not partners:
            group = self.env.ref('im_livechat.im_livechat_group_manager', raise_if_not_found=False)
            if group:
                partners = internal(group.users.partner_id)
        return partners

    def _notify_failure(self, reason, visitor_told=True):
        """Warn the operators that the AI could not answer (see _notify_operators)."""
        self.ensure_one()
        session = self.discuss_channel_id
        text = 'AI 客服無法回覆對話「%s」（%s）。' % (session.display_name, reason)
        text += '已向訪客送出錯誤訊息，請盡快接手。' if visitor_told else '請盡快接手。'
        self._notify_operators(text, 'AI 客服回覆失敗')

    def _notify_operators(self, text, title, notification_type='danger'):
        """Warn operators in a way they see in Odoo 18:

        - a direct chat message from OdooBot (persistent, unread counter in
          the Discuss systray menu and the Discuss app, also when offline);
        - a sticky toast through the bus (simple_notification) for users who
          are online right now.
        """
        self.ensure_one()
        partners = self._failure_recipients()
        if not partners:
            _logger.warning("AI job %s: no operator could be notified (%s)", self.id, title)
            return
        session = self.discuss_channel_id
        url = '/odoo/discuss?active_id=discuss.channel_%s' % session.id
        body = Markup('%s<br/><a href="%s">開啟對話</a>') % (text, url)
        odoobot = self.env.ref('base.partner_root')
        Channel = self.env['discuss.channel'].with_user(SUPERUSER_ID).sudo()
        for partner in partners:
            try:
                with self.env.cr.savepoint():
                    chat = Channel.channel_get([partner.id], pin=False)
                    chat.message_post(
                        body=body,
                        author_id=odoobot.id,
                        message_type='comment',
                        subtype_xmlid='mail.mt_comment',
                    )
            except Exception:  # noqa: BLE001 - notification must not undo the job result
                _logger.exception("AI job %s: could not notify partner %s", self.id, partner.id)
        try:
            partners._bus_send('simple_notification', {
                'title': title,
                'message': text,
                'sticky': True,
                'type': notification_type,
            })
        except Exception:  # noqa: BLE001
            _logger.exception("AI job %s: could not send the bus notification", self.id)

    # --- Recovery cron ---

    @api.model
    def _recovery_domain(self):
        now = fields.Datetime.now()
        return [
            '|',
            '&', ('state', '=', 'pending'),
            ('run_after', '<', now - timedelta(seconds=STALE_PENDING_SECONDS)),
            '&', ('state', '=', 'running'),
            ('started_at', '<', now - timedelta(seconds=STALE_RUNNING_SECONDS)),
        ]

    @api.model
    def _cron_recover_jobs(self, commit=True):
        """Run jobs whose thread was lost (restart, crash, worker recycled).

        Jobs being processed right now hold their session lock and are left
        alone. Each job still counts its runs (MAX_JOB_RUNS) and is never
        answered twice (see _already_answered).

        Args:
            commit (bool): True: start one recovery thread per session (own
                cursors, commits). False in tests: run the jobs inline in
                the current transaction.

        Returns:
            int: number of jobs handed over
        """
        jobs = self.search(self._recovery_domain(), order='id', limit=CRON_SEARCH_LIMIT)
        if not jobs:
            return 0
        by_session = {}
        for job in jobs:
            by_session.setdefault(job.discuss_channel_id.id, []).append(job)
        batch = list(by_session.values())[:CRON_BATCH_LIMIT]
        for session_jobs in batch:
            for job in session_jobs:
                _logger.info("Recovering live chat AI job %s (state %s, runs %s)",
                             job.id, job.state, job.attempts)
            job_ids = [job.id for job in session_jobs]
            if commit:
                # One job can take several minutes (up to 4 tool/answer
                # calls + 2 retries, 45 s each, plus backoff),
                # longer than the cron watchdog (limit_time_real), so the
                # cron only hands the session over to a thread.
                threading.Thread(
                    target=_recovery_worker,
                    args=(self.env.cr.dbname, job_ids),
                    name=f'im_livechat_ai.recover.{job_ids[0]}',
                    daemon=True,
                ).start()
            else:
                for job_id in job_ids:
                    self._run_chain_in_transaction(job_id)
        tried = sum(len(session_jobs) for session_jobs in batch)
        self.env['ir.cron']._notify_progress(done=tried, remaining=len(jobs) - tried)
        return tried

    @api.model
    def _run_chain_in_transaction(self, job_id):
        """Test counterpart of _job_worker(wait=False): same chaining, no commit."""
        for _i in range(CHAIN_LIMIT):
            session_id, ran = self.browse(job_id)._run_job(commit=False)
            if not ran:
                return
            next_id = self._next_pending_job_id(session_id)
            if not next_id or next_id == job_id:
                return
            job_id = next_id

    @api.model
    def _cleanup_old_jobs(self, days):
        """Delete finished jobs older than the log retention."""
        cutoff = fields.Datetime.now() - timedelta(days=days)
        old = self.search([
            ('state', 'in', ('done', 'failed', 'skipped')),
            ('create_date', '<', cutoff),
        ])
        count = len(old)
        if count:
            old.unlink()
            _logger.info("Cleaned up %s live chat AI jobs older than %s days", count, days)
        return count
