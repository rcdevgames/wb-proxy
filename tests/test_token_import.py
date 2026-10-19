"""Import accepts a console access token, not only an offline refresh token.

A WorkBuddy console session token (typ Bearer) authenticates /v2/plugin/accounts and
/v3/config, but /v2/plugin/auth/token/refresh rejects it with 12153 invalid_grant. Such
a line used to fail the import; it is now stored as an access-only account whose
expiresAt comes from the JWT exp, so the pool keeps serving it until the token dies.

A pause that names an account which is no longer stored blocks nothing and cannot be
acted on, so it is resolved at startup instead of sitting in the UI as a manual action.

Run: ./.venv/bin/python -m unittest discover -s tests -v
"""
import base64, json, sys, tempfile, time, unittest
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wb_gateway.server import Gateway
from wb_gateway.storage import Store, now, stamp

ISS = 'https://www.workbuddy.ai/auth/realms/copilot'
UID = 'e18065dc-4b4b-4aab-ad62-e341ff50f3b6'


def jwt(typ='Bearer', uid=UID, exp=None, nickname='crowkre'):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    claims = {'iss': ISS, 'sub': uid, 'typ': typ, 'azp': 'console', 'exp': exp or int(time.time()) + 86400}
    if nickname: claims['preferred_username'] = nickname
    return part({'alg': 'RS256', 'typ': 'JWT'}) + '.' + part(claims) + '.signature'


class Upstream:
    """Stub official API: refresh refuses, every other call accepts the token."""
    def __init__(self):
        self.refreshes = 0
        self.refresh_reply = None
        self.app = web.Application()
        self.app.router.add_post('/v2/plugin/auth/token/refresh', self.refresh)
        self.app.router.add_get('/v2/plugin/accounts', self.accounts)
        self.app.router.add_get('/v3/config', self.config)
    async def start(self):
        self.runner = web.AppRunner(self.app); await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0); await self.site.start()
        return self.site._server.sockets[0].getsockname()[1]
    async def stop(self): await self.runner.cleanup()
    async def refresh(self, request):
        self.refreshes += 1
        if self.refresh_reply: return web.json_response(self.refresh_reply)
        return web.json_response({'code': 12153, 'msg': '12153:refresh token failed:400 Bad Request: invalid_grant: Invalid refresh token'}, status=401)
    async def accounts(self, request):
        return web.json_response({'code': 0, 'data': {'accounts': [{'uid': UID, 'nickname': 'crowkre', 'type': 'personal', 'lastLogin': True}]}})
    async def config(self, request):
        return web.json_response({'code': 0, 'data': {'agents': [], 'models': []}})


class AccessTokenImport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream = Upstream(); port = await self.upstream.start()
        config = {'intl': {'region': 'intl', 'endpoint': 'http://127.0.0.1:%d' % port, 'platform': 'workbuddy-ai', 'version': '5.6.2', 'applicationName': 'workbuddy-ai'}}
        self.tmp = tempfile.TemporaryDirectory()
        self.gateway = Gateway(ROOT, data_dir=self.tmp.name, config=config, test_origins=True)
        self.gateway.http = ClientSession(timeout=ClientTimeout(total=30), trust_env=False)
    async def asyncTearDown(self):
        await self.gateway.http.close(); await self.upstream.stop(); self.tmp.cleanup()
    def record(self, account_id): return self.gateway.store.records[account_id]

    async def test_an_access_token_is_stored_without_asking_for_a_refresh(self):
        exp = int(time.time()) + 31536000
        result = await self.gateway.import_accounts('crowkre|' + jwt(exp=exp))
        self.assertEqual(result['failed'], 0, result)
        row = result['results'][0]
        self.assertEqual(row['auth_type'], 'access_token', row)
        record = self.record(row['account_id'])
        self.assertNotIn('refreshToken', record['auth'])
        self.assertEqual(record['auth']['accessToken'], jwt(exp=exp))
        self.assertEqual(record['auth']['expiresAt'], exp * 1000)
        self.assertEqual(record['account']['uid'], UID)
        self.assertEqual(self.upstream.refreshes, 0)
        self.assertFalse(self.gateway.store.public(record)['refreshable'])

    async def test_an_offline_token_still_goes_through_the_refresh_endpoint(self):
        self.upstream.refresh_reply = {'code': 0, 'data': {'accessToken': 'at-1', 'refreshToken': 'rt-2', 'expiresIn': 3600, 'refreshExpiresIn': 86400}}
        result = await self.gateway.import_accounts('pebble91|' + jwt(typ='Offline'))
        self.assertEqual(result['failed'], 0, result)
        row = result['results'][0]
        self.assertEqual(row['auth_type'], 'refresh_token', row)
        record = self.record(row['account_id'])
        self.assertEqual(record['auth']['refreshToken'], 'rt-2')
        self.assertEqual(self.upstream.refreshes, 1)
        self.assertTrue(self.gateway.store.public(record)['refreshable'])

    async def test_an_expired_access_token_is_still_rejected(self):
        result = await self.gateway.import_accounts('old|' + jwt(exp=int(time.time()) - 60))
        self.assertEqual(result['failed'], 1, result)
        self.assertEqual(result['results'][0]['error']['code'], 'token_expired')
        self.assertEqual(self.gateway.store.records, {})


class RemovedAccountPause(unittest.TestCase):
    def gateway(self, entries, accounts=()):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / 'risk-state.json').write_text(json.dumps({'version': 1, 'entries': entries}))
        store = Store(tmp.name)
        for record in accounts: store.put(record)
        config = {'intl': {'region': 'intl', 'endpoint': 'https://www.workbuddy.ai', 'platform': 'workbuddy-ai', 'version': '5.6.2'}}
        return Gateway(ROOT, data_dir=tmp.name, config=config)
    def entry(self, account_id):
        return {'id': 'r-' + account_id, 'kind': 'login_required', 'scope': 'account', 'manual': True, 'region': 'intl', 'account_id': account_id, 'path': '/v2/plugin/auth/token/refresh', 'status': 401, 'retry_at': 0, 'count': 1, 'action': 'Resolve the account restriction through official channels, then resume manually.', 'upstream_code': '12153', 'updated_at': now()}

    def test_a_pause_for_a_removed_account_is_resolved(self):
        gateway = self.gateway([self.entry('intl_gone')])
        self.assertEqual(gateway.risk.list(), [])
        self.assertEqual(gateway.store.risks[0]['resolution'], 'account_removed')

    def test_a_pause_for_a_stored_account_survives(self):
        record = {'id': 'intl_here', 'region': 'intl', 'account': {'uid': 'uid-1'}, 'auth': {'accessToken': 'at'}}
        gateway = self.gateway([self.entry('intl_here')], accounts=[record])
        self.assertEqual([e['account_id'] for e in gateway.risk.list()], ['intl_here'])


if __name__ == '__main__':
    unittest.main()
