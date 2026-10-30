"""Current employer-posting checks. Transport failure is uncertainty, never closure."""
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit
import re

import requests

from .db import canonical_url, dumps, fingerprint
from .util import now_iso, strip_html


@dataclass
class Check:
    state: str
    evidence: dict = field(default_factory=dict)


def identity_from_url(url):
    parts = urlsplit(canonical_url(url))
    path = parts.path.strip('/').split('/')
    host = parts.hostname or ''
    if host in {'boards.greenhouse.io', 'job-boards.greenhouse.io'} and len(path) >= 3 and path[1] == 'jobs':
        return 'greenhouse', path[0], path[2]
    if host == 'jobs.lever.co' and len(path) >= 2:
        return 'lever', path[0], path[1]
    if host == 'jobs.ashbyhq.com' and len(path) >= 2:
        return 'ashby', path[0], path[1]
    return None


def inspect(job):
    identity = identity_from_url(job.get('apply_url') or job.get('url'))
    if not identity and job.get('source') in {'greenhouse', 'lever', 'ashby'} and job.get('board_slug') and job.get('source_job_id'):
        identity = job['source'], job['board_slug'], str(job['source_job_id'])
    if not identity:
        return Check('uncertain', {'reason': 'No supported authoritative posting endpoint; human verification required'})
    ats, board, jid = identity
    board, jid = quote(board, safe=''), quote(jid, safe='')
    endpoint = {'greenhouse': f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}',
                'lever': f'https://api.lever.co/v0/postings/{board}/{jid}',
                'ashby': f'https://api.ashbyhq.com/posting-api/job-board/{board}'}[ats]
    evidence = {'endpoint': endpoint, 'source_job_id': jid, 'provider': ats}
    try:
        response = requests.get(endpoint, timeout=(5, 20))
        evidence['http_status'] = response.status_code
        if response.status_code in {404, 410}:
            return Check('closed', evidence)
        if response.status_code != 200:
            return Check('uncertain', {**evidence, 'reason': 'Employer endpoint unavailable'})
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('Invalid posting payload')
        if ats == 'ashby':
            jobs = data.get('jobs')
            if not isinstance(jobs, list) or data.get('hasMore') or data.get('nextCursor'):
                raise ValueError('Incomplete published-posting feed')
            matches = [item for item in jobs if isinstance(item, dict) and
                       (str(item.get('id', '')) == jid or
                        any(identity_from_url(item.get(key, '')) == identity for key in ('jobUrl', 'applyUrl')))]
            if not matches:
                return Check('closed', {**evidence, 'reason': 'Absent from complete employer feed'})
            data = matches[0]
            if data.get('isListed') is False:
                return Check('uncertain', {**evidence, 'reason': 'Posting is unlisted; confirm application eligibility'})
        elif str(data.get('id', '')) != jid:
            raise ValueError('Posting identity mismatch')
        title = data.get('title') if ats != 'lever' else data.get('text')
        if not isinstance(title, str) or not title.strip():
            raise ValueError('Missing posting title')
        # An unexpected role is a changed posting, not permission to apply to it.
        normalize = lambda s: re.sub(r'\s+', ' ', s.strip()).casefold()
        if normalize(title) != normalize(job.get('title') or ''):
            return Check('uncertain', {**evidence, 'reason': 'Posting title changed', 'current_title': title})
        description = (strip_html(data.get('content', '')) if ats == 'greenhouse' else
                       data.get('descriptionPlain') or strip_html(data.get('descriptionHtml') or data.get('description', '')))
        if ats == 'lever':
            description = (description or '') + '\n' + '\n'.join(strip_html(item.get('content', '')) for item in data.get('lists', []) if isinstance(item, dict))
        if description:
            evidence['description'] = description
        return Check('open', {**evidence, 'title': title, 'checked_at': now_iso()})
    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        return Check('uncertain', {**evidence, 'reason': type(error).__name__})


def check(ctx, job, stage, checker=None):
    result = (checker or inspect)(job)
    evidence = {**result.evidence, 'stage': stage}
    at = now_iso()
    ctx.db.x('INSERT INTO availability_checks(job_id,at,state,evidence_json) VALUES(?,?,?,?)',
             (job['id'], at, result.state, dumps(evidence)))
    ctx.db.set_job(job['id'], availability=result.state, availability_at=at)
    description = evidence.get('description')
    if result.state == 'open' and description and fingerprint(description) != fingerprint(job.get('description') or ''):
        ctx.db.set_job(job['id'], description=description, description_hash=fingerprint(description),
                       score_key=None, documents_valid=0, status='new')
        ctx.db.x('DELETE FROM application_approvals WHERE job_id=?', (job['id'],))
        ctx.db.record(job['id'], 'posting_changed', {'stage': stage, 'description_hash': fingerprint(description)})
        return Check('uncertain', {**evidence, 'reason': 'Description changed; re-score and regenerate documents'})
    if result.state == 'closed':
        ctx.db.set_job(job['id'], status='closed', reject_reason='employer_posting_closed')
    return result
