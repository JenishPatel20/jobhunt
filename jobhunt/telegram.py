"""Single-user Telegram long polling and durable delivery; no webhook hosting."""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from .apply import hitl
from .util import now_iso


class Telegram:
    def __init__(self, db, profile=None):
        self.db, self.profile = db, profile
        self.token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        self.chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
        self.user = os.environ.get('TELEGRAM_USER_ID', '').strip()

    @property
    def configured(self):
        return bool(self.token and self.chat and self.user)

    def api(self, method, payload):
        response = requests.post(f'https://api.telegram.org/bot{self.token}/{method}', json=payload,
                                 timeout=(5, 40))
        response.raise_for_status()
        body = response.json()
        if not body.get('ok'):
            raise ValueError('Telegram rejected request')
        return body.get('result')

    def flush(self):
        if not self.configured:
            return 0
        count = 0
        for row in self.db.q("SELECT * FROM outbox WHERE state='pending' AND (next_attempt IS NULL OR next_attempt<=?) ORDER BY id LIMIT 30", (now_iso(),)):
            if row['request_id'] and not self.db.one("SELECT id FROM human_requests WHERE id=? AND status='open'", (row['request_id'],)):
                self.db.x("UPDATE outbox SET state='cancelled' WHERE id=?", (row['id'],))
                continue
            try:
                self.api('sendMessage', {'chat_id': self.chat, 'text': row['text'][:4000]})
            except (requests.RequestException, ValueError, TypeError):
                # Never persist exception URLs: they contain the bot token.
                delay = min(3600, 2 ** min(row['attempts'] + 1, 11))
                retry = datetime.now(timezone.utc) + timedelta(seconds=delay)
                self.db.x("UPDATE outbox SET attempts=attempts+1,next_attempt=?,last_error='delivery_failed' WHERE id=?",
                          (retry.isoformat(timespec='seconds'), row['id']))
                continue
            with self.db.conn:
                self.db.conn.execute("UPDATE outbox SET state='sent',sent_at=?,attempts=attempts+1 WHERE id=?", (now_iso(), row['id']))
                if row['request_id']:
                    self.db.conn.execute('UPDATE human_requests SET last_notified_at=? WHERE id=?', (now_iso(), row['request_id']))
            count += 1
        return count

    def reissue(self):
        """Requests remain local truth even when Telegram has discarded old updates."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec='seconds')
        for req in self.db.q("SELECT * FROM human_requests WHERE status='open' AND (last_notified_at IS NULL OR last_notified_at<?)", (cutoff,)):
            if self.db.one("SELECT id FROM outbox WHERE request_id=? AND state='pending'", (req['id'],)):
                continue
            job = self.db.one('SELECT company,title FROM jobs WHERE id=?', (req['job_id'],))
            text = f"[#{req['id']}] Still awaiting you: {job['company']} — {job['title']}\n{req['question']}\nResend your command with request #{req['id']}; a reply sent during extended downtime may not have arrived."
            self.db.x('INSERT INTO outbox(request_id,text) VALUES(?,?)', (req['id'], text))

    def handle(self, update):
        uid = update.get('update_id')
        if not isinstance(uid, int):
            return 'invalid'
        if self.db.one('SELECT 1 FROM telegram_updates WHERE update_id=?', (uid,)):
            return 'duplicate'
        msg = update.get('message') or {}
        valid = (str((msg.get('chat') or {}).get('id')) == self.chat and
                 str((msg.get('from') or {}).get('id')) == self.user and
                 (msg.get('chat') or {}).get('type') == 'private')
        outcome = 'unauthorized'
        if valid:
            if self.profile:
                from .config import load_yaml
                from .profile_store import Profile
                context = self.profile.ctx
                context.profile = load_yaml('profile.yaml', context.root)
                context.answer_bank = load_yaml('answer_bank.yaml', context.root)
                self.profile = Profile(context)
            text = str(msg.get('text') or '').strip()
            reply = msg.get('reply_to_message') or {}
            if not text.startswith('/') and re.match(r'\[#(\d+)\]', reply.get('text', '')):
                rid = re.match(r'\[#(\d+)\]', reply['text']).group(1)
                text = f'/answer {rid} {text}'
            match = re.fullmatch(r'/(answer|done|skip|retry|resume|approve)(?:@\w+)?\s+(\d+)(?:\s+([\s\S]+))?', text)
            if match:
                cmd, rid, answer = match.groups()
                ok = hitl.resolve(self.db, cmd, int(rid), answer or '',
                                  profile_hash=self.profile.fingerprint() if self.profile else None)
                outcome = 'resolved' if ok else 'not_resolved'
            elif text == '/status':
                from .track import digest
                self.db.x('INSERT INTO outbox(text) VALUES(?)', (digest(self.db),))
                outcome = 'status'
            else:
                outcome = 'invalid_command'
            self.db.x('INSERT INTO outbox(text) VALUES(?)',
                      (f'Command result: {outcome}. Use /status, /answer ID text, /approve ID, /resume ID, /skip ID, /done ID receipt, or /retry ID reconciliation evidence.',))
        with self.db.conn:
            self.db.conn.execute('INSERT OR IGNORE INTO telegram_updates VALUES(?,?,?)', (uid, now_iso(), outcome))
            previous = int(self.db.kv_get('telegram_offset', '0'))
            self.db.conn.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', ('telegram_offset', str(max(previous, uid + 1))))
        return outcome

    def poll(self, timeout=25):
        if not self.configured:
            raise ValueError('Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and TELEGRAM_USER_ID for a private chat')
        self.reissue()
        self.flush()
        updates = self.api('getUpdates', {'offset': int(self.db.kv_get('telegram_offset', '0')),
                                         'timeout': timeout, 'allowed_updates': ['message']})
        for update in updates or []:
            self.handle(update)
        self.flush()
        return len(updates or [])
