"""Explainable role restrictions, separate from fit and historical employer signals."""
import re


def evaluate(ctx, job, rules):
    text = job.get('description') or ''
    evidence, restrictions = [], []
    sponsorship = 'unknown'
    for sentence in re.split(r'\n|(?<=[.!?])\s+(?=[A-Z])', text):
        sentence = sentence.strip()
        if not sentence:
            continue
        no_citizenship = re.search(r'(citizenship|citizen)[^.]{0,30}(not required|is not required)|'
                                  r'(do not|does not|not) require[^.]{0,30}(citizenship|citizen)', sentence, re.I)
        if any(p.search(sentence) for p in rules.nospons):
            sponsorship = 'negative'
            restrictions.append({'kind': 'sponsorship', 'quote': sentence})
        elif sponsorship != 'negative' and any(p.search(sentence) for p in rules.pos):
            sponsorship = 'positive'
            evidence.append({'kind': 'sponsorship', 'quote': sentence})
        if not no_citizenship and any(p.search(sentence) for p in rules.citizen):
            # A mere mention or optional/preferred clearance is not a proven restriction.
            if re.search(r'\b(must|required|requires|requirement|only|mandatory|eligible|eligibility)\b', sentence, re.I) and not re.search(r'not required|no .* required|preferred|optional', sentence, re.I):
                restrictions.append({'kind': 'citizenship_or_restriction', 'quote': sentence})
            else:
                evidence.append({'kind': 'restriction_needs_review', 'quote': sentence})
    needs_sponsorship = ctx.profile.get('work_authorization', {}).get('needs_h1b_sponsorship')
    incompatible = [r for r in restrictions if r['kind'] != 'sponsorship' or needs_sponsorship is not False]
    return {'state': 'incompatible' if incompatible else 'review' if any(e['kind'] == 'restriction_needs_review' for e in evidence) else 'eligible_or_unknown',
            'sponsorship': sponsorship, 'evidence': evidence + restrictions,
            'future_sponsorship_required': needs_sponsorship,
            'authorization': dict(ctx.profile.get('work_authorization', {}))}
