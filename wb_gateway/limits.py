"""Per-API-key limits: a model allowlist, a per-minute budget and a concurrency cap.

The limits live on the key record (see Store.new_key) so the console can edit them and
they survive a restart. The counters are in memory only: an RPM window and a concurrency
counter must not survive a restart, because the requests they counted did not.
"""
import fnmatch, time
from collections import deque

from .storage import DEFAULT_MAX_CONCURRENCY, Fault


def model_patterns(key):
    return key.get('models') or []


def model_allowed(key, model_id):
    """A key without a list may use every model; a list may name exact IDs or globs."""
    patterns = model_patterns(key)
    return not patterns or any(fnmatch.fnmatchcase(model_id, pattern) for pattern in patterns)


class KeyLimits:
    """Request accounting for one gateway process."""

    def __init__(self):
        self.windows = {}
        self.active = {}

    def charge(self, key):
        """Count one request against the key's RPM budget; raise 429 when it is spent."""
        limit = key.get('rpm') or 0
        if not limit:
            return
        moment = time.monotonic()
        window = self.windows.setdefault(key['id'], deque())
        while window and moment - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            wait = max(1, int(60 - (moment - window[0]) + 0.999))
            raise Fault(429, f'API key rate limit reached ({limit} requests per minute)', 'key_rate_limited', retry_after=wait)
        window.append(moment)

    def acquire(self, key):
        """Reserve a concurrency slot; raise 429 when the key already runs its maximum."""
        limit = key.get('max_concurrency')
        limit = DEFAULT_MAX_CONCURRENCY if limit is None else limit
        if not limit:
            return True
        running = self.active.get(key['id'], 0)
        if running >= limit:
            raise Fault(429, f'API key concurrency limit reached ({limit} parallel requests)', 'key_concurrency_limit', retry_after=1)
        self.active[key['id']] = running + 1
        return True

    def release(self, key_id):
        self.active[key_id] = max(0, self.active.get(key_id, 1) - 1)

    def snapshot(self):
        return {key_id: count for key_id, count in self.active.items() if count}
