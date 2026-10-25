import re, math, time
from datetime import datetime
from zoneinfo import ZoneInfo
from .storage import require, Fault, now, stamp, atomic, uid

def number(x): return type(x) in (int,float) and math.isfinite(x) and x>=0
def visible(product):
    agents=product.get('agents',[])
    agent=next((a for a in agents if a.get('name')=='craft'),None) or next((a for a in agents if 'default' in a.get('tags',[])),None) or next((a for a in agents if a.get('name')=='cli'),None)
    available=set(product.get('availableModels') or [])
    ids=set(agent['models']) if agent and isinstance(agent.get('models'),list) else available
    require(bool(ids),'Official chat model visibility unavailable',503,'catalog_visibility_unavailable')
    out={}; excluded={'text-to-image','text-to-video','image-to-video'}
    for m in product.get('models',[]):
        i=m.get('id')
        if isinstance(i,str) and i in ids and (not available or i in available) and not m.get('local') and not m.get('url') and not m.get('disabled') and m.get('enabled') is not False and not m.get('hidden') and not excluded.intersection(m.get('tags',[])):
            out.setdefault(i,m)
    return list(out.values())
def controls(m):
    r=m.get('reasoning') or {}; allowed={'minimal','low','medium','high','xhigh','max'}
    efforts=[e for e in r.get('supportedEfforts',[]) if e in allowed]
    lengths=sorted({x for x in m.get('contextWindow',{}).get('supportedLengths',[]) if number(x) and x>0 and x<=m.get('maxInputTokens',float('inf'))})
    return {'id':m['id'],'name':m.get('name',m['id']),'supports_reasoning':bool(m.get('supportsReasoning')),'can_disable':not m.get('onlyReasoning',False) and r.get('canDisableThinking') is not False,'efforts':efforts,'default_effort':r.get('defaultEffort',r.get('effort')),'context_lengths':lengths if len(lengths)>1 else [],'default_context':(m.get('contextWindow',{}).get('defaultLength') if m.get('contextWindow',{}).get('defaultLength') in lengths else lengths[0]) if len(lengths)>1 else m.get('maxInputTokens'),'max_input_tokens':m.get('maxInputTokens'),'max_output_tokens':m.get('maxOutputTokens'),'supports_tools':bool(m.get('supportsToolCall')),'supports_images':bool(m.get('supportsImages')),'temperature':m.get('temperature'),'related_models':m.get('relatedModels',{})}
def multiplier(raw):
    if not isinstance(raw,str): return None
    m=re.fullmatch(r'(?:[x×]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*[x×])(?:\s*credits?)?',raw.strip(),re.I)
    return float(m[1] or m[2]) if m else None
def active(p):
    if p.get('enabled') is False: return False
    s=p.get('schedule') or {}
    try:
        for k,start in [('validFrom',True),('validUntil',False)]:
            if s.get(k):
                t=datetime.fromisoformat(s[k].replace('Z','+00:00')).timestamp()
                if (start and time.time()<t) or (not start and time.time()>=t): return False
        if not s.get('daily'): return True
        d=datetime.now(ZoneInfo(s['timezone'])) if s.get('timezone') else datetime.now(); minute=d.hour*60+d.minute
        for w in s['daily']:
            a,b=[sum(int(z)*f for z,f in zip(w[k].split(':'),(60,1))) for k in ('start','end')]
            if a==b or (a<b and a<=minute<b) or (a>b and (minute>=a or minute<b)): return True
    except (ValueError,KeyError,TypeError): return False
    return False
def quote(m,promotions):
    raw=m.get('credits'); base=multiplier(raw); value=base; source='not_reported' if base is None else 'models.credits'
    ps=[p for p in promotions if m['id'] in p.get('modelIds',[])]+m.get('promotions',[])
    ps=sorted([p for p in ps if ('modelIds' not in p or m['id'] in p['modelIds']) and active(p)],key=lambda p:p.get('priority',0),reverse=True)
    p=ps[0] if ps else None; discount=(p or {}).get('discount') or {}
    explicit=multiplier(discount.get('discountedCredits'))
    if explicit is not None: value=explicit; source='modelPromotions.discountedCredits'
    elif base is not None and number(discount.get('factor')): value=float(format(base*discount['factor'],'.12g')); source='modelPromotions.factor'
    badge=(p or {}).get('badge') or {}
    return {'credits_raw':raw,'base_multiplier':base,'effective_multiplier':value,'status':'provided' if value is not None else 'unparsed' if raw else 'not_provided','source':source,'promotion':{'id':p.get('id'),'label':badge.get('label',badge.get('labelEn',badge.get('labelZh'))),'valid_until':p.get('schedule',{}).get('validUntil'),'factor':discount.get('factor'),'discounted_credits':discount.get('discountedCredits')} if p else None}
# 11140's own wording is "your account has been restricted due to unusual activity ... submit
# feedback to appeal": the vendor's verdict on the account, not on the request. It is not lifted by
# waiting a minute, and every retry is another abnormal-activity datapoint on the same egress IP, so
# the first verdict rests the account for half an hour and a repeat inside the window parks it until
# the operator appeals and resumes.
RESTRICTED_FIRST=30*60*1000
RESTRICTED_REPEAT=2*60*60*1000
# How long a repeat verdict still counts as the same streak. The cooldown is half an hour, so the
# generic fifteen-minute window would always look like a fresh start and the escalation never fire.
RESTRICTED_WINDOW=12*60*60*1000
RESTRICTED_ACTION='Upstream restricted this account (11140: unusual activity). Rested for 30 minutes; a repeat verdict parks it until the restriction is lifted. Appeal in WorkBuddy: Submit feedback.'
RESTRICTED_APPEAL='Upstream restricted this account again (11140: unusual activity). It stays out of the pool until WorkBuddy lifts the restriction: appeal with WorkBuddy → Submit feedback, then Resume this account.'
AUX_ACTION='Rejected on a metering or profile call, not on chat. Only chat traffic can pause an account, so it stays in the pool; this row is diagnostic.'
# What the operator reads when the call itself fails: short, because it lands in a toast or a table
# caption, and the row above carries the full explanation.
def aux_label(path):
    path=path or ''
    if path.startswith('/billing/'): return 'metering'
    if path.startswith('/v3/config'): return 'model catalog'
    return 'profile'
LOGIN_ACCESS_ONLY='The upstream no longer accepts this account\'s console session token. It has no refresh path: import a fresh token with Add account → Import accounts from .txt.'
# Only these paths say anything about whether an account can serve chat. Everything else is a side
# call (metering, check-in, account sync) that can fail on its own without the account being dead.
def pool_critical(path): return '/chat/' in (path or '') or path.startswith('/v3/config') or '/auth/token/refresh' in path
# Rows nobody can act on any more are dropped at load; this caps the file after that.
MAX_RISK_ROWS=2000

class Risk:
    def __init__(self,store):
        self.store=store
        changed=False
        for e in store.risks:
            # A pause that names an account which is no longer stored blocks nothing and
            # cannot be acted on: deleting the account is the resolution.
            if e.get('scope')=='account' and e.get('account_id') and not e.get('resolved_at') and e['account_id'] not in store.records:
                e.update(resolved_at=now(),resolution='account_removed');changed=True
            if e.get('kind')=='request_illegal' and not e.get('resolved_at'):
                # The previous build read 11140 as "risk control on this request" and rested the
                # account for a minute, doubling to fifteen. The vendor's own text says the account
                # is restricted until an appeal is granted, so the old row understates it and kept
                # retrying. Carry it over to the current reading: a longer rest, and the appeal named
                # as the way back.
                e.update(kind='account_restricted',manual=False,scope='account',action=RESTRICTED_ACTION,reclassified_at=now());changed=True
            if e.get('kind')=='permission_denied' and str(e.get('upstream_code'))=='11140':
                # Older builds paused it permanently, which emptied the pool until someone clicked
                # Resume. 11140 is the vendor's account verdict, so it rests for a while and returns
                # by itself unless the verdict repeats.
                e.update(kind='account_restricted',manual=False,scope='account',retry_at=stamp()+RESTRICTED_FIRST,resolved_at=None,resolution=None,action=RESTRICTED_ACTION,reclassified_at=now());changed=True
            if e.get('kind')=='channel_not_allowed' and e.get('scope')=='region' and str(e.get('upstream_code'))=='11128':
                e.update(scope='request',manual=False,retry_at=0,diagnostic_only=True,scope_corrected_at=now());changed=True
            if e.get('kind')=='rate_limited' and e.get('scope')=='region' and e.get('account_id'):
                e.update(scope='account',scope_corrected_at=now());changed=True
            if e.get('kind')=='login_required' and not e.get('resolved_at') and not pool_critical(e.get('path')):
                # An older build paused the whole account because a metering call answered 401, which
                # is also what an edge page, a throttled job and an expired token look like. Nothing
                # about that proves the account cannot serve chat: drop the pause and let the next
                # chat request decide.
                e.update(manual=False,retry_at=stamp(),diagnostic_only=True,scope='account',action=AUX_ACTION,demoted_at=now());changed=True
        if changed:self.save()
        if self.prune(): self.save()
    def prune(self):
        # The file accumulated thousands of dead rows: cooldowns that expired days ago and pauses
        # resolved weeks ago. Anything a human can still act on (manual, unresolved) is kept.
        cutoff=stamp()-7*86400*1000
        def stale(e):
            if e.get('resolved_at'):
                try: return stamp()-datetime.fromisoformat(e['resolved_at'].replace('Z','+00:00')).timestamp()*1000>7*86400*1000
                except (ValueError,TypeError): return False
            return not e.get('manual') and e.get('retry_at',0)<cutoff
        keep=[e for e in self.store.risks if not stale(e)]
        if len(keep)>MAX_RISK_ROWS: keep=keep[-MAX_RISK_ROWS:]
        if len(keep)==len(self.store.risks): return False
        self.store.risks=keep; return True
    def save(self): atomic(self.store.root/'risk-state.json',{'version':1,'entries':self.store.risks})
    def active(self,e): return not e.get('resolved_at') and (e.get('manual') or e.get('retry_at',0)>stamp())
    def matches(self,e,region,account,path):
        if e.get('scope')=='request' or e.get('diagnostic_only'): return False
        if e.get('kind')=='quota_exhausted' and '/chat/' not in path and not path.endswith('/daily-checkin'): return False
        if e.get('kind')=='login_required' and not account and '/auth/' in path and '/refresh' not in path: return False
        if e['scope']=='global': return True
        if e.get('region')!=region: return False
        if e['scope']=='account': return not e.get('account_id') or e['account_id']==account
        if e['scope']=='operation': return e.get('path')==path
        return True
    def block(self,region,account,path):
        hit=None
        for e in self.store.risks:
            if self.active(e) and self.matches(e,region,account,path) and (hit is None or e.get('retry_at',0)>hit.get('retry_at',0)): hit=e
        return hit
    def check(self,region,account,path):
        e=self.block(region,account,path)
        if e: raise Fault(e.get('status',429),e.get('action','Upstream restriction active'),'risk_'+e['kind'],e)
    def served(self,region,account):
        # One 200 from an account ends its failure streak: the transient pause is resolved and the
        # next rejection starts again at the base cooldown instead of a doubled one.
        changed=False
        for e in self.store.risks:
            if e.get('resolved_at') or e.get('manual') or e.get('scope')!='account': continue
            if e.get('region')!=region or e.get('account_id')!=account: continue
            if e['kind'] in ('account_restricted','rate_limited','upstream_unavailable'):
                # A manual restriction is excluded above: only an operator clears that one, because a
                # 200 in between does not mean the vendor lifted an account-wide verdict.
                e.update(resolved_at=now(),resolution='served'); changed=True
        if changed: self.save()
        return changed
    def observe(self,status,body,region,account,path,headers=None,record=None):
        err=body.get('error') if isinstance(body.get('error'),dict) else body
        code=str(err.get('code','')); msg=str(err.get('message',err.get('msg','')))
        # A body the gateway could not parse (an edge page instead of JSON) carries no upstream code:
        # classify on the status alone and say the body was not JSON, instead of storing the gateway's
        # own parse error as if the upstream had sent that code.
        unparsed=code=='invalid_upstream'
        if unparsed: code=''; msg=''
        kind=None; scope='account'; manual=True; delay=0; st=status
        if code=='11128' and 'unapproved channel' in msg.lower(): kind='channel_not_allowed'; scope='request'; manual=False; delay=0; st=status if 400<=status<500 else 400
        elif code=='11140' or 'account has been restricted' in msg.lower() or '账号存在异常' in msg: kind='account_restricted'; scope='account'; manual=False; delay=RESTRICTED_FIRST; st=403
        elif code=='10081': kind='ip_restricted'; scope='global'; st=403
        elif code in ('E0010458','E0010459'): kind='enterprise_quota'; scope='operation'; st=403
        elif re.search('captcha|turing|device.?token|device.?verification|risk.?control|verification.?code|human.?verification|验证码|人机验证|风控',code+' '+msg,re.I): kind='device_verification'; scope='global'; st=403
        elif status==401 or code=='401': kind='login_required'; st=401
        elif status==429 or code=='429' or re.search('rate_limit|too_many_requests',code,re.I): kind='rate_limited'; scope='account'; manual=False; delay=60000; st=429
        elif status==402 or re.search('insufficient_quota|quota_exceeded|insufficient_credits',code,re.I): kind='quota_exhausted'; st=402
        elif status==403 or code=='403': kind='permission_denied'; st=403
        elif status>=500: kind='upstream_unavailable'; scope='region'; manual=False; delay=5000; st=503
        if not kind: return None
        # A side call failing is not evidence that the account cannot serve chat: a WAF page, a
        # throttled background job and a revoked token all look alike on a metering endpoint. Keep the
        # row for diagnosis, but leave the account in the pool and let chat traffic decide its fate.
        # An IP ban and a risk-control verdict are about the account's standing, not the endpoint being
        # reachable, so they keep their full effect wherever they arrive.
        diagnostic=kind not in ('ip_restricted','account_restricted') and not pool_critical(path)
        if diagnostic:
            if scope!='request': scope='account'
            manual=False; delay=0
        # Consecutive: a rejection only extends the streak while the account's previous one is recent.
        # A 200 from that account ends it (see served), so the backoff restarts at the base cooldown
        # instead of inheriting a doubled one from hours ago.
        old=next((e for e in reversed(self.store.risks) if e['kind']==kind and e.get('region')==region and e.get('account_id')==account and bool(e.get('diagnostic_only'))==diagnostic),None)
        window=RESTRICTED_WINDOW if kind=='account_restricted' else 900000
        if old and (old.get('resolved_at') or stamp()-old.get('retry_at',0)>=window): old=None
        count=(old or {}).get('count',0)+1
        if kind=='account_restricted':
            # Half an hour on the first verdict; from the second one on the account is parked until a
            # human appeals, because retrying a restriction the vendor says is account-wide only feeds
            # the unusual-activity signal that caused it.
            delay=RESTRICTED_FIRST if count<2 else RESTRICTED_REPEAT
            if count>=2: manual=True
        else:
            delay=min(delay*2**min(count-1,10),60000 if kind=='upstream_unavailable' else 900000)
        try: delay=max(delay,float((headers or {}).get('Retry-After',0))*1000)
        except ValueError: pass
        e={'id':(old or {}).get('id',uid()),'kind':kind,'scope':scope,'manual':manual,'region':region,'account_id':account,'path':path,'status':st,'retry_at':stamp()+(0 if diagnostic else delay),'count':count,'action':'Resolve the account restriction through official channels, then resume manually.' if manual else 'Upstream is temporarily rate limiting requests; no account switching or replay during cooldown.','upstream_code':code or None,'request_id':(headers or {}).get('X-Request-ID'),'updated_at':now(),**({'diagnostic_only':True} if diagnostic else {}),**({'upstream_body':'non-json'} if unparsed else {})}
        if kind=='channel_not_allowed':e['action']='Upstream rejected this request (11128: unapproved channel). The cause is not yet identified; no automatic replay, no account switching, and no region-wide block on this basis.'
        if kind=='rate_limited':e['action']='Upstream is rate limiting this account; the region keeps serving from the other accounts during the cooldown.'
        if kind=='account_restricted':e['action']=RESTRICTED_APPEAL if manual else RESTRICTED_ACTION
        if kind=='login_required' and not diagnostic and record is not None and not (record.get('auth') or {}).get('refreshToken'):
            # An access-only import cannot be refreshed, so "resolve it officially" points the operator
            # at a path that does not exist for this account.
            e['action']=LOGIN_ACCESS_ONLY
        if diagnostic:e['action']=AUX_ACTION
        if diagnostic and kind=='login_required' and record is not None and not (record.get('auth') or {}).get('refreshToken'):
            e['action']=AUX_ACTION+' If chat is refused the same way, the console token was revoked or expired and cannot be refreshed: import a fresh token with Add account → Import accounts from .txt.'
        if old: old.update(e)
        else: self.store.risks.append(e)
        self.save()
        # The caller gets one line: what failed and whether anything is paused. The row keeps the
        # explanation, so the console toast is not a paragraph of operator notes.
        if diagnostic:
            message='Upstream refused the %s call (HTTP %d). The account is not paused; chat traffic decides.'%(aux_label(path),st)
            if kind=='login_required' and record is not None and not (record.get('auth') or {}).get('refreshToken'):
                message+=' If chat is refused too, import a fresh token.'
            return Fault(st,message,'risk_'+kind,e)
        return Fault(st,e['action'],'risk_'+kind,e)
    def list(self): return [e for e in self.store.risks if self.active(e)]
