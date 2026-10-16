"""Headers the gateway sends to the upstream chat endpoint.

The intl channel verifies that a call came from the official client and keys its
conversation on the X-Conversation-* header family, so the gateway has to mirror
the IDE: one stable RFC-4122 conversation id per session, and a fresh id per
request and per message.

Run: ./.venv/bin/python -m unittest discover -s tests -v
"""
import json, re, sys, tempfile, unittest
from pathlib import Path

from aiohttp import web, ClientSession

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from wb_gateway.protocol import conversation_key, message_key
from wb_gateway.server import Gateway, create_app
from wb_gateway.storage import stamp

MODEL='deepseek-test'
UUID5=re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
UUID4=re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')

class Upstream:
    """Stub official API: records what the gateway sends, answers a valid SSE stream."""
    def __init__(self):
        self.calls=[]
        self.app=web.Application()
        self.app.router.add_post('/v2/chat/completions',self.chat)
        self.app.router.add_get('/v3/config',self.config)
    async def start(self):
        self.runner=web.AppRunner(self.app); await self.runner.setup()
        self.site=web.TCPSite(self.runner,'127.0.0.1',0); await self.site.start()
        return self.site._server.sockets[0].getsockname()[1]
    async def stop(self): await self.runner.cleanup()
    async def chat(self,request):
        self.calls.append({'headers':dict(request.headers),'body':await request.json()})
        response=web.StreamResponse(status=200,headers={'Content-Type':'text/event-stream'})
        await response.prepare(request)
        for chunk in ({'id':'stub-1','object':'chat.completion.chunk','choices':[{'index':0,'delta':{'role':'assistant','content':'OK'}}]},
                      {'id':'stub-1','object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1}}):
            await response.write(('data: '+json.dumps(chunk)+'\n\n').encode())
        await response.write(b'data: [DONE]\n\n'); await response.write_eof()
        return response
    async def config(self,request):
        return web.json_response({'code':0,'data':{'agents':[{'name':'craft','models':[MODEL]}],'models':[{'id':MODEL,'enabled':True}],'availableModels':[MODEL]}})

class ConversationHeaders(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream=Upstream(); port=await self.upstream.start()
        endpoint='http://127.0.0.1:%d'%port
        config={region:{'region':region,'endpoint':endpoint,'platform':'workbuddy-ai' if region=='intl' else 'workbuddy','version':'5.6.2','applicationName':'workbuddy-ai' if region=='intl' else 'WorkBuddy'} for region in ('cn','intl')}
        self.tmp=tempfile.TemporaryDirectory()
        self.gateway=Gateway(ROOT,data_dir=self.tmp.name,config=config,test_origins=True)
        for region in ('intl','cn'):
            for suffix in (('a','b') if region=='intl' else ('a',)):
                self.gateway.store.put({'id':region+'_test_'+suffix,'region':region,'account':{'uid':'uid-'+region+'-'+suffix,'enterpriseId':'ent-'+region+'-'+suffix,'nickname':region+suffix},'auth':{'accessToken':'token-'+suffix,'expiresAt':stamp()+3600000}})
        self.key=self.gateway.store.new_key({'name':'tests','scope':'intl'})['key']
        self.any_key=self.gateway.store.new_key({'name':'tests-all','scope':'all'})['key']
        self.runner=web.AppRunner(create_app(self.gateway)); await self.runner.setup()
        self.site=web.TCPSite(self.runner,'127.0.0.1',0); await self.site.start()
        self.base='http://127.0.0.1:%d'%self.site._server.sockets[0].getsockname()[1]
        self.session=ClientSession()
    async def asyncTearDown(self):
        await self.session.close(); await self.runner.cleanup(); await self.upstream.stop(); self.tmp.cleanup()
    async def send(self,key=None,**extra):
        body={'model':'gl/'+MODEL,'messages':[{'role':'user','content':'hello'}],'max_tokens':16,**extra}
        async with self.session.post(self.base+'/v1/chat/completions',json=body,headers={'Authorization':'Bearer '+(key or self.key)}) as response:
            status=response.status; payload=await response.json()
        self.assertEqual(status,200,payload)
        return self.upstream.calls[-1]

    async def test_headers_mirror_the_official_client(self):
        call=await self.send(); headers=call['headers']
        self.assertRegex(headers['X-Conversation-ID'],UUID5)
        self.assertRegex(headers['X-Conversation-Request-ID'],UUID4)
        self.assertRegex(headers['X-Conversation-Message-ID'],UUID4)
        self.assertNotEqual(headers['X-Conversation-Request-ID'],headers['X-Conversation-Message-ID'])
        self.assertEqual(headers['X-Model-ID'],MODEL)
        self.assertEqual(headers['x-codebuddy-request'],'1')
        self.assertEqual(headers['X-Agent-Intent'],'craft')
        self.assertEqual(call['body']['messages'][0]['role'],'system')

    async def test_conversation_id_survives_a_retry(self):
        first=await self.send(prompt_cache_key='sess-1')
        second=await self.send(prompt_cache_key='sess-1')
        self.assertEqual(first['headers']['X-Conversation-ID'],second['headers']['X-Conversation-ID'])
        self.assertNotEqual(first['headers']['X-Conversation-Request-ID'],second['headers']['X-Conversation-Request-ID'])
        self.assertNotEqual(first['headers']['X-Conversation-Message-ID'],second['headers']['X-Conversation-Message-ID'])

    async def test_conversation_id_survives_an_account_failover(self):
        first=await self.send(prompt_cache_key='sess-2')
        served=first['headers']['X-User-Id']
        self.gateway.busy['intl_test_'+served[-1]]=8
        second=await self.send(prompt_cache_key='sess-2')
        self.assertNotEqual(second['headers']['X-User-Id'],served)
        self.assertEqual(first['headers']['X-Conversation-ID'],second['headers']['X-Conversation-ID'])

    async def test_explicit_session_id_wins_over_other_hints(self):
        first=await self.send(session_id='session-A',prompt_cache_key='cache-B')
        same=await self.send(session_id='session-A',prompt_cache_key='cache-C')
        other=await self.send(session_id='session-B',prompt_cache_key='cache-C')
        self.assertEqual(first['headers']['X-Conversation-ID'],same['headers']['X-Conversation-ID'])
        self.assertNotEqual(first['headers']['X-Conversation-ID'],other['headers']['X-Conversation-ID'])

    async def test_conversation_id_follows_the_opening_user_message(self):
        first=await self.send()
        retry=await self.send()
        other=await self.send(messages=[{'role':'user','content':'another conversation'}])
        self.assertEqual(first['headers']['X-Conversation-ID'],retry['headers']['X-Conversation-ID'])
        self.assertNotEqual(first['headers']['X-Conversation-ID'],other['headers']['X-Conversation-ID'])

    async def test_typed_block_opening_message_matches_the_string_form(self):
        text=await self.send(messages=[{'role':'user','content':'hello world'}])
        typed=await self.send(messages=[{'role':'user','content':[{'type':'text','text':'hello world'}]}])
        self.assertEqual(text['headers']['X-Conversation-ID'],typed['headers']['X-Conversation-ID'])

    async def test_cn_region_gets_the_headers_without_the_intl_body_rule(self):
        call=await self.send(self.any_key,model='cn/'+MODEL)
        self.assertRegex(call['headers']['X-Conversation-ID'],UUID5)
        # The default system prompt is an intl-only body rule; cn keeps its body.
        self.assertEqual(call['body']['messages'][0]['role'],'user')

class SessionKey(unittest.TestCase):
    def test_client_header_is_the_session(self):
        self.assertEqual(conversation_key({'messages':[]},'from-client'),'from-client')
    def test_session_id_wins_over_other_hints(self):
        self.assertEqual(conversation_key({'session_id':'s','prompt_cache_key':'p','conversation_id':'c'}),'s')
        self.assertEqual(conversation_key({'prompt_cache_key':'p','conversation_id':'c'}),'p')
        self.assertEqual(conversation_key({'conversation_id':'c'}),'c')
    def test_opening_user_message_is_the_fallback(self):
        self.assertEqual(conversation_key({'messages':[{'role':'system','content':'be brief'},{'role':'user','content':'hello'},{'role':'assistant','content':'hi'}]}),'hello')
    def test_random_session_without_any_identity(self):
        first=conversation_key({'messages':[{'role':'assistant','content':'hi'}]})
        second=conversation_key({'messages':[]})
        self.assertTrue(first.startswith('session_'))
        self.assertNotEqual(first,second)
    def test_typed_blocks_key_the_text_and_stay_order_independent(self):
        self.assertEqual(message_key({'content':[{'type':'text','text':'a'},{'type':'text','text':'b'}]}),'a'+chr(10)+'b')
        self.assertEqual(message_key({'content':[{'type':'image_url','image_url':{'url':'u'}}]}),message_key({'content':[{'image_url':{'url':'u'},'type':'image_url'}]}))
        self.assertEqual(message_key({'content':'plain'}),'plain')
