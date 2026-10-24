"""A side call failing is not the same as an account that cannot serve chat.

`/billing/meter/*` and the profile endpoints are called by the balance sync and the check-in job.
They can answer 401 for their own reasons — an edge/WAF page instead of JSON, a throttled job — and
that used to park the whole account behind a manual "Login required again" pause that also blocked
`/v2/chat/completions`. Only chat-serving paths may take an account out of the pool now; the rest is
recorded as a diagnostic row.

Run: ./.venv/bin/python -m unittest discover -s tests -v
"""
import json, sys, tempfile, unittest
from pathlib import Path

from aiohttp import web

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from wb_gateway.server import Gateway
from wb_gateway.storage import now, stamp

CHAT='/v2/chat/completions'
METER='/billing/meter/get-user-resource-summary'
EDGE_PAGE='<html><head><title>401 Authorization Required</title></head><body>openresty</body></html>'

class Upstream:
    """Stub official API: chat answers, every metering call is refused by the edge."""
    def __init__(self):
        self.calls=[]
        self.app=web.Application()
        self.app.router.add_post(CHAT,self.chat)
        self.app.router.add_get('/v3/config',self.config)
        self.app.router.add_post(METER,self.meter)
    async def start(self):
        self.runner=web.AppRunner(self.app); await self.runner.setup()
        self.site=web.TCPSite(self.runner,'127.0.0.1',0); await self.site.start()
        return self.site._server.sockets[0].getsockname()[1]
    async def stop(self): await self.runner.cleanup()
    async def chat(self,request):
        self.calls.append({'headers':dict(request.headers),'body':await request.json()})
        return web.json_response({'code':401,'msg':'invalid token'},status=401)
    async def config(self,request):
        return web.json_response({'code':0,'data':{'agents':[{'name':'craft','models':['deepseek-test']}],'models':[{'id':'deepseek-test','enabled':True}],'availableModels':['deepseek-test']}})
    async def meter(self,request):
        return web.Response(text=EDGE_PAGE,status=401,content_type='text/html')

class AuxiliaryProbe(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream=Upstream(); port=await self.upstream.start()
        self.tmp=tempfile.TemporaryDirectory()
        config={'intl':{'region':'intl','endpoint':'http://127.0.0.1:%d'%port,'platform':'workbuddy-ai','version':'5.6.2','applicationName':'workbuddy-ai'},
                'cn':{'region':'cn','endpoint':'http://127.0.0.1:%d'%port,'platform':'workbuddy','version':'5.6.2','applicationName':'WorkBuddy'}}
        self.gateway=Gateway(ROOT,data_dir=self.tmp.name,config=config,test_origins=True)
        await self.gateway.start(None)
        self.rid='cn_probe_account'
        self.gateway.store.put({'id':self.rid,'region':'cn','account':{'uid':'uid-probe','nickname':'probe'},
                                'auth':{'accessToken':'console-token','expiresAt':stamp()+3600000}})
    async def asyncTearDown(self):
        await self.gateway.close(None); await self.upstream.stop(); self.tmp.cleanup()

    def rows(self,kind=None):
        return [e for e in self.gateway.store.risks if kind is None or e['kind']==kind]

    async def test_a_failed_balance_probe_does_not_pause_the_account(self):
        # The edge answered the metering call with an HTML 401, which is exactly what a revoked token
        # also looks like. Nothing here proves the account cannot chat, so it stays in the pool.
        result=await self.gateway.inspect(self.gateway.store.records[self.rid],'balance')
        self.assertIsNotNone(result['balance'].get('error'),result['balance'])
        self.assertIsNone(self.gateway.risk.block('cn',self.rid,CHAT))
        self.gateway.risk.check('cn',self.rid,CHAT)
        row=self.rows('login_required')[-1]
        self.assertTrue(row.get('diagnostic_only'),row)
        self.assertFalse(row['manual'],row)
        self.assertEqual(row['path'],METER)
        self.assertEqual(row['scope'],'account')

    async def test_an_unparsable_body_is_not_reported_as_an_upstream_code(self):
        await self.gateway.inspect(self.gateway.store.records[self.rid],'balance')
        row=self.rows('login_required')[-1]
        self.assertIsNone(row['upstream_code'],row)
        self.assertEqual(row.get('upstream_body'),'non-json',row)
        self.assertIn('metering',row['action'])

    async def test_the_account_is_still_usable_for_chat_after_the_probe(self):
        # Not blocked by the probe means chat traffic actually reaches the upstream, which then says
        # for itself whether the account can serve.
        from wb_gateway.models import Fault
        await self.gateway.inspect(self.gateway.store.records[self.rid],'balance')
        with self.assertRaises(Fault) as caught:
            await self.gateway.call('cn',CHAT,self.gateway.store.records[self.rid],'POST',
                                    {'model':'deepseek-test','messages':[{'role':'user','content':'hi'}],'stream':True})
        self.assertEqual(len(self.upstream.calls),1)
        self.assertFalse((caught.exception.risk or {}).get('diagnostic_only'),caught.exception.risk)

    def test_a_chat_401_still_pauses_the_account(self):
        entry=self.gateway.risk.observe(401,{'code':401,'msg':'invalid token'},'cn',self.rid,CHAT,
                                        record=self.gateway.store.records[self.rid])
        self.assertIsNotNone(entry)
        self.assertFalse(entry.risk.get('diagnostic_only'),entry.risk)
        self.assertTrue(entry.risk['manual'],entry.risk)
        self.assertIsNotNone(self.gateway.risk.block('cn',self.rid,CHAT))

    def test_a_revoked_console_token_says_to_import_a_fresh_one(self):
        # An access-only account has no refresh path, so "resolve it through official channels" is
        # advice the operator cannot follow. Name the only action that works.
        entry=self.gateway.risk.observe(401,{'code':401,'msg':'invalid token'},'cn',self.rid,CHAT,
                                        record=self.gateway.store.records[self.rid])
        self.assertIn('import a fresh token',entry.risk['action'].lower())
        self.assertNotIn('official channels',entry.risk['action'].lower())

    def test_a_refreshable_account_keeps_the_generic_resolution_text(self):
        record={'id':'intl_refresher','region':'intl','account':{'uid':'uid-r'},'auth':{'accessToken':'a','refreshToken':'r'}}
        entry=self.gateway.risk.observe(401,{'code':401,'msg':'invalid token'},'intl','intl_refresher',CHAT,record=record)
        self.assertIn('official channels',entry.risk['action'].lower())

    def test_a_diagnostic_row_never_extends_a_chat_pause(self):
        # The probe must not overwrite the pause chat created, nor inherit its cooldown.
        self.gateway.risk.observe(401,{'code':401,'msg':'invalid token'},'cn',self.rid,CHAT,
                                  record=self.gateway.store.records[self.rid])
        pause=[e for e in self.rows('login_required') if not e.get('diagnostic_only')][-1]
        self.gateway.risk.observe(401,{'code':401,'msg':'invalid token'},'cn',self.rid,METER,
                                  record=self.gateway.store.records[self.rid])
        still=[e for e in self.rows('login_required') if not e.get('diagnostic_only')][-1]
        self.assertEqual(still['id'],pause['id'],still)
        self.assertTrue(still['manual'],still)
        self.assertIsNotNone(self.gateway.risk.block('cn',self.rid,CHAT))

class StaleState(unittest.TestCase):
    """Rows nobody can act on are dropped at load; rows a human must still act on are kept."""
    def gateway(self,entries):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        config={'intl':{'region':'intl','endpoint':'https://www.workbuddy.ai','platform':'workbuddy-ai','version':'5.6.2'}}
        # A pause naming an account that no longer exists is dropped at load (deleting the account is
        # the fix), so seed the account first and only then the rows.
        Gateway(ROOT,data_dir=tmp.name,config=config).store.put({'id':'intl_x','region':'intl','account':{'uid':'uid-x'},'auth':{'accessToken':'t'}})
        (Path(tmp.name)/'risk-state.json').write_text(json.dumps({'version':1,'entries':entries}))
        return Gateway(ROOT,data_dir=tmp.name,config=config)
    def row(self,**over):
        row={'id':'r','kind':'rate_limited','scope':'account','manual':False,'region':'intl','account_id':'intl_x',
             'path':CHAT,'status':429,'retry_at':stamp()-9*86400*1000,'count':1,'action':'stale',
             'upstream_code':'14003','updated_at':now()}
        row.update(over); return row

    def test_a_metering_pause_is_lifted_at_startup(self):
        gateway=self.gateway([self.row(kind='login_required',manual=True,retry_at=stamp(),path=METER)])
        stored=gateway.store.risks[0]
        self.assertTrue(stored.get('diagnostic_only'),stored)
        self.assertFalse(stored['manual'],stored)
        self.assertIsNone(gateway.risk.block('intl','intl_x',CHAT))

    def test_a_chat_pause_survives_startup(self):
        gateway=self.gateway([self.row(kind='login_required',manual=True,retry_at=stamp(),path=CHAT)])
        self.assertIsNotNone(gateway.risk.block('intl','intl_x',CHAT))

    def test_dead_cooldowns_are_dropped_and_live_pauses_kept(self):
        entries=[self.row(id='old%d'%i) for i in range(50)]
        entries.append(self.row(id='paused',kind='login_required',manual=True,retry_at=stamp(),path=CHAT))
        gateway=self.gateway(entries)
        self.assertEqual([e['id'] for e in gateway.store.risks],['paused'],gateway.store.risks)
        self.assertIsNotNone(gateway.risk.block('intl','intl_x',CHAT))

if __name__=='__main__': unittest.main()
