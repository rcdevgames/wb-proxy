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
        for suffix in suffixes: self.upstream.fail['uid-intl-'+suffix]=(403,{'code':11140,'msg':'request illegal','displayMsg':{'en':'Your account has been restricted due to unusual activity'}})

    async def test_a_restricted_account_rests_and_the_next_one_serves(self):
        # 11140 is the vendor's verdict on the account, so it rests for half an hour instead of a
        # minute — but the region keeps serving from the other accounts meanwhile.
        self.illegal('a')
        status,payload,_=await self.send()
        self.assertEqual(status,200,payload)
        entry=self.entry('account_restricted')
        self.assertEqual(entry['scope'],'account',entry)
        self.assertFalse(entry['manual'],entry)
        self.assertGreaterEqual(entry['retry_at']-stamp(),29*60*1000)
        self.assertLessEqual(entry['retry_at']-stamp(),30*60*1000)
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_a',PATH))
        entry['retry_at']=stamp()-1
        self.assertIsNone(self.gateway.risk.block('intl','intl_test_a',PATH))

    def pool(self,size):
        for i in range(size):
            self.gateway.store.put({'id':'intl_cap_%02d'%i,'region':'intl','account':{'uid':'uid-cap-%02d'%i,'nickname':'cap%d'%i},'auth':{'accessToken':'token-cap-%02d'%i,'expiresAt':stamp()+3600000}})
    def limit(self,*indexes):
        for i in indexes: self.upstream.fail['uid-cap-%02d'%i]=(429,{'code':14003,'msg':'Too many requests, please retry later.'})

    async def test_the_default_cap_still_finds_the_tenth_account(self):
        # Nine consecutive rejections do not exhaust the budget: the tenth account serves.
        self.pool(12); self.limit(*range(9))
        status,payload,_=await self.send()
        self.assertEqual(status,200,payload)
        self.assertEqual(len(self.upstream.calls),10)

    async def test_the_cap_stops_at_ten_consecutive_failures(self):
        # Ten rejections in a row are the whole budget, so the eleventh account is left alone.
        self.pool(12); self.limit(*range(10))
        status,payload,_=await self.send()
        self.assertEqual(status,429,payload)
        self.assertEqual(len(self.upstream.calls),10)

    async def test_one_success_ends_the_account_restriction_streak(self):
        self.illegal('a')
        status,_,_=await self.send()
        self.assertEqual(status,200)
        first=self.entry('account_restricted')
        self.assertEqual(first['count'],1)
        # The rest expires and the account serves again: that ends its streak.
        first['retry_at']=stamp()-1; self.gateway.risk.save(); self.upstream.fail.pop('uid-intl-a')
        status,payload,_=await self.send()
        self.assertEqual(status,200,payload)
        self.assertIsNone(self.gateway.risk.block('intl','intl_test_a',PATH))
        self.illegal('a')
        status,_,_=await self.send()
        self.assertEqual(status,200)
        again=[e for e in self.gateway.store.risks if e['kind']=='account_restricted'][-1]
        self.assertEqual(again['count'],1,again)
        self.assertFalse(again['manual'],again)

    async def test_a_repeat_restriction_parks_the_account_until_an_appeal(self):
        # No success in between, so the second verdict is the second one in the streak: the account
        # stays out until the operator appeals, because waiting a minute does not lift 11140.
        self.illegal('a')
        await self.send()
        first=self.entry('account_restricted')
        first['retry_at']=stamp()-1; self.gateway.risk.save()
        await self.send()
        again=[e for e in self.gateway.store.risks if e['kind']=='account_restricted'][-1]
        self.assertEqual(again['count'],2,again)
        self.assertTrue(again['manual'],again)
        self.assertIn('Submit feedback',again['action'],again)
        self.assertGreaterEqual(again['retry_at']-stamp(),119*60*1000)
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_a',PATH))
        # A manual row is not cleared by a 200: only the operator ends it.
        self.gateway.risk.served('intl','intl_test_a')
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_a',PATH))

    async def test_a_rejected_account_does_not_take_the_region_with_it(self):
        # 11140 lands per account, not per region: with one account flagged the other keeps serving.
        self.illegal('a')
        status,payload,_=await self.send()
        self.assertEqual(status,200,payload)
        self.assertEqual(self.entry('account_restricted')['scope'],'account')
        self.assertIsNone(self.gateway.risk.block('intl','intl_test_b',PATH))


    async def test_a_restricted_account_is_skipped_by_later_requests(self):
        # The rest is what makes the walk cheap: once every account is restricted, a request is refused
        # locally instead of walking the pool again.
        self.pool(6); self.illegal('a','b')
        for i in range(6):
            self.upstream.fail['uid-cap-%02d'%i]=(403,{'code':11140,'msg':'request illegal'})
        status,payload,_=await self.send()
        self.assertEqual(status,403,payload)
        walked=len(self.upstream.calls)
        self.assertEqual(walked,8,self.upstream.calls)
        status,payload,_=await self.send()
        self.assertEqual(status,403,payload)
        self.assertEqual(len(self.upstream.calls),walked,self.upstream.calls)

    async def test_one_restriction_still_fails_over_to_a_healthy_account(self):
        # A 11140 is per account, so a restricted account never fails a request a healthy one can serve.
        self.pool(3); self.upstream.fail['uid-cap-00']=(403,{'code':11140,'msg':'request illegal'})
        status,payload,_=await self.send()
        self.assertEqual(status,200,payload)
        self.assertEqual(len(self.upstream.calls),2,self.upstream.calls)


class AccountSwitch(unittest.IsolatedAsyncioTestCase):
    """A disabled account stays saved but leaves the pool."""
    async def asyncSetUp(self):
        self.upstream=Upstream(); port=await self.upstream.start()
        config={'intl':{'region':'intl','endpoint':'http://127.0.0.1:%d'%port,'platform':'workbuddy-ai','version':'5.6.2','applicationName':'workbuddy-ai'}}
        self.tmp=tempfile.TemporaryDirectory()
        self.gateway=Gateway(ROOT,data_dir=self.tmp.name,config=config,test_origins=True)
        self.gateway.store.put({'id':'intl_test_a','region':'intl','account':{'uid':'uid-intl-a','nickname':'intla'},'auth':{'accessToken':'token-a','expiresAt':stamp()+3600000}})
        self.key=self.gateway.store.new_key({'name':'tests','scope':'intl'})['key']
        self.runner=web.AppRunner(create_app(self.gateway)); await self.runner.setup()
        self.site=web.TCPSite(self.runner,'127.0.0.1',0); await self.site.start()
        self.base='http://127.0.0.1:%d'%self.site._server.sockets[0].getsockname()[1]
        self.session=ClientSession()
    async def asyncTearDown(self):
        await self.session.close(); await self.runner.cleanup(); await self.upstream.stop(); self.tmp.cleanup()
    async def admin(self,path,method='POST',body=None):
        async with self.session.request(method,self.base+path,headers={'Authorization':'Bearer '+self.gateway.admin_key},json={} if body is None else body) as response:
            return response.status,await response.json()
    async def chat(self):
        body={'model':'gl/'+MODEL,'messages':[{'role':'user','content':'hello'}],'max_tokens':16}
        async with self.session.post(self.base+'/v1/chat/completions',json=body,headers={'Authorization':'Bearer '+self.key}) as response:
            return response.status,await response.json()

    async def test_a_disabled_account_leaves_the_pool_and_keeps_its_record(self):
        status,payload=await self.admin('/admin/accounts/intl_test_a/disable')
        self.assertEqual(status,200,payload)
        self.assertFalse(payload['enabled'],payload)
        self.assertIn('intl_test_a',self.gateway.store.records)
        status,payload=await self.chat()
        self.assertEqual(status,503,payload)
        self.assertEqual(self.upstream.calls,[],self.upstream.calls)

    async def test_enable_puts_it_back_in_the_pool(self):
        await self.admin('/admin/accounts/intl_test_a/disable')
        status,payload=await self.admin('/admin/accounts/intl_test_a/enable')
        self.assertEqual(status,200,payload)
        self.assertTrue(payload['enabled'],payload)
        status,payload=await self.chat()
        self.assertEqual(status,200,payload)
        self.assertEqual(len(self.upstream.calls),1)

    async def test_a_disabled_account_is_not_swept_by_a_bulk_job(self):
        await self.admin('/admin/accounts/intl_test_a/disable')
        self.assertEqual(self.gateway.accounts(region=None),[])

    async def test_a_disabled_account_refuses_balance_and_refresh(self):
        # Nothing is sent upstream for an account out of the pool: no balance probe, no check-in,
        # no token refresh, so a parked account cannot be re-flagged while it rests.
        await self.admin('/admin/accounts/intl_test_a/disable')
        for action in ('balance','checkin','inspect','refresh'):
            status,payload=await self.admin('/admin/accounts/intl_test_a/'+action)
            self.assertEqual(status,409,payload)
            self.assertEqual(payload['error']['code'],'account_disabled',payload)
        self.assertEqual(self.upstream.calls,[],self.upstream.calls)

    async def test_bulk_disable_and_enable_by_selection(self):
        self.gateway.store.put({'id':'intl_test_b','region':'intl','account':{'uid':'uid-intl-b','nickname':'intlb'},'auth':{'accessToken':'token-b','expiresAt':stamp()+3600000}})
        status,payload=await self.admin('/admin/jobs',body={'action':'disable','account_ids':['intl_test_a','intl_test_b']})
        self.assertEqual(status,202,payload)
        self.assertEqual((payload['disabled'],payload['enabled'],payload['skipped']),(2,0,0),payload)
        self.assertEqual(self.gateway.accounts(region=None),[])
        status,payload=await self.admin('/admin/jobs',body={'action':'enable','account_ids':['intl_test_a']})
        self.assertEqual(status,202,payload)
        self.assertEqual([r['id'] for r in self.gateway.accounts(region=None)],['intl_test_a'])
        # A refresh job never reaches the disabled account, so nothing is probed upstream for it.
        before=len(self.upstream.calls)
        status,job=await self.admin('/admin/jobs',body={'action':'refresh','account_ids':['intl_test_b']})
        self.assertEqual(job['total'],0,job)
        self.assertEqual(len(self.upstream.calls),before)

    async def test_a_dismissed_restriction_keeps_its_cooldown_and_stays_blocking(self):
        # Dismissing hides the row from the console; it does not unblock the account, so traffic keeps
        # being refused until the cooldown ends on its own.
        self.upstream.fail['uid-intl-a']=(429,{'code':14003,'msg':'Too many requests, please retry later.'})
        status,payload=await self.chat()
        self.assertEqual(status,429,payload)
        entry=self.gateway.store.risks[-1]
        self.assertEqual(entry['kind'],'rate_limited',entry)
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_a',PATH))
        self.assertIn(entry['id'],[e['id'] for e in self.gateway.risk.visible()])
        status,payload=await self.admin('/admin/risk/'+entry['id']+'/dismiss')
        self.assertEqual(status,200,payload)
        self.assertIsNotNone(payload['dismissed_at'],payload)
        self.assertNotIn(entry['id'],[e['id'] for e in self.gateway.risk.visible()])
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_a',PATH))
        # The pause itself is untouched, so hiding it never turns into a silent resume.
        self.assertFalse(any(e.get('resolved_at') for e in self.gateway.store.risks),self.gateway.store.risks)

    async def test_dismissing_a_manual_restriction_needs_the_official_confirmation(self):
        self.upstream.fail['uid-intl-a']=(403,{'code':'E0010458','msg':'Enterprise creation quota exceeded'})
        status,payload=await self.chat()
        self.assertEqual(status,403,payload)
        entry=self.gateway.store.risks[-1]
        self.assertTrue(entry['manual'],entry)
        status,payload=await self.admin('/admin/risk/'+entry['id']+'/dismiss')
        self.assertEqual(status,400,payload)
        self.assertEqual(payload['error']['code'],'official_issue_resolved_required',payload)
        status,payload=await self.admin('/admin/risk/'+entry['id']+'/dismiss',body={'official_issue_resolved':True})
        self.assertEqual(status,200,payload)
        self.assertIsNotNone(payload['dismissed_at'],payload)
        self.assertNotIn(entry['id'],[e['id'] for e in self.gateway.risk.visible()])
        # A manual row still blocks traffic until it is resumed, not merely hidden.
        self.assertIsNotNone(self.gateway.risk.block('intl','intl_test_a',PATH))

    async def test_dismissing_an_unknown_restriction_is_a_404(self):
        status,payload=await self.admin('/admin/risk/nope/dismiss')
        self.assertEqual(status,404,payload)
        self.assertEqual(payload['error']['code'],'restriction_not_found',payload)


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

    def test_a_permanently_paused_11140_row_is_reclassified_and_rested(self):
        entry=self.entry('intl_x'); entry.update(kind='permission_denied',manual=True,upstream_code='11140')
        gateway=self.gateway(entry)
        stored=gateway.store.risks[0]
        self.assertEqual(stored['kind'],'account_restricted')
        self.assertFalse(stored['manual'])
        self.assertIsNotNone(gateway.risk.block('intl','intl_x',PATH))

    def test_an_old_request_illegal_row_is_carried_over_to_the_longer_rest(self):
        # The previous build rested the account for a minute and called the verdict transient. On load
        # the row becomes an account restriction with the appeal named as the way back.
        entry=self.entry('intl_x'); entry.update(kind='request_illegal',manual=False,upstream_code='11140')
        gateway=self.gateway(entry)
        stored=gateway.store.risks[0]
        self.assertEqual(stored['kind'],'account_restricted')
        self.assertIn('Submit feedback',stored['action'])
        self.assertIsNotNone(gateway.risk.block('intl','intl_x',PATH))
