# -*- coding: utf-8 -*-
"""Durable AI reply jobs for live chat sessions (package C: P1-4..P1-7).

Flow:
  visitor message_post
    -> discuss.channel._notify_thread creates an im_livechat.ai.job in the
       SAME transaction as the message (state pending)
    -> after COMMIT (cr.postcommit) a worker thread starts, waits the
       debounce delay and runs the job with its own cursor (not a daemon
       thread: a prefork worker that recycles itself finishes the reply
       before it exits; the recovery cron is also triggered for the case
       the process dies)
    -> the job runs under a per-session PostgreSQL advisory lock, so only
       one job per discuss.channel is processed at a time, across all
       prefork workers and the cron process
    -> before calling the LLM and again before posting, a job that has a
       newer job in the same session is marked skipped; the newest job
       answers with the full conversation as context. The checks before
       posting run with the session row locked, so a visitor or operator
       post that is being committed is waited for (or retried after the
       reply)
    -> when the lock holder finishes it runs the newest pending job of the
       session (the jobs whose own thread found the lock busy)
  Human handoff (package D, P1-2; P0-1): an operator's post pauses the
  session (discuss.channel.ai_paused_until, reason 'operator') so no job is
  created; a visitor asking for a human creates a 'handoff' job that posts a
  fixed notice (no LLM), notifies the operators and starts a handoff wait
  (reason 'handoff'). The first visitor message during the wait gets a
  'notice' job (fixed waiting notice, no LLM); every visitor message during
  the wait gets a DEFERRED reply job due when the wait ends, run by the
  recovery cron (triggered for that time). If an operator replied meanwhile
  the deferred jobs are closed quietly; otherwise the newest one answers
  everything the visitor wrote. Reply jobs check the pause before calling
  the LLM and again before posting.
  A cron (every 2 minutes) hands jobs left pending/running after a crash or
  restart to recovery threads (one per session), at most MAX_JOB_RUNS runs
  per job, and never answers a job whose trigger message was already in the
  LLM context of a finished reply.
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

from .discuss_channel import PAUSE_HANDOFF
from .im_livechat_channel import (
    DEFAULT_ERROR_MESSAGE,
    DEFAULT_HANDOFF_MESSAGE,
    LLM_MAX_RETRIES,
    LLM_RETRY_BACKOFF,
    MAX_REQUEST_TIMEOUT,
)

_logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.5  # rapid visitor messages within this window get one reply
MAX_JOB_RUNS = 3  # processing runs per job (each run makes up to 3 backend calls)
STALE_PENDING_SECONDS = 30  # pending this long past run_after: the thread was lost
# Running and unlocked this long: the process died. A live run holds the
# session's advisory lock for its whole duration, however long the backend
# takes, and the cron skips locked sessions (_session_lock_held), so this
# does not need to exceed MAX_RUN_SECONDS.
STALE_RUNNING_SECONDS = 60
# The longest one run can legitimately take: every call waiting the longest
# allowed timeout, plus the retry backoff (3 x 300 s + 13 s, about 15 min).
MAX_RUN_SECONDS = (LLM_MAX_RETRIES + 1) * MAX_REQUEST_TIMEOUT + sum(LLM_RETRY_BACKOFF)
JOB_MAX_AGE_MINUTES = 30  # older unfinished jobs are given up (visitor likely gone); > MAX_RUN_SECONDS
CRON_SEARCH_LIMIT = 200
CRON_BATCH_LIMIT = 10  # sessions handed to recovery threads per cron run
CHAIN_LIMIT = 20  # jobs one thread runs back to back for the same session
MAX_JOB_ERROR_LENGTH = 300
SESSION_ROW_LOCK_TIMEOUT = '15s'  # waiting for a visitor/operator transaction to commit
# The recovery cron is triggered this long after a job is due, in case its
# thread died (the cron only takes pending jobs STALE_PENDING_SECONDS late).
CRON_TRIGGER_DELAY_SECONDS = STALE_PENDING_SECONDS + 5
# Deferred jobs: the cron is triggered this long after the end of the wait
# (clock differences between the Odoo processes and PostgreSQL).
DEFERRED_TRIGGER_DELAY_SECONDS = 2
# Settings monitoring (P1-6): unfinished jobs this old, or a recovery cron
# that has not run for this long, are reported.
MONITOR_STALE_MINUTES = 5

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
            ('notice', 'Waiting Notice'),
        ],
        string='Type',
        default='reply',
        required=True,
        readonly=True,
        help='AI Reply: answer through the AI backend. Human Handoff: the visitor asked '
             'for a human; send the handoff message and notify the operators. '
             'Waiting Notice: first visitor message while waiting for a human; '
             'send the waiting notice once (no backend call).',
    )
    deferred = fields.Boolean(
        string='Deferred',
        readonly=True,
        help='Reply to a message written while the AI waited for an operator: run '
             'by the recovery cron when the wait ends (Run After), closed quietly '
             'if an operator replied meanwhile.',
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
        string='Backend Calls',
        default=0,
        readonly=True,
        help='Backend calls made in the last run (one call, plus at most 2 retries)',
    )
    run_after = fields.Datetime(
        string='Run After',
        readonly=True,
        help='Earliest processing time (debounce of rapid messages)',
    )
    started_at = fields.Datetime(string='Started', readonly=True)
    finished_at = fields.Datetime(string='Finished', readonly=True)
    error = fields.Char(string='Error', readonly=True)
    context_message_id = fields.Integer(
        string='Last Message In Context',
        readonly=True,
        help='Highest message id sent to the AI backend as conversation; visitor '
             'messages up to it are covered by this job\'s reply',
    )

    @api.depends('discuss_channel_id')
    def _compute_display_name(self):
        for job in self:
            job.display_name = f'{job.discuss_channel_id.display_name or "?"} #{job.id}'

    # --- Creation (visitor transaction) ---

    @api.model
    def _create_for_message(self, discuss_channel, message, kind='reply', run_after=None):
        """Create the job for a visitor message and start it after commit.

        Runs in the visitor's transaction, so the job exists if and only if
        the message was committed.

        Args:
            run_after (datetime|None): deferred reply (end of a handoff
                wait): no thread is started; the recovery cron is triggered
                for that time and runs it (this works with a separate cron
                process and survives restarts)
        """
        deferred = bool(run_after)
        job = self.sudo().create({
            'discuss_channel_id': discuss_channel.id,
            'livechat_channel_id': discuss_channel.livechat_channel_id.id,
            'message_id': message.id,
            'kind': kind,
            'deferred': deferred,
            'run_after': run_after or fields.Datetime.now() + timedelta(seconds=DEBOUNCE_SECONDS),
        })
        if deferred:
            self._trigger_recovery_cron(job.run_after + timedelta(seconds=DEFERRED_TRIGGER_DELAY_SECONDS))
            return job
        job._start_after_commit()
        # Safety net when the process running the thread dies: wake the
        # recovery cron (cron process) instead of waiting for its interval.
        self._trigger_recovery_cron(job.run_after + timedelta(seconds=CRON_TRIGGER_DELAY_SECONDS))
        return job

    @api.model
    def _trigger_recovery_cron(self, at):
        cron = self.env.ref('im_livechat_ai.ir_cron_recover_ai_jobs', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger(at)

    def _start_after_commit(self):
        """Start one worker thread per job once the transaction is committed.

        Not daemon threads: a prefork worker exits with sys.exit() when it
        recycles itself (memory or request limit, rollout), and Python joins
        non-daemon threads first, so the reply in progress is finished (the
        server's watchdog still kills the process after limit_time_real).
        """
        db_name = self.env.cr.dbname
        job_ids = list(self.ids)

        def start_ai_job_threads():
            for job_id in job_ids:
                try:
                    threading.Thread(
                        target=_job_worker,
                        args=(db_name, job_id),
                        name=f'im_livechat_ai.job.{job_id}',
                        daemon=False,
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

    @api.model
    def _session_lock_held(self, session_id):
        """True when some connection (a job thread) holds the session lock."""
        self.env.cr.execute("""
            SELECT 1 FROM pg_locks
             WHERE locktype = 'advisory' AND granted
               AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
               AND classid = %s AND objid = %s AND objsubid = 2
             LIMIT 1""", (LOCK_NAMESPACE, session_id))
        return bool(self.env.cr.fetchone())

    def _lock_session_row(self):
        """Lock the session row until the next commit, before the final checks
        and the post.

        A visitor or operator post updates this row (last_interest_dt, and
        ai_paused_until for an operator) in its own transaction. If that
        transaction already wrote the row, this waits until it commits, and
        the checks that follow (READ COMMITTED) see its new job or pause.
        A post that comes later waits for the reply to be committed and is
        retried by Odoo (serialization failure), so its message comes after
        the reply and gets its own job.
        """
        cr = self.env.cr
        cr.execute("SET LOCAL lock_timeout = %s", (SESSION_ROW_LOCK_TIMEOUT,))
        cr.execute("SELECT id FROM discuss_channel WHERE id = %s FOR NO KEY UPDATE",
                   (self.discuss_channel_id.id,))
        cr.execute("SET LOCAL lock_timeout = DEFAULT")

    # --- Processing ---

    @api.model
    def _seconds_until_due(self, job_id):
        job = self.browse(job_id).exists()
        if not job or not job.run_after:
            return 0
        return (job.run_after - fields.Datetime.now()).total_seconds()

    @api.model
    def _next_pending_job_id(self, session_id):
        """Newest pending job of a session (older ones are superseded by it),
        except deferred jobs that are not due yet (the cron runs those)."""
        job = self.search([
            ('discuss_channel_id', '=', session_id),
            ('state', '=', 'pending'),
            '|', ('deferred', '=', False), ('run_after', '<=', fields.Datetime.now()),
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
        if self.deferred and self.state == 'pending' and self._defer_again(commit_step, now):
            return
        # A deferred job is as old as its due time (the wait can be long).
        reference = max(self.create_date, self.run_after) if self.deferred and self.run_after else self.create_date
        if reference and reference < now - timedelta(minutes=JOB_MAX_AGE_MINUTES):
            self._finish('failed', f'超過 {JOB_MAX_AGE_MINUTES} 分鐘仍未處理，已放棄')
            self._notify_failure('訪客的訊息超過 %s 分鐘未能由 AI 處理' % JOB_MAX_AGE_MINUTES,
                                 visitor_told=False)
            commit_step()
            return
        if self.attempts >= MAX_JOB_RUNS:
            # Previous runs died (worker killed, crash) before finishing.
            self._finish_exhausted(commit_step)
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

        if self.kind == 'handoff':
            self._process_handoff(commit_step)
            return
        if self.kind == 'notice':
            self._process_notice(commit_step)
            return
        if self._skip_reply(commit_step):
            return

        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        context = session._ai_context_messages(channel)
        conversation = session._ai_conversation(channel, recent_messages=context, resume=self.deferred)
        if not any(e['kind'] == 'visitor' for e in conversation['history']):
            self._finish('skipped', '對話中沒有訪客訊息')
            commit_step()
            return
        # The visitor messages up to this id are answered by this reply.
        self.write({'context_message_id': max(context.ids)})

        # No transaction stays open during the HTTP calls and retry waits:
        # before_call commits right before each request.
        commit_step()
        result = channel._generate_ai_reply(session, conversation, before_call=commit_step,
                                            trigger_message=self.message_id)
        self.write({'llm_calls': result['calls']})

        # Final checks and the post, with the session row locked (see
        # _lock_session_row): committed in one transaction.
        self._lock_session_row()
        if self._has_newer_job():
            # A newer message arrived while the backend was answering: drop
            # this answer, the newest job answers with the full context.
            self._finish('skipped', '回覆前有新的訪客訊息，由最新的工作一併回覆')
            commit_step()
            return
        if session._ai_is_paused():
            # An operator took over while the backend was answering.
            self._finish('skipped', '回覆前真人客服已接手，AI 暫停回覆')
            self._supersede_older()
            commit_step()
            return
        if result['error']:
            self._give_up(result['error'])
        elif result['skip']:
            # n8n answered {"skip": true}: the workflow chose not to reply
            # (or replies later through its own channel).
            self._finish('done', '後端選擇不回覆（skip）')
        elif result['replies']:
            posted = [channel._create_bot_message(self.env, session, text) for text in result['replies']]
            self._finish('done', False, reply_message=posted[0])
        else:
            self._give_up('AI 沒有回覆')
        self._supersede_older()
        commit_step()

    def _skip_reply(self, commit_step):
        """Finish a reply job without calling the LLM when it must not answer.

        Returns:
            bool: True when the job was finished (and committed)
        """
        channel = self.livechat_channel_id
        if not channel or not channel.ai_enabled or not channel._ai_connection_is_ready():
            self._finish('skipped', 'AI 已關閉或後端連線未設定')
        elif self._has_newer_job():
            self._finish('skipped', '有更新的訪客訊息，由最新的工作一併回覆')
        elif self._already_answered():
            self._finish('done', '觸發訊息已由先前的 AI 回覆一併回答，不重複回答')
            self._supersede_older()
        elif self.discuss_channel_id._ai_is_paused():
            self._finish('skipped', '真人客服處理中，AI 暫停回覆')
            self._supersede_older()
        elif self.deferred and self._operator_replied_since_trigger():
            # The wait ended, but an operator answered meanwhile.
            self._finish('skipped', '等候期間真人客服已接手，AI 不補回覆')
            self._supersede_older()
        else:
            return False
        commit_step()
        return True

    def _defer_again(self, commit_step, now):
        """Deferred job run before its time (a chained run, an early cron
        trigger) or whose handoff wait was extended: leave it pending, due at
        the end of the wait, and trigger the cron for then. A wait turned
        into an operator pause is not waited for: the run closes the job.

        Returns:
            bool: True when the job was left pending
        """
        session = self.discuss_channel_id
        due = self.run_after
        if session._ai_waiting_for_human() and session.ai_paused_until > (due or now):
            due = session.ai_paused_until
        if not due or due <= now + timedelta(seconds=1):
            return False
        if due != self.run_after:
            self.write({'run_after': due})
            self._trigger_recovery_cron(due + timedelta(seconds=DEFERRED_TRIGGER_DELAY_SECONDS))
            commit_step()
        return True

    def _operator_replied_since_trigger(self):
        """True when a human operator commented in the session after this
        job's trigger message (they took over during the handoff wait)."""
        session = self.discuss_channel_id
        domain = [
            ('model', '=', 'discuss.channel'),
            ('res_id', '=', session.id),
            ('message_type', '=', 'comment'),
        ]
        if self.message_id:
            domain.append(('id', '>', self.message_id.id))
        visitor = session._ai_visitor_partner()
        for message in self.env['mail.message'].search(domain, order='id'):
            if session._ai_author_kind(message, visitor_partner=visitor) == 'operator':
                return True
        return False

    def _finish_exhausted(self, commit_step):
        """MAX_JOB_RUNS runs died before finishing. A handoff still gets its
        notice (no LLM involved); a reply job that must not answer anyway
        (newer job, AI off, paused, answered) is closed quietly; otherwise
        the visitor gets the error message."""
        if self.kind == 'handoff':
            self._process_handoff(commit_step)
            return
        if self.kind == 'notice':
            self._process_notice(commit_step)
            return
        if self._skip_reply(commit_step):
            return
        self._give_up(f'已嘗試處理 {self.attempts} 次仍未完成')
        commit_step()

    def _process_handoff(self, commit_step):
        """Visitor asked for a human: send the handoff message once, then
        notify the operators (the session was paused by the visitor post).

        The notice and the job state are committed together, so a job that
        is not done has never posted its notice; a reply that covered the
        message does not replace the notice and the operator alert."""
        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        if not channel or not channel.ai_enabled:
            self._finish('skipped', 'AI 已關閉')
            commit_step()
            return
        message = channel._create_bot_message(
            self.env, session, channel.ai_handoff_message or DEFAULT_HANDOFF_MESSAGE,
        )
        self._finish('done', False, reply_message=message)
        self._supersede_older()
        commit_step()
        minutes = channel._ai_wait_minutes()
        text = ('訪客在對話「%s」要求真人客服，AI 會先等候 %s 分鐘；'
                '若沒有人回覆，AI 會繼續回答訪客。請盡快接手。') % (session.display_name, minutes)
        self._notify_operators(text, 'AI 客服：訪客要求真人客服', notification_type='warning')
        commit_step()

    def _process_notice(self, commit_step):
        """First visitor message of a handoff wait: post the waiting notice
        (fixed text with the wait minutes, no LLM). Not sent when the wait
        is over or an operator took over meanwhile."""
        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        if not channel or not channel.ai_enabled:
            self._finish('skipped', 'AI 已關閉')
        elif not session._ai_waiting_for_human():
            self._finish('skipped', '已不在等候真人客服（客服已接手或等候已結束），不送等候通知')
        else:
            message = channel._create_bot_message(self.env, session, channel._ai_pause_notice_text())
            self._finish('done', False, reply_message=message)
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
        """True when a finished reply job of the session had this job's
        trigger message in its LLM context, so its reply covered it.

        A bot message posted after the trigger is not enough: that reply may
        have been produced before the trigger message was visible (posted
        while the reply was being written), and then the visitor's question
        was never answered."""
        if not self.message_id:
            return False
        return bool(self.search_count([
            ('discuss_channel_id', '=', self.discuss_channel_id.id),
            ('id', '!=', self.id),
            ('kind', '=', 'reply'),
            ('state', '=', 'done'),
            ('reply_message_id', '!=', False),
            ('context_message_id', '>=', self.message_id.id),
        ], limit=1))

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
        """Jobs the cron runs: pending jobs whose thread was lost, running
        jobs whose process died, and deferred jobs that are due."""
        now = fields.Datetime.now()
        return [
            '|', '|',
            '&', ('state', '=', 'pending'),
            ('run_after', '<', now - timedelta(seconds=STALE_PENDING_SECONDS)),
            '&', '&', ('state', '=', 'pending'), ('deferred', '=', True),
            ('run_after', '<=', now),
            '&', ('state', '=', 'running'),
            ('started_at', '<', now - timedelta(seconds=STALE_RUNNING_SECONDS)),
        ]

    @api.model
    def _stale_job_count(self, minutes=MONITOR_STALE_MINUTES):
        """AI tab monitoring (P1-6): pending jobs due more than `minutes`
        ago and jobs running for longer than a run can legitimately take
        (`minutes`, at least MAX_RUN_SECONDS plus a minute: a slow backend
        with a long timeout is not a stuck job). Deferred jobs that are not
        due yet are not counted."""
        now = fields.Datetime.now()
        cutoff = now - timedelta(minutes=minutes)
        running_cutoff = now - timedelta(seconds=max(minutes * 60, MAX_RUN_SECONDS + 60))
        return self.sudo().search_count([
            '|',
            '&', ('state', '=', 'pending'), ('run_after', '<', cutoff),
            '&', ('state', '=', 'running'), ('started_at', '<', running_cutoff),
        ])

    @api.model
    def _cron_recover_jobs(self, commit=True):
        """Run jobs whose thread was lost (restart, crash, worker recycled).

        Sessions whose lock is held (a job is being processed right now) are
        left alone, so repeated cron runs do not start duplicate threads.
        Each job still counts its runs (MAX_JOB_RUNS) and is never answered
        twice (see _already_answered). The work is handed to threads, so the
        cron reports no progress (Odoo would re-run it at once otherwise);
        sessions beyond CRON_BATCH_LIMIT are taken on the next runs.

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
        batch = []
        for session_id, session_jobs in by_session.items():
            if self._session_lock_held(session_id):
                continue
            batch.append(session_jobs)
            if len(batch) >= CRON_BATCH_LIMIT:
                break
        for session_jobs in batch:
            for job in session_jobs:
                _logger.info("Recovering live chat AI job %s (state %s, runs %s)",
                             job.id, job.state, job.attempts)
            job_ids = [job.id for job in session_jobs]
            if commit:
                # One job can take minutes (a call + 2 retries, up to
                # 300 s each, plus backoff: MAX_RUN_SECONDS),
                # longer than the cron watchdog (limit_time_real), so the
                # cron only hands the session over to a thread.
                threading.Thread(
                    target=_recovery_worker,
                    args=(self.env.cr.dbname, job_ids),
                    name=f'im_livechat_ai.recover.{job_ids[0]}',
                    daemon=False,
                ).start()
            else:
                for job_id in job_ids:
                    self._run_chain_in_transaction(job_id)
        return sum(len(session_jobs) for session_jobs in batch)

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
    def _cleanup_old_jobs(self, days, livechat_channel_ids=None):
        """Delete finished jobs older than `days` (all jobs, or those of the
        given live chat channels; False in the list: jobs without channel)."""
        cutoff = fields.Datetime.now() - timedelta(days=days)
        domain = [
            ('state', 'in', ('done', 'failed', 'skipped')),
            ('create_date', '<', cutoff),
        ]
        if livechat_channel_ids is not None:
            domain.append(('livechat_channel_id', 'in', list(livechat_channel_ids)))
        old = self.sudo().search(domain)
        count = len(old)
        if count:
            old.unlink()
            _logger.info("Cleaned up %s live chat AI jobs older than %s days", count, days)
        return count
