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
import os
import threading
import time
import zlib
from contextlib import contextmanager
from datetime import timedelta

from markupsafe import Markup
from psycopg2.extensions import ISOLATION_LEVEL_READ_COMMITTED

import odoo
from odoo import api, fields, models, SUPERUSER_ID
from odoo.modules.registry import Registry
from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY
from odoo.tools import clean_context, config

from ..tools.backend import exception_frames
from .discuss_channel import PAUSE_HANDOFF, attachment_ids_of, attachment_ids_text
from .im_livechat_channel import (
    ALERT_WINDOW_MINUTES,
    LLM_MAX_RETRIES,
    LLM_RETRY_BACKOFF,
    MAX_REQUEST_TIMEOUT,
)

_logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 1.5  # rapid visitor messages within this window get one reply
# Processing runs per job. Each run makes one backend request with its retries (at most 3 calls), plus at most two
# requests of the file fallback (contract 3.7).
MAX_JOB_RUNS = 3
STALE_PENDING_SECONDS = 30  # pending this long past run_after: the thread was lost
# Running and unlocked this long: the process died. A live run holds the
# session's advisory lock for its whole duration, however long the backend
# takes, and the cron skips locked sessions (_session_lock_held), so this
# does not need to exceed MAX_RUN_SECONDS.
STALE_RUNNING_SECONDS = 60
# A generous bound of one run (18.0.1.x: every call waiting the longest
# allowed timeout, plus the retry backoff, 3 x 300 s + 13 s, about 15 min; since
# 18.0.2.0.0 the timeout bounds the whole answer, so a run is much shorter).
# Kept as it was: the per-channel cap counts a running job this long.
MAX_RUN_SECONDS = (LLM_MAX_RETRIES + 1) * MAX_REQUEST_TIMEOUT + sum(LLM_RETRY_BACKOFF)
JOB_MAX_AGE_MINUTES = 30  # older unfinished jobs are given up (visitor likely gone); > MAX_RUN_SECONDS
CRON_SEARCH_LIMIT = 200
# Backend calls (and the DB connection each in-flight run holds) are bounded per
# live chat channel: an anonymous visitor can open many sessions, so more than
# this many unfinished reply jobs in a channel are not queued. Override with the
# system parameter im_livechat_ai.max_active_jobs (0 disables the limit).
MAX_ACTIVE_JOBS_PER_CHANNEL = 30
# The same bound for all the live chat channels of the database together (an
# abuse bound, whatever the processes and their pools). Override with the
# system parameter im_livechat_ai.max_active_jobs_total (0 disables it). The
# connections are bounded per process by the job slots (see job_slot_count).
MAX_ACTIVE_JOBS_TOTAL = 60
# A job thread waits at most this long for a job slot of its process (it holds
# no database connection while it waits); then the job stays pending and the
# recovery cron runs it. Recovery threads wait less: the cron hands the job out
# again on its next run.
JOB_SLOT_WAIT_SECONDS = 300
RECOVERY_SLOT_WAIT_SECONDS = 60
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
# After a failure the operators of a live chat channel are alerted at most once
# per kind of failure (HTTP 429, HTTP 500, a timeout, ...) in this window, which
# starts at the alert: one alert per flood or outage, not one per visitor
# message; the visitor still gets the error message every time.
FAILURE_ALERT_MINUTES = ALERT_WINDOW_MINUTES
# Note of a reply job skipped by the gate when it ran (contract 3.5)
BLOCKED_AT_RUN_NOTE = 'the AI assistant cannot answer (checked when the job ran): no backend call or no reply'
# Who decided a handoff (im_livechat.ai.job.handoff_source)
HANDOFF_KEYWORD = 'keyword'  # a handoff keyword in the visitor message (no backend call)
HANDOFF_BACKEND = 'backend'  # the backend's answer ([[handoff]] / n8n "handoff": true)

# First key of the two-int advisory lock; the second is the discuss.channel id.
LOCK_NAMESPACE = zlib.crc32(b'im_livechat_ai.job') & 0x7FFFFFFF
# First key of the transaction lock around a failure alert; the second is the
# im_livechat.channel id (see _claim_failure_alert).
ALERT_LOCK_NAMESPACE = zlib.crc32(b'im_livechat_ai.alert') & 0x7FFFFFFF


def job_slot_count():
    """Job threads of this process that may hold a database connection at the
    same time: half of what the process's connection pool (db_maxconn, or
    db_maxconn_gevent in the gevent process) leaves after the cron threads, at
    least 1. The other half stays free for the web requests, as Odoo's own
    threaded server sizes its request threads."""
    def number(key):
        try:
            return int(config.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    pool = (getattr(odoo, 'evented', False) and number('db_maxconn_gevent')) or number('db_maxconn') or 64
    return max(1, (pool - number('max_cron_threads')) // 2)


class _NoJobSlot(Exception):
    """No job slot of this process came free in time."""


# The job slots of this process, made on first use. A forked worker makes its own
# (the pool is per process, and a semaphore copied by fork() is not usable there).
_job_slots = None
_job_slots_lock = threading.Lock()


def _reset_job_slots():
    global _job_slots, _job_slots_lock  # noqa: PLW0603 - per-process state, reset after fork()
    _job_slots = None
    _job_slots_lock = threading.Lock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_job_slots)


@contextmanager
def _job_slot(timeout):
    """Hold one job slot of this process (taken before the job's cursor is
    opened, given back after it is closed). Raises _NoJobSlot after
    `timeout` seconds."""
    global _job_slots  # noqa: PLW0603
    if _job_slots is None:
        with _job_slots_lock:
            if _job_slots is None:
                _job_slots = threading.BoundedSemaphore(job_slot_count())
    slots = _job_slots
    if not slots.acquire(timeout=timeout):
        raise _NoJobSlot()
    try:
        yield
    finally:
        slots.release()


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


def _seconds_until_due(db_name, job_id):
    with _job_cursor(db_name) as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        return env['im_livechat.ai.job']._seconds_until_due(job_id)


def _run_job_step(db_name, job_id):
    """One run of a job on its own cursor: (whether it ran, the session's
    next pending job id)."""
    with _job_cursor(db_name) as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        Job = env['im_livechat.ai.job']
        session_id, ran = Job.browse(job_id)._run_job(commit=True)
        return ran, (Job._next_pending_job_id(session_id) if ran else None)


def _job_worker(db_name, job_id, wait=True):
    """Run a job and then the session's newest pending job, in this thread.

    Used by the post-commit thread (wait=True: debounce first) and by the
    recovery cron (wait=False). Every cursor is opened inside a job slot of
    this process (see job_slot_count): a thread waits for a slot holding no
    connection, and one that waits too long leaves its job pending for the
    recovery cron. Never raises.
    """
    threading.current_thread().dbname = db_name
    timeout = JOB_SLOT_WAIT_SECONDS if wait else RECOVERY_SLOT_WAIT_SECONDS
    first = True
    try:
        for _i in range(CHAIN_LIMIT):
            if wait:
                if first:
                    delay = DEBOUNCE_SECONDS
                else:
                    with _job_slot(timeout):
                        delay = _seconds_until_due(db_name, job_id)
                if delay > 0:
                    time.sleep(min(delay, DEBOUNCE_SECONDS))
            with _job_slot(timeout):
                ran, next_id = _run_job_step(db_name, job_id)
            if not ran:
                # Lock busy: the holder runs the newest pending job when
                # it is done; the cron is the safety net.
                return
            if not next_id or next_id == job_id:
                return
            job_id, first, wait = next_id, False, True
    except _NoJobSlot:
        _logger.info("Live chat AI job %s: no job slot of this process came free in %s s; the job stays pending "
                     "for the recovery cron", job_id, timeout)
    except Exception as e:  # noqa: BLE001 - a background thread must never die loudly
        _logger.error("Live chat AI job worker failed (job %s): %s", job_id, exception_frames(e))


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
    # The handoff record (owner decision 40): who decided it and why.
    handoff_source = fields.Selection(
        selection=[(HANDOFF_KEYWORD, 'Visitor keyword'), (HANDOFF_BACKEND, 'AI backend')],
        string='Handoff Decided By',
        readonly=True,
        help='Visitor keyword: the visitor message contained a handoff keyword (no backend '
             'call). AI backend: the backend asked for a human ([[handoff]] or n8n '
             '"handoff": true).',
    )
    handoff_reason = fields.Char(
        string='Handoff Reason',
        readonly=True,
        help='Reason given by the backend (n8n "handoff_reason"), also sent to the operators.',
    )
    context_message_id = fields.Integer(
        string='Last Message In Context',
        readonly=True,
        help='Highest message id sent to the AI backend as conversation; visitor '
             'messages up to it are covered by this job\'s reply',
    )
    alert_sent = fields.Boolean(
        string='Operators Alerted',
        readonly=True,
        help='This job\'s failure alerted the operators; the same kind of failure of the channel is not '
             'announced again for 10 minutes.',
    )
    # Contract 3.7, the file fallback: the conversation remembers the files the model could not read (left out by a
    # request that then succeeded). Written by the runner only (comma-separated ir.attachment ids); every later
    # request of the conversation reads the union of its jobs' values.
    file_fallback_ids = fields.Char(
        string='Files Sent as Links Only',
        copy=False,
        readonly=True,
        help='Ids of the attachments of this conversation the model could not read: a request with them was '
             'refused, and the same turn sent without them was answered. Every later request of the '
             'conversation sends them as download links only, with a note for the model.',
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
        if kind == 'reply' and not deferred and self._active_jobs_over_limit(discuss_channel.livechat_channel_id):
            # the caller tells the visitor and the operators (im_livechat.channel._ai_handle_full)
            return self.browse()
        if deferred and kind == 'reply':
            # The newest deferred job answers all the messages of the wait:
            # a session keeps at most one pending deferred job.
            self.sudo().search([
                ('discuss_channel_id', '=', discuss_channel.id),
                ('kind', '=', 'reply'), ('deferred', '=', True), ('state', '=', 'pending'),
            ]).write({
                'state': 'skipped',
                'finished_at': fields.Datetime.now(),
                'error': 'answered together with a newer visitor message',
            })
        # Explicit values only (contract 3.5): create() fills every field it is not given from the
        # context's default_* keys (in the visitor's request the context may come from the client) and from
        # the creating user's own ir.default rows (any employee may write them for themselves). So the job is
        # created as the superuser, with the context cleaned and every stored field given.
        job = self.with_user(SUPERUSER_ID).with_context(clean_context(self.env.context)).create({
            'discuss_channel_id': discuss_channel.id,
            'livechat_channel_id': discuss_channel.livechat_channel_id.id,
            'message_id': message.id,
            'kind': kind,
            'deferred': deferred,
            'run_after': run_after or fields.Datetime.now() + timedelta(seconds=DEBOUNCE_SECONDS),
            'state': 'pending',
            'attempts': 0,
            'llm_calls': 0,
            'started_at': False,
            'finished_at': False,
            'error': False,
            'handoff_source': False,
            'handoff_reason': False,
            'context_message_id': 0,
            'reply_message_id': False,
            'alert_sent': False,
            'file_fallback_ids': False,
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
    def _active_job_slots(self, livechat_channel):
        """Reply jobs the channel can still take before the cap.

        Returns:
            int|None: remaining slots (never below 0); None when the limit
                is disabled
        """
        channel_limit = self._active_job_limit('im_livechat_ai.max_active_jobs', MAX_ACTIVE_JOBS_PER_CHANNEL)
        total_limit = self._active_job_limit('im_livechat_ai.max_active_jobs_total', MAX_ACTIVE_JOBS_TOTAL)
        slots = []
        if channel_limit > 0:
            slots.append(channel_limit - self.sudo().search_count(
                [('livechat_channel_id', '=', livechat_channel.id)] + self._active_job_domain()))
        if total_limit > 0:
            slots.append(total_limit - self.sudo().search_count(self._active_job_domain()))
        if not slots:
            return None
        return max(min(slots), 0)

    @api.model
    def _active_job_limit(self, param, default):
        raw = self.env['ir.config_parameter'].sudo().get_param(param)
        try:
            return int(raw) if raw not in (None, False, '') else default
        except ValueError:
            return default

    @api.model
    def _active_job_domain(self):
        """The reply jobs that count toward the caps."""
        now = fields.Datetime.now()
        oldest = now - timedelta(minutes=JOB_MAX_AGE_MINUTES)
        return [
            # Only work that makes (or is about to make) a backend call
            # counts: notice/handoff jobs make none, and a deferred job waits
            # for the end of a handoff while it is pending (it is held back
            # by the same limit when it is due, see _process_locked).
            ('kind', '=', 'reply'),
            '|',
            # A running job counts for as long as one run can last, however
            # old it is by now (it may have been claimed just before the age
            # limit); a crashed run is bounded by the same window. A start in
            # the future never counts.
            '&', '&', ('state', '=', 'running'),
            ('started_at', '>=', now - timedelta(seconds=MAX_RUN_SECONDS + STALE_RUNNING_SECONDS)),
            ('started_at', '<=', now),
            # A live (not deferred) job waiting for its turn counts while it
            # is younger than the age limit (older ones are given up).
            '&', '&', ('state', '=', 'pending'), ('deferred', '=', False),
            ('create_date', '>=', oldest),
        ]

    @api.model
    def _active_jobs_over_limit(self, livechat_channel):
        slots = self._active_job_slots(livechat_channel)
        return slots is not None and slots <= 0

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
                except Exception as e:  # noqa: BLE001 - the cron recovers the job
                    _logger.error("Could not start live chat AI job %s: %s", job_id, exception_frames(e))

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
            _logger.error("AI job %s failed unexpectedly; the cron will retry it: %s", self.id, exception_frames(e))
            cr.rollback()
            self.env.invalidate_all()
            try:
                if self.exists() and self.state in ('pending', 'running'):
                    # the exception type name only, never its text (contract 3.9)
                    self.write({'error': f'unexpected error ({type(e).__name__})'[:MAX_JOB_ERROR_LENGTH]})
                    cr.commit()
            except Exception:  # noqa: BLE001
                cr.rollback()
        finally:
            try:
                self._unlock_session(session_id)
                if commit:
                    cr.commit()
            except Exception as e:  # noqa: BLE001 - never return a pooled connection holding the lock
                _logger.error("AI job %s: could not release the session lock: %s", self.id, exception_frames(e))
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
            note = f'not processed within {JOB_MAX_AGE_MINUTES} minutes: given up'
            self._finish('failed', note)
            if self._claim_failure_alert(note):
                # after an outage the cron gives up the backlog: one alert per channel, not one per job (the
                # recovery threads of one channel claim the alert one after the other)
                self._notify_failure(
                    lambda env: env._('the visitor message was not processed by the AI within %(minutes)s minutes',
                                      minutes=JOB_MAX_AGE_MINUTES),
                    visitor_told=False)
            commit_step()
            return
        if self.attempts >= MAX_JOB_RUNS:
            # Previous runs died (worker killed, crash) before finishing.
            self._finish_exhausted(commit_step)
            return

        if (self.deferred and self.kind == 'reply' and self.state == 'pending'
                and not self._skip_decision()
                and self._hold_deferred_when_channel_full(commit_step)):
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
            self._finish('skipped', 'no visitor message in the conversation')
            commit_step()
            return
        # The visitor messages up to this id are answered by this reply.
        self.write({'context_message_id': max(context.ids)})

        # No transaction stays open during the HTTP calls and retry waits:
        # before_call commits right before each request.
        commit_step()
        result = channel._generate_ai_reply(session, conversation, before_call=commit_step,
                                            trigger_message=self.message_id, job=self)
        self.write({'llm_calls': result['calls']})
        if result['left_out']:
            # Contract 3.7: a request without these files succeeded. The conversation remembers them, committed
            # now, before the post, so that a run of this job after a crash reads them too.
            self._remember_file_fallback(result['left_out'])
            commit_step()

        # Final checks and the post, with the session row locked (see
        # _lock_session_row): committed in one transaction.
        self._lock_session_row()
        if self._has_newer_job():
            # A newer message arrived while the backend was answering: drop
            # this answer, the newest job answers with the full context.
            self._finish('skipped', 'a newer visitor message arrived before the reply: the newest job answers')
            commit_step()
            return
        if session._ai_is_paused():
            # An operator took over while the backend was answering.
            self._finish('skipped', 'an operator took over before the reply: the AI is paused')
            self._supersede_older()
            commit_step()
            return
        # Contract 3.5: the gate again before posting: an administrator may have changed the connection or
        # the assistant, or a manager turned the AI off, while the backend was answering.
        self.env.invalidate_all()
        if not channel.ai_enabled:
            self._finish('skipped', 'the AI was turned off before the reply: nothing posted')
            commit_step()
            return
        if not channel._ai_connection_is_ready():
            self._finish('skipped', BLOCKED_AT_RUN_NOTE)
            channel._ai_handle_blocked(session, self.message_id)
            commit_step()
            return
        if result['error']:
            self._give_up(result['error'], result=result, commit_step=commit_step)
        elif result['handoff']:
            # Owner decision 40: the backend asked for a human. Its text is
            # posted first, then the same handoff as for a keyword.
            posted = [channel._create_bot_message(self.env, session, text, assistant=result.get('assistant'))
                      for text in result['replies']]
            self._count_usage(result, ok=True)
            self._process_backend_handoff(commit_step, posted, result['handoff_reason'])
            return
        elif result['skip']:
            # n8n answered {"skip": true}: the workflow chose not to reply
            # (or replies later through its own channel).
            self._finish('done', 'the backend chose not to reply (skip)')
        elif result['replies']:
            posted = [channel._create_bot_message(self.env, session, text, assistant=result.get('assistant'))
                      for text in result['replies']]
            self._count_usage(result, ok=True)
            self._finish('done', False, reply_message=posted[0])
        else:
            self._give_up('the AI gave no answer', result=result,
                          reason=lambda env: env._('the AI gave no answer'), commit_step=commit_step)
        self._supersede_older()
        commit_step()

    def _skip_decision(self):
        """Why a reply job must not call the LLM (no change is made).

        Returns:
            tuple|None: (state, note, supersede_older); None when it answers
        """
        channel = self.livechat_channel_id
        if not channel or not channel.ai_enabled:
            return 'skipped', 'the AI is off', False
        if not channel._ai_connection_is_ready():
            # contract 3.5: the gate applies again when the job runs (the connection or the assistant may have
            # changed while it waited); _skip_reply gives the notice
            return 'skipped', BLOCKED_AT_RUN_NOTE, False
        if self._has_newer_job():
            return 'skipped', 'a newer visitor message: the newest job answers', False
        if self._already_answered():
            return 'done', 'the trigger message was answered by an earlier AI reply', True
        if self.discuss_channel_id._ai_is_paused():
            return 'skipped', 'an operator is handling the conversation: the AI is paused', True
        if self.deferred and self._operator_replied_since_trigger():
            # The wait ended, but an operator answered meanwhile.
            return 'skipped', 'an operator took over during the wait: no reply after the wait', True
        return None

    def _skip_reply(self, commit_step):
        """Finish a reply job without calling the LLM when it must not answer.

        Returns:
            bool: True when the job was finished (and committed)
        """
        decision = self._skip_decision()
        if not decision:
            return False
        state, note, supersede = decision
        self._finish(state, note)
        if supersede:
            self._supersede_older()
        if note == BLOCKED_AT_RUN_NOTE:
            # one notice per blocked period for the person, one alert for the operators (as at the trigger)
            self.livechat_channel_id._ai_handle_blocked(self.discuss_channel_id, self.message_id)
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

    def _hold_deferred_when_channel_full(self, commit_step):
        """A due deferred reply makes a backend call like a live one: while
        the channel already has the maximum of unfinished reply jobs, leave it
        pending and let the recovery cron try again shortly (it is not
        dropped; the usual age limit still applies).

        The channel lock is held until the claim is committed, so jobs of
        different sessions started together cannot all pass the check.

        Returns:
            bool: True when the job was left pending
        """
        channel = self.livechat_channel_id
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_NAMESPACE, -channel.id))
        if not self._active_jobs_over_limit(channel):
            return False
        _logger.info("AI job %s waits: live chat channel %s already has too many unfinished jobs",
                     self.id, channel.id)
        self._trigger_recovery_cron(fields.Datetime.now() + timedelta(seconds=CRON_TRIGGER_DELAY_SECONDS))
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
        bot_ids = session._ai_bot_ids()
        visitor = session._ai_visitor_partner(bot_ids=bot_ids)
        for message in self.env['mail.message'].search(domain, order='id'):
            if session._ai_author_kind(message, visitor_partner=visitor, bot_ids=bot_ids) == 'operator':
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
        runs = self.attempts
        self._give_up(f'{runs} runs did not finish',
                      reason=lambda env: env._('%(runs)s runs did not finish', runs=runs), commit_step=commit_step)
        commit_step()

    def _visitor_lang(self):
        """Language of the visitor of this job's session ('' when unknown)."""
        return self.discuss_channel_id._ai_visitor_lang() if self.discuss_channel_id else ''

    def _process_handoff(self, commit_step):
        """Visitor asked for a human: send the handoff message once, then
        notify the operators (the session was paused by the visitor post).

        The notice and the job state are committed together, so a job that
        is not done has never posted its notice; a reply that covered the
        message does not replace the notice and the operator alert."""
        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        if not channel or not channel.ai_enabled:
            self._finish('skipped', 'the AI is off')
            commit_step()
            return
        message = channel._create_bot_message(self.env, session,
                                              channel._ai_handoff_message_text(self._visitor_lang()))
        self.write({'handoff_source': HANDOFF_KEYWORD})
        self._finish('done', False, reply_message=message)
        self._supersede_older()
        commit_step()
        minutes = channel._ai_wait_minutes()
        name = session.display_name

        def build(env):
            return (env._('The visitor in conversation "%(session)s" asked for a human. The AI waits %(minutes)s '
                          'minutes; if nobody replies, the AI answers the visitor again. Please take over soon.',
                          session=name, minutes=minutes),
                    env._('Live Chat AI: the visitor asked for a human'))
        self._notify_operators(build, notification_type='warning')
        commit_step()

    def _process_backend_handoff(self, commit_step, posted, reason=None):
        """The backend's answer asked for a human ([[handoff]] or n8n
        "handoff": true) and its text, if any, was posted as `posted`; the
        caller holds the session row lock and checked that no operator wrote
        meanwhile. Without text the channel's handoff message is posted.
        Then the session waits for an operator (as after a handoff keyword),
        the job records the handoff (source backend, reason) and the
        operators are notified, with the reason when n8n gave one."""
        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        if not posted:
            posted = [channel._create_bot_message(self.env, session,
                                                  channel._ai_handoff_message_text(self._visitor_lang()))]
        minutes = channel._ai_wait_minutes()
        session._ai_pause(minutes=minutes, reason=PAUSE_HANDOFF)
        self.write({'handoff_source': HANDOFF_BACKEND, 'handoff_reason': reason or False})
        self._finish('done', False, reply_message=posted[0])
        self._supersede_older()
        commit_step()
        name = session.display_name

        def build(env):
            if reason:
                text = env._('The AI found that the visitor in conversation "%(session)s" wants a human '
                             '(%(reason)s). The AI waits %(minutes)s minutes; if nobody replies, the AI answers the '
                             'visitor again. Please take over soon.', session=name, reason=reason, minutes=minutes)
            else:
                text = env._('The AI found that the visitor in conversation "%(session)s" wants a human. The AI '
                             'waits %(minutes)s minutes; if nobody replies, the AI answers the visitor again. Please '
                             'take over soon.', session=name, minutes=minutes)
            return text, env._('Live Chat AI: the visitor asked for a human')
        self._notify_operators(build, notification_type='warning')
        commit_step()

    def _process_notice(self, commit_step):
        """First visitor message of a handoff wait: post the waiting notice
        (fixed text with the wait minutes, no LLM). Not sent when the wait
        is over or an operator took over meanwhile."""
        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        if not channel or not channel.ai_enabled:
            self._finish('skipped', 'the AI is off')
        elif not session._ai_waiting_for_human():
            self._finish('skipped', 'not waiting for an operator any more (taken over or the wait ended): '
                                    'no waiting notice')
        else:
            message = channel._create_bot_message(self.env, session,
                                                  channel._ai_pause_notice_text(self._visitor_lang()))
            self._finish('done', False, reply_message=message)
        commit_step()

    def action_forget_link_only_attachments(self):
        """Forget the files the conversations of these jobs send as links only (contract 3.7: the memory lasts as
        long as the conversation; changing the connection or the model does not clear it). After a switch to a
        model that reads such files, a system administrator clears it here, for every job of each conversation:
        the next request sends them as file parts again (and a fallback remembers them again when they are
        refused). The jobs are written with sudo: system administrators may read and delete jobs, not write them."""
        self.env['im_livechat.channel']._check_system_admin()
        sessions = self.sudo().discuss_channel_id
        jobs = self.sudo().search([('discuss_channel_id', 'in', sessions.ids), ('file_fallback_ids', '!=', False)])
        jobs.write({'file_fallback_ids': False})
        _logger.info("Live chat AI jobs %s: the files sent as links only of conversations %s are forgotten (user %s)",
                     jobs.ids, sessions.ids, self.env.uid)
        return True

    def _remember_file_fallback(self, attachment_ids):
        """Remember the attachments a successful request of the file fallback left out (contract 3.7): every later
        request of the conversation, a later run of this job included, sends them as links with the fallback note
        where they would be file parts (see discuss.channel._ai_file_fallback_ids). The runner commits it before it
        posts the answer."""
        self.ensure_one()
        known = attachment_ids_of(self.file_fallback_ids)
        ids = known | set(attachment_ids)
        if ids != known:
            self.write({'file_fallback_ids': attachment_ids_text(ids)})

    def _finish(self, state, error, reply_message=None):
        vals = {
            'state': state,
            'finished_at': fields.Datetime.now(),
            'error': (error or '')[:MAX_JOB_ERROR_LENGTH] or False,
        }
        if reply_message:
            vals['reply_message_id'] = reply_message.id
        self.write(vals)

    def _count_usage(self, result, ok=True):
        """Count one answer in the monthly usage (in the posting transaction)."""
        assistant = result.get('assistant') if result else None
        connection = result.get('connection') if result else None
        if not assistant or not connection:
            assistant = self.livechat_channel_id._ai_assistant() if self.livechat_channel_id else None
            connection = assistant.connection_id if assistant else None
        if not assistant or not connection:
            return
        prompt, completion = (result or {}).get('usage') or (0, 0)
        self.env['im_livechat.ai.usage.month'].sudo()._add(
            assistant, connection, livechat_channel=self.livechat_channel_id, ok=ok,
            prompt_tokens=prompt if ok else 0, completion_tokens=completion if ok else 0)

    def _give_up(self, error, result=None, reason=None, commit_step=None):
        """Final failure: tell the visitor (the AI assistant's error message, in
        their language) and the operators; counted as a failed reply.

        ``error``: the job's note; ``reason``: a function (env) -> the reason
        in that env's language for the operators (default: from the backend
        error of ``result``, else ``error``). ``commit_step``: the error message,
        the job state and the usage count are committed before the operator
        alerts, so failing jobs of a channel do not wait on the usage row (and
        on each other's session rows) while alerts are sent."""
        channel = self.livechat_channel_id
        session = self.discuss_channel_id
        message = channel._create_bot_message(self.env, session, channel._ai_error_text(self._visitor_lang()))
        self._finish('failed', error, reply_message=message)
        self._count_usage(result, ok=False)
        if commit_step:
            commit_step()
        if not self._claim_failure_alert(error):
            _logger.info("AI job %s: live chat channel %s failed the same way in the last %s minutes; its "
                         "operators were alerted then", self.id, channel.id, FAILURE_ALERT_MINUTES)
            return
        exc = (result or {}).get('error_exc')
        if reason is None and exc is not None:
            def reason(env):
                return channel.with_env(env)._short_llm_error(exc)
        self._notify_failure(reason or error, visitor_told=True)

    @staticmethod
    def _failure_kind(error):
        """What makes two failures the same for the operator alert: the part of
        the job's note before the first colon ('HTTP 429', 'HTTP 500'), or the
        whole note (fixed sentences with the exception type name)."""
        error = (error or '').strip()
        return error.split(':', 1)[0].strip() if ':' in error else error

    def _failure_alert_sent_recently(self, error):
        """True when another job of the same live chat channel alerted the
        operators about the same kind of failure in the last
        FAILURE_ALERT_MINUTES (the window starts at that alert, not at the
        last failure)."""
        kind = self._failure_kind(error)
        if not kind or not self.livechat_channel_id:
            return False
        since = fields.Datetime.now() - timedelta(minutes=FAILURE_ALERT_MINUTES)
        alerted = self.search([
            ('livechat_channel_id', '=', self.livechat_channel_id.id),
            ('id', '!=', self.id),
            ('alert_sent', '=', True),
            ('finished_at', '>=', since),
            ('error', '=like', kind + '%'),
        ], order='id desc', limit=50)
        return any(self._failure_kind(job.error) == kind for job in alerted)

    def _claim_failure_alert(self, error):
        """Whether this failed job alerts the operators: one alert per kind of
        failure and live chat channel in FAILURE_ALERT_MINUTES. The check and
        the mark (alert_sent) run under a transaction lock of the channel, so
        jobs that fail together in several threads (READ COMMITTED) alert once:
        the next one waits for the first one's commit and then sees its mark."""
        self.ensure_one()
        if not self._failure_kind(error) or not self.livechat_channel_id:
            return True
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                            (ALERT_LOCK_NAMESPACE, self.livechat_channel_id.id))
        if self._failure_alert_sent_recently(error):
            return False
        self.write({'alert_sent': True})
        self.flush_recordset(['alert_sent'])
        return True

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
                'error': 'covered by a newer job',
            })

    # --- Operator notification ---

    @api.model
    def _ai_recipients(self, session, livechat_channel):
        """Internal users to warn: the session operator and internal members,
        else the channel operators, else the Live Chat managers."""
        excluded = set(self.env['im_livechat.channel']._ai_bot_partner_ids(livechat_channel))
        odoobot = self.env.ref('base.partner_root', raise_if_not_found=False)
        if odoobot:
            excluded.add(odoobot.id)

        def internal(partners):
            return partners.filtered(
                lambda p: p.id not in excluded and any(not u.share for u in p.user_ids)
            )

        session = session.sudo()
        partners = internal(session.livechat_operator_id | session.channel_member_ids.partner_id)
        if not partners and livechat_channel:
            partners = internal(livechat_channel.sudo().user_ids.partner_id)
        if not partners:
            group = self.env.ref('im_livechat.im_livechat_group_manager', raise_if_not_found=False)
            if group:
                partners = internal(group.sudo().users.partner_id)
        return partners

    def _failure_recipients(self):
        self.ensure_one()
        return self._ai_recipients(self.discuss_channel_id, self.livechat_channel_id)

    def _notify_failure(self, reason, visitor_told=True):
        """Warn the operators that the AI could not answer (see _ai_notify).
        ``reason``: a short text, or a function (env) -> text in that env's language."""
        self.ensure_one()
        name = self.discuss_channel_id.display_name

        def build(env):
            why = reason(env) if callable(reason) else reason
            if visitor_told:
                text = env._('The Live Chat AI could not answer conversation "%(session)s" (%(reason)s). The '
                             'visitor got the error message; please take over soon.', session=name, reason=why)
            else:
                text = env._('The Live Chat AI could not answer conversation "%(session)s" (%(reason)s). '
                             'Please take over soon.', session=name, reason=why)
            text = '%s %s' % (text, env._('Other conversations of this channel that fail the same way in the next '
                                          '%(minutes)s minutes are not announced again.',
                                          minutes=FAILURE_ALERT_MINUTES))
            return text, env._('Live Chat AI: no reply')
        self._notify_operators(build)

    def _notify_operators(self, build, notification_type='danger'):
        self.ensure_one()
        self._ai_notify(self.discuss_channel_id, self.livechat_channel_id, build,
                        notification_type=notification_type, ref='AI job %s' % self.id)

    @api.model
    def _notify_blocked(self, livechat_channel, session):
        """Tell the operators once per blocked period that the AI of
        `livechat_channel` is on but cannot answer (see _ai_notify). Returns
        whether anybody was told."""
        name = session.sudo().display_name
        channel_name = livechat_channel.sudo().name or ''

        def build(env):
            reasons = ' '.join(livechat_channel.with_env(env)._ai_problems())
            return (env._('The AI of the Live Chat channel "%(channel)s" is on but cannot answer (conversation '
                          '"%(session)s"): %(reason)s No reply job is queued until this is fixed; please take '
                          'over the visitors meanwhile. A system administrator can see why on the AI Assistant tab '
                          'of the channel.', channel=channel_name, session=name, reason=reasons),
                    env._('Live Chat AI: cannot answer'))
        return self._ai_notify(session, livechat_channel, build, ref='live chat channel %s' % livechat_channel.id)

    @api.model
    def _notify_full(self, livechat_channel, session):
        """Tell the operators once per period that the cap of unfinished reply
        jobs refused new visitor messages of `livechat_channel` (see _ai_notify).
        Returns whether anybody was told."""
        name = session.sudo().display_name
        channel_name = livechat_channel.sudo().name or ''

        def build(env):
            return (env._('The Live Chat AI has too many unfinished replies (Live Chat channel "%(channel)s", '
                          'conversation "%(session)s"): new visitor messages get no AI answer until replies '
                          'finish. Please take over the visitors meanwhile.', channel=channel_name, session=name),
                    env._('Live Chat AI: cannot answer'))
        return self._ai_notify(session, livechat_channel, build, ref='live chat channel %s' % livechat_channel.id)

    @api.model
    def _ai_notify(self, session, livechat_channel, build, notification_type='danger', ref=''):
        """Warn operators in a way they see in Odoo 18, each in their own language:

        - a direct chat message from OdooBot (persistent, unread counter in
          the Discuss systray menu and the Discuss app, also when offline);
        - a sticky toast through the bus (simple_notification) for users who
          are online right now.

        ``build(env)`` returns (text, title) in the language of ``env``.
        Returns whether at least one operator got the chat message or the
        notification.
        """
        partners = self._ai_recipients(session, livechat_channel)
        if not partners:
            _logger.warning("%s: no operator could be notified", ref)
            return False
        told = False
        Assistant = self.env['im_livechat.ai.assistant']
        url = '/odoo/discuss?active_id=discuss.channel_%s' % session.id
        odoobot = self.env.ref('base.partner_root')
        Channel = self.env['discuss.channel'].with_user(SUPERUSER_ID).sudo().with_context(
            clean_context(self.env.context))
        by_lang = {}
        for partner in partners:
            by_lang.setdefault(Assistant._installed_lang(partner.lang), self.env['res.partner'])
            by_lang[Assistant._installed_lang(partner.lang)] |= partner
        for lang, group in by_lang.items():
            env = self.with_context(lang=lang).env
            text, title = build(env)
            body = Markup('%s<br/><a href="%s">%s</a>') % (text, url, env._('Open the conversation'))
            for partner in group:
                try:
                    with self.env.cr.savepoint():
                        chat = Channel.channel_get([partner.id], pin=False)
                        chat.message_post(
                            body=body,
                            author_id=odoobot.id,
                            message_type='comment',
                            subtype_xmlid='mail.mt_comment',
                        )
                    told = True
                except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
                    raise  # the caller's transaction must be retried (contract 3.5 step 1)
                except Exception as e:  # noqa: BLE001 - notification must not undo the job result
                    _logger.error("%s: could not notify partner %s: %s", ref, partner.id, exception_frames(e))
            try:
                group._bus_send('simple_notification', {
                    'title': title,
                    'message': text,
                    'sticky': True,
                    'type': notification_type,
                })
                told = True
            except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
                raise
            except Exception as e:  # noqa: BLE001
                _logger.error("%s: could not send the bus notification: %s", ref, exception_frames(e))
        return told

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
        slots = {}
        for session_id, session_jobs in by_session.items():
            if self._session_lock_held(session_id):
                continue
            if (all(job.deferred and job.state == 'pending' for job in session_jobs)
                    and not all(job._skip_decision() for job in session_jobs)):
                # Due deferred replies start only while the channel has room,
                # so they do not take the batch from other sessions' jobs.
                channel = session_jobs[0].livechat_channel_id
                if channel.id not in slots:
                    slots[channel.id] = self._active_job_slots(channel)
                if slots[channel.id] is not None:
                    if slots[channel.id] <= 0:
                        continue
                    slots[channel.id] -= 1
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
        given live chat channels; False in the list: jobs without channel).

        A job that remembers files the model could not read (file_fallback_ids)
        is kept: the memory lasts as long as the conversation (contract 3.7),
        and the job is deleted with it. A system administrator clears the
        memory of a conversation with "Forget Files Sent as Links" on one of its
        reply jobs (action_forget_link_only_attachments)."""
        cutoff = fields.Datetime.now() - timedelta(days=days)
        domain = [
            ('state', 'in', ('done', 'failed', 'skipped')),
            ('create_date', '<', cutoff),
            ('file_fallback_ids', '=', False),
        ]
        if livechat_channel_ids is not None:
            domain.append(('livechat_channel_id', 'in', list(livechat_channel_ids)))
        old = self.sudo().search(domain)
        count = len(old)
        if count:
            old.unlink()
            _logger.info("Cleaned up %s live chat AI jobs older than %s days", count, days)
        return count
