"""Per-key limits: model allowlist, RPM budget and parallel-request cap.

A key is provisioned for one client, so the client's own view of the catalogue and its
own budget are enforced by the gateway: /v1/models returns only what the key may call,
and a request that exceeds the budget is refused before an account is touched.

Run: ./.venv/bin/python -m unittest discover -s tests -v
"""
import asyncio, hashlib, json, sys, tempfile, unittest
from pathlib import Path

from aiohttp import web, ClientSession

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wb_gateway.limits import KeyLimits, model_allowed
from wb_gateway.server import Gateway, create_app
from wb_gateway.storage import Fault, stamp

MODEL = 'deepseek-test'
OTHER = 'other-model'
PATH = '/v2/chat/completions'


class Upstream:
    """Stub official API: one catalogue with two models, and a configurable delay."""

    def __init__(self):
        self.calls = []
        self.delay = 0
        self.app = web.Application()
        self.app.router.add_post(PATH, self.chat)
        self.app.router.add_get('/v3/config', self.config)

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        return self.site._server.sockets[0].getsockname()[1]

    async def stop(self):
        await self.runner.cleanup()

    async def chat(self, request):
        self.calls.append({'headers': dict(request.headers), 'body': await request.json()})
        if self.delay:
            await asyncio.sleep(self.delay)
        response = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        for chunk in ({'id': 'stub-1', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'OK'}}]},
                      {'id': 'stub-1', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}):
            await response.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
        await response.write(b'data: [DONE]\n\n')
        await response.write_eof()
        return response

    async def config(self, request):
        return web.json_response({'code': 0, 'data': {'agents': [{'name': 'craft', 'models': [MODEL, OTHER]}],
                                                      'models': [{'id': MODEL, 'enabled': True}, {'id': OTHER, 'enabled': True}],
                                                      'availableModels': [MODEL, OTHER]}})


class Allowlist(unittest.TestCase):
    def test_an_empty_list_allows_every_model(self):
        self.assertTrue(model_allowed({}, 'gl/anything'))
        self.assertTrue(model_allowed({'models': []}, 'cn/anything'))

    def test_exact_ids_and_globs_both_match(self):
        key = {'models': ['gl/deepseek-v4.1-flash', 'cn/*']}
        self.assertTrue(model_allowed(key, 'gl/deepseek-v4.1-flash'))
        self.assertFalse(model_allowed(key, 'gl/other'))
        self.assertTrue(model_allowed(key, 'cn/anything'))


class Counters(unittest.TestCase):
    def test_the_rpm_budget_is_a_sliding_minute(self):
        limits = KeyLimits(); key = {'id': 'k', 'rpm': 2, 'max_concurrency': 0}
        limits.charge(key); limits.charge(key)
        with self.assertRaises(Fault) as caught:
            limits.charge(key)
        self.assertEqual(caught.exception.code, 'key_rate_limited')
        self.assertEqual(caught.exception.status, 429)
        self.assertGreaterEqual(caught.exception.retry_after, 1)

    def test_the_parallel_cap_defaults_to_two(self):
        limits = KeyLimits(); key = {'id': 'k', 'max_concurrency': None}
        limits.acquire(key); limits.acquire(key)
        with self.assertRaises(Fault) as caught:
            limits.acquire(key)
        self.assertEqual(caught.exception.code, 'key_concurrency_limit')
        limits.release('k')
        limits.acquire(key)

    def test_zero_means_no_cap(self):
        limits = KeyLimits(); key = {'id': 'k', 'rpm': 0, 'max_concurrency': 0}
        for _ in range(50): limits.charge(key); limits.acquire(key)
        # Nothing is tracked, so nothing shows up as running for the console either.
        self.assertEqual(limits.snapshot(), {})


class KeyRecords(unittest.TestCase):
    def store(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        return Gateway(ROOT, data_dir=tmp.name, config={}, admin_key='admin-test').store

    def test_a_new_key_carries_the_limits_and_defaults(self):
        store = self.store()
        created = store.new_key({'name': 'client', 'models': ['gl/x', 'gl/x', 'cn/*'], 'rpm': 30, 'max_concurrency': 4})
        self.assertEqual(created['models'], ['gl/x', 'cn/*'])
        self.assertEqual(created['rpm'], 30)
        self.assertEqual(created['max_concurrency'], 4)
        self.assertEqual(store.new_key({'name': 'plain'})['max_concurrency'], 2)

    def test_an_update_changes_only_what_it_names(self):
        store = self.store()
        created = store.new_key({'name': 'client', 'models': ['gl/x'], 'rpm': 30, 'max_concurrency': 4})
        updated = store.update_key(created['id'], {'rpm': 5})
        self.assertEqual(updated['rpm'], 5)
        self.assertEqual(updated['models'], ['gl/x'])
        self.assertEqual(updated['max_concurrency'], 4)

    def test_an_update_can_move_a_key_to_another_region(self):
        store = self.store()
        created = store.new_key({'name': 'client', 'scope': 'all'})
        self.assertEqual(store.update_key(created['id'], {'scope': 'cn'})['scope'], 'cn')
        with self.assertRaises(Fault):
            store.update_key(created['id'], {'scope': 'mars'})
        self.assertEqual(store.update_key(created['id'], {'rpm': 9})['scope'], 'cn')

    def test_a_negative_or_non_numeric_limit_is_refused(self):
        store = self.store()
        for body in ({'rpm': -1}, {'max_concurrency': 'lots'}, {'models': 'gl/x'}):
            with self.assertRaises(Fault):
                store.new_key({'name': 'client', **body})


class KeySecrets(unittest.TestCase):
    def store(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        return Gateway(ROOT, data_dir=tmp.name, config={}, admin_key='admin-test').store

    def test_the_secret_can_be_read_back_but_never_leaves_the_ciphertext(self):
        store = self.store()
        created = store.new_key({'name': 'client'})
        listed = store.public_keys()[0]
        self.assertEqual(listed['key'], created['key'])
        self.assertEqual(listed['prefix'], created['key'][:14])
        self.assertNotIn('digest', listed)
        self.assertNotIn('secret_enc', listed)
        self.assertNotIn(created['key'], json.dumps(store.keys))

    def test_the_secret_survives_a_reload(self):
        store = self.store()
        created = store.new_key({'name': 'client'})
        reopened = type(store)(store.root)
        self.assertEqual(reopened.public_keys()[0]['key'], created['key'])

    def test_a_rotated_key_gets_a_new_secret_and_the_old_one_stops_working(self):
        store = self.store()
        created = store.new_key({'name': 'client'})
        rotated = store.update_key(created['id'], {'rotate': True})
        self.assertNotEqual(rotated['key'], created['key'])
        self.assertEqual(rotated['prefix'], rotated['key'][:14])
        self.assertEqual(store.authenticate(rotated['key'])['id'], created['id'])
        with self.assertRaises(Fault):
            store.authenticate(created['key'])

    def test_a_key_stored_before_secrets_were_kept_has_no_readable_secret(self):
        store = self.store()
        store.keys.append({'id': 'old', 'name': 'legacy', 'scope': 'all', 'prefix': 'legacy-prefix', 'digest': hashlib.sha256(b'legacy-secret').hexdigest(), 'enabled': True, 'created_at': '2026-01-01T00:00:00.000Z', 'expires_at': None, 'last_used_at': None})
        self.assertIsNone(store.public_keys()[0]['key'])
        self.assertEqual(store.authenticate('legacy-secret')['id'], 'old')
        self.assertTrue(store.update_key('old', {'rotate': True})['key'].startswith('sk-wb-'))

    def test_a_legacy_key_record_gets_the_defaults_on_load(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / 'api-keys.json').write_text(json.dumps({'version': 1, 'keys': [
            {'id': 'old', 'name': 'old', 'scope': 'all', 'prefix': 'sk-wb-old', 'digest': 'x', 'enabled': True}]}))
        store = Gateway(ROOT, data_dir=tmp.name, config={}, admin_key='admin-test').store
        self.assertEqual(store.keys[0]['models'], [])
        self.assertEqual(store.keys[0]['rpm'], 0)
        self.assertEqual(store.keys[0]['max_concurrency'], 2)


class Served(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = Upstream(); port = await self.upstream.start()
        endpoint = 'http://127.0.0.1:%d' % port
        config = {'intl': {'region': 'intl', 'endpoint': endpoint, 'platform': 'workbuddy-ai', 'version': '5.6.2', 'applicationName': 'workbuddy-ai'}}
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = Gateway(ROOT, data_dir=self.tmp.name, config=config, admin_key='admin-test', test_origins=True)
        self.gateway.store.put({'id': 'intl_a', 'region': 'intl', 'account': {'uid': 'uid-a', 'nickname': 'a'},
                                'auth': {'accessToken': 'token-a', 'expiresAt': stamp() + 3_600_000}})
        self.runner = web.AppRunner(create_app(self.gateway)); await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0); await self.site.start()
        self.base = 'http://127.0.0.1:%d' % self.site._server.sockets[0].getsockname()[1]
        self.session = ClientSession()

    async def asyncTearDown(self):
        await self.session.close(); await self.runner.cleanup(); await self.upstream.stop(); self.tmp.cleanup()

    def key(self, **limits):
        return self.gateway.store.new_key({'name': 'client', 'scope': 'intl', **limits})['key']

    async def chat(self, key, model='gl/' + MODEL):
        body = {'model': model, 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8}
        async with self.session.post(self.base + '/v1/chat/completions', json=body, headers={'Authorization': 'Bearer ' + key}) as response:
            return response.status, await response.json(), dict(response.headers)

    async def models(self, key):
        async with self.session.get(self.base + '/v1/models', headers={'Authorization': 'Bearer ' + key}) as response:
            return response.status, await response.json()

    async def test_the_catalogue_shows_only_the_allowed_models(self):
        status, payload = await self.models(self.key(models=['gl/' + MODEL]))
        self.assertEqual(status, 200, payload)
        self.assertEqual([m['id'] for m in payload['data']], ['gl/' + MODEL])
        self.assertTrue(payload['restricted'])
        status, payload = await self.models(self.key())
        self.assertEqual(sorted(m['id'] for m in payload['data']), ['gl/' + MODEL, 'gl/' + OTHER])

    async def test_a_model_outside_the_allowlist_is_a_403_not_a_404(self):
        key = self.key(models=['gl/' + MODEL])
        status, payload, _ = await self.chat(key, 'gl/' + OTHER)
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload['error']['type'], 'model_not_allowed')
        self.assertEqual(self.upstream.calls, [])
        status, payload, _ = await self.chat(key)
        self.assertEqual(status, 200, payload)

    async def test_the_rpm_budget_answers_429_with_retry_after(self):
        key = self.key(rpm=2)
        self.assertEqual((await self.chat(key))[0], 200)
        self.assertEqual((await self.chat(key))[0], 200)
        served = len(self.upstream.calls)
        status, payload, headers = await self.chat(key)
        self.assertEqual(status, 429, payload)
        self.assertEqual(payload['error']['type'], 'key_rate_limited')
        self.assertGreaterEqual(int(headers['Retry-After']), 1)
        self.assertEqual(len(self.upstream.calls), served)

    async def test_the_parallel_cap_holds_while_the_first_request_runs(self):
        self.upstream.delay = 0.4
        key = self.key(max_concurrency=1)
        first = asyncio.create_task(self.chat(key))
        await asyncio.sleep(0.15)
        status, payload, _ = await self.chat(key)
        self.assertEqual(status, 429, payload)
        self.assertEqual(payload['error']['type'], 'key_concurrency_limit')
        self.assertEqual((await first)[0], 200)
        # The slot is released on completion, so the next request is served normally.
        self.assertEqual((await self.chat(key))[0], 200)

    async def test_the_console_can_change_the_limits_of_a_live_key(self):
        created = self.gateway.store.new_key({'name': 'client', 'scope': 'intl'})
        async with self.session.post(self.base + '/admin/keys/' + created['id'], json={'rpm': 7, 'models': ['gl/' + MODEL], 'max_concurrency': 3},
                                     headers={'Authorization': 'Bearer admin-test'}) as response:
            self.assertEqual(response.status, 200, await response.text())
            payload = await response.json()
        self.assertEqual((payload['rpm'], payload['max_concurrency'], payload['models']), (7, 3, ['gl/' + MODEL]))
        status, listing = await self.models(created['key'])
        self.assertEqual([m['id'] for m in listing['data']], ['gl/' + MODEL])


if __name__ == '__main__':
    unittest.main()
