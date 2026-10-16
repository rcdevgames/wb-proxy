"""Every call log row says which API key made the call, and the logs can be filtered by it.

A revoked key is deleted from api-keys.json, so the row has to carry the name and the
masked prefix itself: otherwise the history of a revoked key is a bare UUID forever.

Run: ./.venv/bin/python -m unittest discover -s tests -v
"""
import json, sys, tempfile, unittest
from pathlib import Path

from aiohttp import web, ClientSession

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wb_gateway.server import Gateway, create_app
from wb_gateway.storage import now, stamp

MODEL = 'deepseek-test'
PATH = '/v2/chat/completions'
KEYS = ('alpha', 'beta')


class Upstream:
    """Stub official API: one streamed completion per request."""
    def __init__(self):
        self.calls = 0
        self.app = web.Application()
        self.app.router.add_post(PATH, self.chat)
        self.app.router.add_get('/v3/config', self.config)
    async def start(self):
        self.runner = web.AppRunner(self.app); await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0); await self.site.start()
        return self.site._server.sockets[0].getsockname()[1]
    async def stop(self): await self.runner.cleanup()
    async def chat(self, request):
        self.calls += 1
        response = web.StreamResponse(status=200, headers={'Content-Type': 'text/event-stream'})
        await response.prepare(request)
        for chunk in ({'id': 'stub-1', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'OK'}}]},
                      {'id': 'stub-1', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}):
            await response.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
        await response.write(b'data: [DONE]\n\n'); await response.write_eof()
        return response
    async def config(self, request):
        return web.json_response({'code': 0, 'data': {'agents': [{'name': 'craft', 'models': [MODEL]}], 'models': [{'id': MODEL, 'enabled': True}], 'availableModels': [MODEL]}})


class KeyAttribution(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = Upstream(); port = await self.upstream.start()
        endpoint = 'http://127.0.0.1:%d' % port
        config = {'intl': {'region': 'intl', 'endpoint': endpoint, 'platform': 'workbuddy-ai', 'version': '5.6.2', 'applicationName': 'workbuddy-ai'}}
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = Gateway(ROOT, data_dir=self.tmp.name, config=config, admin_key='admin-secret', test_origins=True)
        self.gateway.store.put({'id': 'intl_test', 'region': 'intl', 'account': {'uid': 'uid-intl', 'nickname': 'intl'}, 'auth': {'accessToken': 'token', 'expiresAt': stamp() + 3600000}})
        self.keys = {}
        for name in KEYS: self.keys[name] = self.gateway.store.new_key({'name': name, 'scope': 'intl'})
        self.runner = web.AppRunner(create_app(self.gateway)); await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0); await self.site.start()
        self.base = 'http://127.0.0.1:%d' % self.site._server.sockets[0].getsockname()[1]
        self.session = ClientSession()
    async def asyncTearDown(self):
        await self.session.close(); await self.runner.cleanup(); await self.upstream.stop(); self.tmp.cleanup()

    async def chat(self, name):
        body = {'model': 'gl/' + MODEL, 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 16}
        async with self.session.post(self.base + '/v1/chat/completions', json=body, headers={'Authorization': 'Bearer ' + self.keys[name]['key']}) as response:
            await response.read()
            return response.status
    async def logs(self, **query):
        url = self.base + '/admin/call-logs?limit=100' + ''.join('&' + k + '=' + v for k, v in query.items())
        async with self.session.get(url, headers={'Authorization': 'Bearer admin-secret'}) as response:
            return response.status, await response.json()

    async def test_each_row_carries_the_key_name_and_masked_prefix(self):
        for name in KEYS:
            self.assertEqual(await self.chat(name), 200)
        status, payload = await self.logs()
        self.assertEqual(status, 200, payload)
        rows = payload['data']
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual({r['api_key_name'] for r in rows}, set(KEYS))
        self.assertEqual({r['api_key_id'] for r in rows}, {self.keys[n]['id'] for n in KEYS})
        for row in rows:
            self.assertEqual(row['api_key_prefix'], self.keys[row['api_key_name']]['prefix'])
            self.assertNotIn(self.keys[row['api_key_name']]['key'], json.dumps(row))

    async def test_the_dropdown_offers_every_key_with_its_call_count(self):
        await self.chat('alpha'); await self.chat('alpha'); await self.chat('beta')
        _, payload = await self.logs()
        options = {o['id']: o for o in payload['key_options']}
        self.assertEqual(set(options), {self.keys[n]['id'] for n in KEYS})
        self.assertEqual(options[self.keys['alpha']['id']]['calls'], 2)
        self.assertEqual(options[self.keys['beta']['id']]['calls'], 1)
        self.assertEqual(options[self.keys['alpha']['id']]['name'], 'alpha')
        self.assertEqual(options[self.keys['alpha']['id']]['state'], 'active')
        self.assertEqual(options[self.keys['alpha']['id']]['label'], 'alpha \xb7 ' + self.keys['alpha']['prefix'] + '\u2026')
        self.assertEqual(payload['summary']['requests'], 3)

    async def test_filtering_by_key_returns_only_that_key(self):
        await self.chat('alpha'); await self.chat('beta'); await self.chat('beta')
        _, payload = await self.logs(key=self.keys['beta']['id'])
        self.assertEqual(payload['total'], 2, payload)
        self.assertEqual({r['api_key_name'] for r in payload['data']}, {'beta'})
        self.assertEqual(payload['summary']['requests'], 2)
        options = {o['id']: o for o in payload['key_options']}
        self.assertEqual(set(options), {self.keys[n]['id'] for n in KEYS})
        self.assertEqual(options[self.keys['beta']['id']]['calls'], 2)
        self.assertEqual(options[self.keys['alpha']['id']]['calls'], 1)
        _, payload = await self.logs(key='no-such-key')
        self.assertEqual(payload['total'], 0)
        self.assertEqual(payload['data'], [])

    async def test_a_revoked_key_keeps_its_name_in_history(self):
        await self.chat('alpha')
        revoked = self.keys['alpha']
        self.gateway.store.keys = [k for k in self.gateway.store.keys if k['id'] != revoked['id']]
        self.gateway.store.save_keys()
        _, payload = await self.logs()
        options = {o['id']: o for o in payload['key_options']}
        self.assertEqual(options[revoked['id']]['name'], 'alpha')
        self.assertEqual(options[revoked['id']]['prefix'], revoked['prefix'])
        self.assertEqual(options[revoked['id']]['state'], 'revoked')
        self.assertEqual(options[revoked['id']]['calls'], 1)
        self.assertNotIn(self.keys['beta']['id'], options)
        self.assertEqual(payload['data'][0]['api_key_name'], 'alpha')

    async def test_the_key_filter_composes_with_the_others(self):
        await self.chat('alpha'); await self.chat('beta')
        self.upstream.calls = 0
        _, payload = await self.logs(key=self.keys['alpha']['id'], region='cn')
        self.assertEqual(payload['total'], 0, payload)
        _, payload = await self.logs(key=self.keys['alpha']['id'], status='completed')
        self.assertEqual(payload['total'], 1, payload)

    async def test_a_row_without_a_snapshot_stays_distinguishable(self):
        await self.chat('alpha')
        revoked = self.keys['alpha']
        for row in self.gateway.store.logs: row.pop('api_key_name', None); row.pop('api_key_prefix', None)
        self.gateway.store.keys = [k for k in self.gateway.store.keys if k['id'] != revoked['id']]
        _, payload = await self.logs()
        options = {o['id']: o for o in payload['key_options']}
        self.assertEqual(options[revoked['id']]['name'], 'Revoked key ' + revoked['id'][:8])
        self.assertEqual(options[revoked['id']]['state'], 'revoked')
        self.assertIn(revoked['id'][:8], options[revoked['id']]['label'])
        self.assertEqual(len({o['label'] for o in payload['key_options']}), len(payload['key_options']))

    async def test_a_disabled_key_is_reported_as_disabled(self):
        await self.chat('alpha')
        key = next(k for k in self.gateway.store.keys if k['id'] == self.keys['alpha']['id'])
        key['enabled'] = False
        _, payload = await self.logs()
        options = {o['id']: o for o in payload['key_options']}
        self.assertEqual(options[self.keys['alpha']['id']]['state'], 'disabled')