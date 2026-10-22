"""Upstream restrictions are scoped to what the upstream actually restricts.

A 429 is a property of one account, so the gateway cools that account down and keeps
serving the region from the others; only when every matching account is restricted does
the client get a 429. A rejection the gateway cannot classify (a bare 11128) still lands
in the call log with its upstream code.

Run: ./.venv/bin/python -m unittest discover -s tests -v
"""
import json, sys, tempfile, unittest
from unittest import mock
from pathlib import Path

from aiohttp import web, ClientSession

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from wb_gateway.server import Gateway, create_app
from wb_gateway.storage import now, stamp

MODEL='deepseek-test'
PATH='/v2/chat/completions'

class Upstream:
    """Stub official API: answers per account, and records every call it receives."""
    def __init__(self):
        self.calls=[]
        self.fail={}
        self.die_midstream=set()
        self.app=web.Application()
        self.app.router.add_post(PATH,self.chat)
        self.app.router.add_get('/v3/config',self.config)
    async def start(self):
        self.runner=web.AppRunner(self.app); await self.runner.setup()
        self.site=web.TCPSite(self.runner,'127.0.0.1',0); await self.site.start()
        return self.site._server.sockets[0].getsockname()[1]
    async def stop(self): await self.runner.cleanup()
    async def chat(self,request):
        self.calls.append({'headers':dict(request.headers),'body':await request.json()})
        rejected=self.fail.get(request.headers.get('X-User-Id',''))
        if rejected:
            status,body=rejected
            return web.json_response(body,status=status)
        if request.headers.get('X-User-Id','') in self.die_midstream:
            response=web.StreamResponse(status=200,headers={'Content-Type':'text/event-stream'})
            await response.prepare(request)
            await response.write(('data: '+json.dumps({'id':'stub-1','object':'chat.completion.chunk','choices':[{'index':0,'delta':{'content':'par'}}]})+'\n\n').encode())
            request.transport.abort()
            return response
        response=web.StreamResponse(status=200,headers={'Content-Type':'text/event-stream'})
        await response.prepare(request)
        for chunk in ({'id':'stub-1','object':'chat.completion.chunk','choices':[{'index':0,'delta':{'role':'assistant','content':'OK'}}]},
                      {'id':'stub-1','object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1}}):
            await response.write(('data: '+json.dumps(chunk)+'\n\n').encode())
        await response.write(b'data: [DONE]\n\n'); await response.write_eof()
        return response
    async def config(self,request):
        return web.json_response({'code':0,'data':{'agents':[{'name':'craft','models':[MODEL]}],'models':[{'id':MODEL,'enabled':True}],'availableModels':[MODEL]}})

class AccountScope(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream=Upstream(); port=await self.upstream.start()
        endpoint='http://127.0.0.1:%d'%port
        config={'intl':{'region':'intl','endpoint':endpoint,'platform':'workbuddy-ai','version':'5.6.2','applicationName':'workbuddy-ai'}}
        self.tmp=tempfile.TemporaryDirectory()
        self.gateway=Gateway(ROOT,data_dir=self.tmp.name,config=config,test_origins=True)
        for suffix in ('a','b'):
            self.gateway.store.put({'id':'intl_test_'+suffix,'region':'intl','account':{'uid':'uid-intl-'+suffix,'nickname':'intl'+suffix},'auth':{'accessToken':'token-'+suffix,'expiresAt':stamp()+3600000}})
        self.key=self.gateway.store.new_key({'name':'tests','scope':'intl'})['key']
        self.runner=web.AppRunner(create_app(self.gateway)); await self.runner.setup()
        self.site=web.TCPSite(self.runner,'127.0.0.1',0); await self.site.start()
        self.base='http://127.0.0.1:%d'%self.site._server.sockets[0].getsockname()[1]
        self.session=ClientSession()
    async def asyncTearDown(self):
        await self.session.close(); await self.runner.cleanup(); await self.upstream.stop(); self.tmp.cleanup()
    async def send(self):
        body={'model':'gl/'+MODEL,'messages':[{'role':'user','content':'hello'}],'max_tokens':16}
        async with self.session.post(self.base+'/v1/chat/completions',json=body,headers={'Authorization':'Bearer '+self.key}) as response:
            payload=await response.json()
            return response.status,payload,dict(response.headers)
    def rate_limit(self,*suffixes):
        for suffix in suffixes: self.upstream.fail['uid-intl-'+suffix]=(429,{'code':14003,'msg':'Too many requests, please retry later.'})
    def entry(self,kind):
        return next((e for e in self.gateway.store.risks if e['kind']==kind),None)

    async def test_a_rate_limited_account_is_replaced_before_the_first_byte(self):
        self.rate_limit('a')
        status,payload,_=await self.send()
        self.assertEqual(status,200,payload)
        self.assertEqual([c['headers']['X-User-Id'] for c in self.upstream.calls],['uid-intl-a','uid-intl-b'])
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_a',PATH))
        served=len(self.upstream.calls)
        second,payload,_=await self.send()
        self.assertEqual(second,200,payload)
        self.assertEqual(len(self.upstream.calls),served+1)
        self.assertEqual(self.upstream.calls[-1]['headers']['X-User-Id'],'uid-intl-b')

    async def test_a_stream_that_already_started_is_never_replayed(self):
        self.upstream.die_midstream.add('uid-intl-a')
        body={'model':'gl/'+MODEL,'messages':[{'role':'user','content':'hello'}],'stream':True,'max_tokens':16}
        async with self.session.post(self.base+'/v1/chat/completions',json=body,headers={'Authorization':'Bearer '+self.key}) as response:
            self.assertEqual(response.status,200)
            text=await response.text()
        self.assertIn('error',text)
        self.assertEqual(len(self.upstream.calls),1,self.upstream.calls)

    async def test_an_account_scoped_permission_denial_moves_to_another_account(self):
        self.upstream.fail['uid-intl-a']=(403,{'code':14003,'msg':'permission denied for this account'})
        status,payload,_=await self.send()
        self.assertEqual(status,200,payload)
        self.assertEqual([c['headers']['X-User-Id'] for c in self.upstream.calls],['uid-intl-a','uid-intl-b'])

    async def test_a_global_restriction_is_not_replayed(self):
        for suffix in ('a','b'): self.upstream.fail['uid-intl-'+suffix]=(403,{'code':'risk_control','msg':'human verification required'})
        status,payload,_=await self.send()
        self.assertEqual(status,403,payload)
        self.assertEqual(len(self.upstream.calls),1,self.upstream.calls)
        self.assertEqual(self.entry('device_verification')['scope'],'global')

    async def test_a_client_error_is_not_replayed_on_another_account(self):
        for suffix in ('a','b'): self.upstream.fail['uid-intl-'+suffix]=(400,{'code':11128,'msg':'conversation id missing'})
        status,payload,_=await self.send()
        self.assertEqual(status,400,payload)
        self.assertEqual(len(self.upstream.calls),1,self.upstream.calls)

    async def test_failover_stops_at_the_configured_cap(self):
        self.rate_limit('a','b')
        with mock.patch('wb_gateway.server.MAX_FAILOVER',1):
            status,payload,_=await self.send()
        self.assertEqual(status,429,payload)
        self.assertEqual(len(self.upstream.calls),1,self.upstream.calls)

    async def test_the_restriction_belongs_to_the_account_not_the_region(self):
        self.rate_limit('a')
        await self.send()
        entry=self.entry('rate_limited')
        self.assertEqual(entry['scope'],'account',entry)
        self.assertEqual(entry['account_id'],'intl_test_a')
        self.assertIsNone(self.gateway.risk.block('intl','intl_test_b',PATH))
        self.gateway.risk.check('intl','intl_test_b',PATH)
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_a',PATH))

    async def test_every_account_restricted_answers_429_with_retry_after(self):
        self.rate_limit('a','b')
        await self.send()
        await self.send()
        served=len(self.upstream.calls)
        status,payload,headers=await self.send()
        self.assertEqual(status,429,payload)
        self.assertEqual(payload['error']['type'],'risk_rate_limited')
        self.assertGreaterEqual(int(headers['Retry-After']),1)
        self.assertEqual(len(self.upstream.calls),served)

    async def test_a_region_restriction_still_covers_every_account(self):
        self.gateway.store.risks.append({'id':'r1','kind':'upstream_unavailable','scope':'region','manual':False,'region':'intl','account_id':None,'path':PATH,'status':503,'retry_at':stamp()+5000,'count':1,'action':'Upstream unavailable','upstream_code':'503','updated_at':now()})
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_b',PATH))
        status,payload,_=await self.send()
        self.assertEqual(status,503,payload)
        self.assertEqual(self.upstream.calls,[])

    async def test_an_unclassified_rejection_keeps_its_upstream_code(self):
        for suffix in ('a','b'): self.upstream.fail['uid-intl-'+suffix]=(400,{'code':11128,'msg':'conversation id missing'})
        status,payload,_=await self.send()
        self.assertEqual(status,400,payload)
        self.assertEqual(payload['error']['type'],'upstream_11128')
        self.assertIsNone(self.entry('rate_limited'))
        row=self.gateway.store.logs[-1]
        self.assertEqual(row['outcome'],'failed')
        self.assertEqual(row['status_code'],400)
        self.assertEqual(row['error_code'],'upstream_11128')
        self.assertEqual(row['upstream_code'],'11128')

    def illegal(self,*suffixes):
        for suffix in suffixes: self.upstream.fail['uid-intl-'+suffix]=(403,{'code':11140,'msg':'request illegal'})

    async def test_request_illegal_cools_down_instead_of_waiting_for_a_human(self):
        # 11140 is risk control on one request: the account must come back on its own, not sit
        # behind a permanent manual pause that only a Resume click can clear.
        self.illegal('a')
        status,payload,_=await self.send()
        self.assertEqual(status,200,payload)
        entry=self.entry('request_illegal')
        self.assertEqual(entry['scope'],'account',entry)
        self.assertFalse(entry['manual'],entry)
        self.assertGreater(entry['retry_at'],stamp())
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_a',PATH))
        entry['retry_at']=stamp()-1
        self.assertIsNone(self.gateway.risk.block('intl','intl_test_a',PATH))

    async def test_a_rejected_account_does_not_take_the_region_with_it(self):
        # 11140 is per request, not per region: with one account flagged the other keeps serving.
        self.illegal('a')
        status,payload,_=await self.send()
        self.assertEqual(status,200,payload)
        self.assertEqual(self.entry('request_illegal')['scope'],'account')
        self.assertIsNone(self.gateway.risk.block('intl','intl_test_b',PATH))


class ScopeMigration(unittest.TestCase):
    """A rate limit written with the old region scope is corrected at startup."""
    def gateway(self,entry):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        (Path(tmp.name)/'risk-state.json').write_text(json.dumps({'version':1,'entries':[entry]}))
        config={'intl':{'region':'intl','endpoint':'https://www.workbuddy.ai','platform':'workbuddy-ai','version':'5.6.2'}}
        return Gateway(ROOT,data_dir=tmp.name,config=config)
    def entry(self,account_id):
        return {'id':'r','kind':'rate_limited','scope':'region','manual':False,'region':'intl','account_id':account_id,'path':PATH,'status':429,'retry_at':stamp()+60000,'count':1,'action':'stale','upstream_code':'14003','updated_at':now()}
    def test_the_account_takes_over_the_region_scope(self):
        gateway=self.gateway(self.entry('intl_x'))
        self.assertEqual(gateway.store.risks[0]['scope'],'account')
        self.assertIsNotNone(gateway.risk.block('intl','intl_x',PATH))
        self.assertIsNone(gateway.risk.block('intl','intl_y',PATH))
    def test_without_an_account_the_region_scope_stays(self):
        gateway=self.gateway(self.entry(None))
        self.assertEqual(gateway.store.risks[0]['scope'],'region')
        self.assertIsNotNone(gateway.risk.block('intl','intl_y',PATH))

    def test_a_permanently_paused_11140_row_is_reclassified_and_cleared(self):
        entry=self.entry('intl_x'); entry.update(kind='permission_denied',manual=True,upstream_code='11140')
        gateway=self.gateway(entry)
        stored=gateway.store.risks[0]
        self.assertEqual(stored['kind'],'request_illegal')
        self.assertFalse(stored['manual'])
        self.assertIsNone(gateway.risk.block('intl','intl_x',PATH))
