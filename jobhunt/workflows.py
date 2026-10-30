"""Explicit operator promotion; reviewed live receipts are required, never just exit codes."""
import json

from .db import dumps
from .util import now_iso


def enable(ctx, workflow, evidence):
    required = int(ctx.settings['apply'].get('reviewed_trials_required', 3))
    rows = ctx.db.q("SELECT * FROM applications WHERE workflow=? AND mode='live' AND status='SUBMITTED'", (workflow,))
    reviewed = {r['job_id'] for r in rows if json.loads(r['evidence_json']).get('confirmation', {}).get('individually_approved')}
    if len(reviewed) < required or not evidence.strip():
        raise SystemExit(f'Workflow promotion requires {required} distinct individually reviewed, confirmed live applications; found {len(reviewed)}')
    ctx.db.x('INSERT OR REPLACE INTO workflow_approvals VALUES(?,?,?,?)', (workflow, 1, now_iso(), evidence))
    ctx.db.record(None, 'workflow_enabled', {'workflow': workflow, 'reviewed_jobs': sorted(reviewed), 'evidence': evidence})
    print('Workflow enabled for previously validated form shapes only. New shapes still require your approval.')


def disable(ctx, workflow):
    ctx.db.x('UPDATE workflow_approvals SET enabled=0,at=? WHERE workflow=?', (now_iso(), workflow))
    ctx.db.record(None, 'workflow_disabled', {'workflow': workflow})
    print('Workflow automatic submission disabled.')
