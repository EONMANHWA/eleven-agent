"""Small-host safeguards; no credential logging and no durable browser storage."""
import asyncio
import os
import time
from pathlib import Path

LOW_MEMORY = os.getenv('LOW_MEMORY_MODE', '1') == '1'


def memory_usage():
    """Cgroup working set, excluding readily reclaimable inactive file cache."""
    try:
        root = Path('/sys/fs/cgroup')
        if (root / 'memory.current').exists():
            current = int((root / 'memory.current').read_text())
            raw_limit = (root / 'memory.max').read_text().strip()
            limit = int(raw_limit) if raw_limit != 'max' else 0
            stat = dict(line.split() for line in (root / 'memory.stat').read_text().splitlines())
            reclaimable = int(stat.get('inactive_file', 0))
        else:
            root = root / 'memory'
            current = int((root / 'memory.usage_in_bytes').read_text())
            limit = int((root / 'memory.limit_in_bytes').read_text())
            stat = dict(line.split() for line in (root / 'memory.stat').read_text().splitlines())
            reclaimable = int(stat.get('total_inactive_file', 0))
        if limit > 64 * 1024**3:
            limit = 0
        return {'working': max(0, current-reclaimable), 'current': current, 'limit': limit}
    except (OSError, ValueError):
        return {'working': 0, 'current': 0, 'limit': 0}


def chromium_args():
    args = ['--no-sandbox', '--disable-dev-shm-usage']
    if LOW_MEMORY:
        args += ['--disable-gpu', '--renderer-process-limit=2',
                 '--js-flags=--optimize-for-size --max-old-space-size=192']
    return args


TRACKERS = (
    'google-analytics.com', 'googletagmanager.com', 'doubleclick.net',
    'connect.facebook.net', 'api.segment.io', 'cdn.segment.com',
    'api.amplitude.com', 'cdn.amplitude.com', 'browser.sentry-cdn.com',
    'ingest.sentry.io', 'us.i.posthog.com', 'eu.i.posthog.com',
)


def unnecessary_request(host, resource_type):
    if not LOW_MEMORY:
        return False
    if any(host == suffix or host.endswith('.'+suffix) for suffix in TRACKERS):
        return True
    challenge_hosts=('google.com','gstatic.com','recaptcha.net','hcaptcha.com','challenges.cloudflare.com','arkoselabs.com','funcaptcha.com')
    if any(host == suffix or host.endswith('.'+suffix) for suffix in challenge_hosts):
        return False
    # System fonts suffice for the application; keep CAPTCHA fonts and every image.
    return resource_type == 'font'


async def watch_resources(app):
    state = app['state']
    while True:
        await asyncio.sleep(1)
        memory = memory_usage()
        state.memory = memory
        if not state.browser or state.memory_recovering:
            continue
        limit = memory['limit']
        if limit and memory['working'] > limit * .80:
            await state.pause_browser_for_memory()
            continue
        # Best-effort RAM-only cookies/localStorage/IndexedDB checkpoint. Never log it.
        now = time.monotonic()
        if now - state.last_checkpoint >= 20 and not state.lock.locked():
            async with state.lock:
                await state.checkpoint_browser()
