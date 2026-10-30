"""Explicit provider health check; sends no candidate or job data."""
from .providers import make_provider
from .llm import LLMError


def run(ctx):
    try:
        provider = make_provider(ctx.db, ctx.settings)
        answer = provider.text('Return only OK. Do not invoke tools.', 'Connectivity check: reply OK.')
    except LLMError as error:
        print('Provider check:', error)
        return False
    print(f'Subscription provider responded ({len(answer)} characters); no API billing fallback is configured.')
    return True
