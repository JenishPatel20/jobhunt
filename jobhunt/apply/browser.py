"""Deterministic Greenhouse classic-form workflow with code-enforced submission gates.

Unrecognized forms pause. The model never receives a generic browser click tool.
"""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from ..db import dumps, fingerprint
from ..util import now_iso
from .backends import Result

WORKFLOW = 'greenhouse-classic-v1'


def application_fingerprint(job, profile_hash, shape, values):
    files = {}
    for key in ('resume_path', 'cover_path'):
        path = job.get(key)
        files[key] = hashlib.sha256(Path(path).read_bytes()).hexdigest() if path and Path(path).is_file() else None
    return fingerprint({'job_id': job['id'], 'description_hash': job.get('description_hash'),
                        'profile': profile_hash, 'files': files, 'shape': shape, 'answers': values})


class PlaywrightBackend:
    def __init__(self, settings, ctx=None, profile=None, fixture_url=None):
        self.settings, self.ctx, self.profile = settings, ctx, profile
        self.fixture_url = fixture_url  # Only injected by local browser acceptance tests.
        self.timeout = int(settings['apply'].get('browser_timeout_ms', 20000))

    def apply(self, job, answers, dry_run):
        if not self.ctx or not self.profile:
            return Result('FAILED', 'Browser requires a project context and reviewed profile')
        parts = urlsplit(job['apply_url'])
        fixture = self.fixture_url and job['apply_url'].startswith(self.fixture_url)
        if not fixture and (parts.scheme != 'https' or parts.hostname not in {'boards.greenhouse.io', 'job-boards.greenhouse.io'}):
            return Result('NEEDS_HUMAN', kind='workflow', text='This ATS/form has no validated application adapter; apply manually or skip.')
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return Result('FAILED', 'Install the pdf/browser extra and Playwright Chromium')
        folder = self.ctx.data_dir / 'browser' / job['id']
        folder.mkdir(parents=True, exist_ok=True)
        started_submit = False
        with sync_playwright() as pw:
            try:
                context = pw.chromium.launch_persistent_context(str(folder / 'session'), headless=True)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(self.timeout)
                    # Preparation cannot make a form submission, including implicit Enter submits.
                    permitted = {'submit': False}
                    def route(request_route):
                        request = request_route.request
                        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                            if not permitted['submit'] or self.ctx.db.kv_get('live_enabled') != '1':
                                request_route.abort()
                                return
                            target = urlsplit(request.url).hostname or ''
                            if not fixture and target not in {'boards.greenhouse.io', 'job-boards.greenhouse.io'} and not target.endswith('.greenhouse.io'):
                                request_route.abort()
                                return
                        request_route.continue_()
                    context.route('**/*', route)
                    page.goto(job['apply_url'], wait_until='domcontentloaded')
                    current = urlsplit(page.url)
                    if current.hostname != parts.hostname:
                        return self._pause(folder, page, 'login', 'Application redirected to another host; human verification required.')
                    if page.locator('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],input[type=password]').count():
                        return self._pause(folder, page, 'login', 'Complete the login or verification challenge in the takeover browser.')
                    form = page.locator('form#application_form')
                    if form.count() != 1:
                        return self._pause(folder, page, 'workflow', 'Unrecognized Greenhouse form; this adapter supports the classic application form only.')
                    job = {**job, '_workflow': WORKFLOW}
                    if not fixture:
                        headings = page.locator('h1').all_text_contents()
                        if not any(job['title'].casefold() in h.casefold() for h in headings):
                            return self._pause(folder, page, 'availability', 'The browser job title no longer matches the reviewed posting.')
                    fields, values, answer_values = [], {}, {}
                    controls = form.locator('input,textarea,select')
                    for index in range(controls.count()):
                        control = controls.nth(index)
                        if not control.is_visible() or not control.is_enabled():
                            continue
                        info = control.evaluate('''e => ({id:e.id,name:e.name,tag:e.tagName.toLowerCase(),
                          type:e.type||'text',required:e.required||e.getAttribute('aria-required')==='true',
                          label:[...(e.labels||[])].map(l=>l.innerText).join(' ').trim()||e.getAttribute('aria-label')||'',
                          options:e.tagName==='SELECT'?[...e.options].map(o=>({value:o.value,text:o.text})):[]})''')
                        if info['type'] in {'submit', 'button', 'hidden'}:
                            continue
                        question = re.sub(r'\s+', ' ', info['label']).strip().rstrip('*').strip()
                        required = info['required']
                        fields.append({**info, 'label': question})
                        if not question:
                            return self._pause(folder, page, 'workflow', 'A visible form control has no accessible label; review its mapping.')
                        if info['type'] == 'file':
                            cover = bool(re.search(r'cover', question, re.I))
                            path = job.get('cover_path' if cover else 'resume_path')
                            if cover and not required:
                                continue
                            if cover and required and not path:
                                self.ctx.db.set_job(job['id'], cover_required=1, status='queued', documents_valid=0)
                                return Result('DOCUMENTS_REQUIRED', 'Required cover letter discovered; returned to tailoring')
                            if not path or not Path(path).is_file() or Path(path).suffix.lower() != '.pdf':
                                return Result('FAILED', 'A required validated PDF is missing')
                            control.set_input_files(path)
                            values[question] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                            continue
                        value, origin = self.profile.answer(question, job)
                        if value is None:
                            if required or origin == 'attestation':
                                return self._pause(folder, page, 'attestation' if origin == 'attestation' else 'question', question,
                                                   {'choices': info['options'], 'workflow': WORKFLOW})
                            continue
                        values[question] = value
                        answer_values[question] = value
                        if info['type'] == 'checkbox':
                            if value.casefold() not in {'yes', 'no', 'true', 'false'}:
                                return self._pause(folder, page, 'question', question, {'choices': ['Yes', 'No']})
                            control.set_checked(value.casefold() in {'yes', 'true'})
                        elif info['type'] == 'radio':
                            # Radio groups need explicit option-level mappings; do not guess.
                            return self._pause(folder, page, 'workflow', 'Review this radio-group mapping: ' + question)
                        elif info['tag'] == 'select':
                            matches = [o for o in info['options'] if o['text'].strip().casefold() == value.casefold()]
                            if len(matches) != 1:
                                return self._pause(folder, page, 'question', question, {'choices': info['options']})
                            control.select_option(value=matches[0]['value'])
                        else:
                            if info['type'] == 'number' and not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
                                return self._pause(folder, page, 'question', question,
                                                   {'format': 'numeric', 'workflow': WORKFLOW})
                            if info['type'] == 'date' and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                                return self._pause(folder, page, 'question', question,
                                                   {'format': 'YYYY-MM-DD', 'workflow': WORKFLOW})
                            control.fill(value)
                    shape = fingerprint({'fields': fields,
                                         'visible_text': re.sub(r'\s+', ' ', form.inner_text()).strip(),
                                         'linked_terms': form.locator('a').evaluate_all('(links) => links.map(a => ({text:a.innerText,href:a.href}))')})
                    fp = application_fingerprint(job, self.profile.fingerprint(), shape, values)
                    provenance = json.loads(self.ctx.db.one('SELECT provenance_json FROM applications WHERE id=?', (job['_attempt_id'],))['provenance_json'])
                    provenance.update(profile_hash=self.profile.fingerprint(), entered_fields=values,
                                      shape=shape, fingerprint=fp, workflow=WORKFLOW)
                    self.ctx.db.x('UPDATE applications SET provenance_json=? WHERE id=?', (dumps(provenance), job['_attempt_id']))
                    (folder / 'checkpoint.json').write_text(dumps({'url': page.url, 'workflow': WORKFLOW, 'shape': shape,
                                                                 'fingerprint': fp, 'at': now_iso()}))
                    screenshot = folder / ('prepared-' + fp[:12] + '.png')
                    page.screenshot(path=str(screenshot), full_page=True)
                    if not form.evaluate('(f) => f.checkValidity()'):
                        return self._pause(folder, page, 'workflow', 'Browser validation found incomplete or invalid required fields.')
                    if dry_run:
                        return Result('DRY_RUN_READY', 'Filled and validated form; submission requests blocked in code',
                                      raw={'prepared': {'fingerprint': fp, 'shape': shape, 'screenshot': str(screenshot), 'workflow': WORKFLOW}})
                    approved = self.ctx.db.one('SELECT 1 FROM application_approvals WHERE job_id=? AND fingerprint=?', (job['id'], fp))
                    promoted = self.ctx.db.one('SELECT 1 FROM workflow_approvals WHERE workflow=? AND enabled=1', (WORKFLOW,))
                    known_form = self.ctx.db.kv_get('validated_form:' + WORKFLOW + ':' + shape) == '1'
                    if not approved and not (promoted and known_form):
                        return self._pause(folder, page, 'approval', 'Review the prepared form and documents, then approve this application.',
                                           {'fingerprint': fp, 'shape': shape, 'screenshot': str(screenshot), 'workflow': WORKFLOW})
                    # Permission is checked again at the actual boundary, independently of model output.
                    if self.ctx.db.kv_get('live_enabled') != '1' or not job.get('_submit_permitted'):
                        return Result('FAILED', 'Live submission permission is not enabled')
                    if (self.ctx.db.one('SELECT status FROM jobs WHERE id=?', (job['id'],)) or {}).get('status') != 'applying':
                        return Result('FAILED', 'Application is no longer claimed by this attempt')
                    from .. import availability
                    latest = availability.check(self.ctx, job, 'immediately_before_submit', job.get('_availability_checker'))
                    if latest.state != 'open':
                        if self.ctx.db.one('SELECT status FROM jobs WHERE id=?', (job['id'],))['status'] in {'new', 'closed'}:
                            return Result('RECHECK_REQUIRED', 'Posting changed or closed before submission')
                        return self._pause(folder, page, 'availability', 'Could not revalidate posting immediately before submission.')
                    if not self.profile.reviewed() or application_fingerprint(job, self.profile.fingerprint(), shape, values) != fp:
                        return Result('FAILED', 'Candidate facts or documents changed after preparation')
                    if any(self.profile.answer(question, job)[0] != value for question, value in answer_values.items()):
                        return Result('FAILED', 'An approved answer expired or changed before submission')
                    buttons = form.locator('button[type=submit],input[type=submit]')
                    if buttons.count() != 1:
                        return self._pause(folder, page, 'workflow', 'Final submit control is ambiguous; review the form.')
                    existing_receipt = page.locator('#application_confirmation,[data-testid="application-confirmation"]')
                    if any(existing_receipt.nth(index).is_visible() for index in range(existing_receipt.count())):
                        return self._pause(folder, page, 'interrupted', 'A receipt was already visible before submission; reconcile this application first.')
                    self.ctx.db.x('UPDATE applications SET submit_started_at=? WHERE id=?', (now_iso(), job['_attempt_id']))
                    started_submit = True
                    permitted['submit'] = True
                    buttons.click()
                    confirmation = page.locator('#application_confirmation,[data-testid="application-confirmation"]')
                    confirmation.first.wait_for(state='visible')
                    text = confirmation.first.inner_text().strip()
                    if not re.search(r'thank you for applying|application (?:has been |was )?(?:received|submitted)', text, re.I):
                        return Result('SUBMISSION_UNKNOWN', 'Submit clicked; recognizable receipt not found')
                    receipt = folder / ('receipt-' + str(job['_attempt_id']) + '.png')
                    page.screenshot(path=str(receipt), full_page=True)
                    return Result('SUBMITTED', 'Employer confirmation displayed', raw={'confirmation': {
                        'kind': 'employer_page', 'text': text, 'url': page.url, 'screenshot': str(receipt),
                        'screenshot_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(), 'workflow': WORKFLOW,
                        'shape': shape, 'individually_approved': bool(approved), 'fingerprint': fp}})
                finally:
                    context.close()
            except Exception as error:
                return Result('SUBMISSION_UNKNOWN' if started_submit else 'FAILED',
                              ('Submission may have occurred; reconciliation required. ' if started_submit else 'Browser preparation failed. ') + type(error).__name__)

    def _pause(self, folder, page, kind, text, extra=None):
        details = {'url': page.url, 'workflow': WORKFLOW, 'profile_hash': self.profile.fingerprint(), **(extra or {})}
        (folder / 'checkpoint.json').write_text(dumps(details))
        page.screenshot(path=str(folder / 'paused.png'), full_page=True)
        return Result('NEEDS_HUMAN', kind=kind, text=text, raw={'details': details})


def takeover(ctx, request_id):
    """Operator-controlled visible browser; never called by unattended pipeline code."""
    req = ctx.db.one("SELECT * FROM human_requests WHERE id=? AND status='open'", (request_id,))
    if not req or req['kind'] not in {'login', 'captcha', 'question', 'attestation', 'approval', 'workflow', 'availability'}:
        raise ValueError('No compatible open browser request')
    folder = ctx.data_dir / 'browser' / req['job_id']
    checkpoint = json.loads((folder / 'checkpoint.json').read_text())
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(folder / 'session'), headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(checkpoint['url'])
            input('Complete the account/challenge step. Do not submit unless applying manually. Press Enter to save and close: ')
        finally:
            context.close()
    print('Session saved. Resume with jobhunt resume; if you submitted manually, use jobhunt done with receipt evidence instead.')
