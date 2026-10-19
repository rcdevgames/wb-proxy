import base64, hashlib, json, os, secrets, time, uuid
from pathlib import Path
from datetime import datetime, timezone
from .crypto import AESGCM

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def stamp(): return int(time.time()*1000)
def uid(prefix=''): return prefix+uuid.uuid4().hex
def atomic(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    try: os.chmod(tmp,0o600)
    except OSError: pass
    os.replace(tmp,path)
def read(path,default):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).exists() else default
class Fault(Exception):
    def __init__(self,status,message,code='invalid_request',risk=None,retry_after=None):
        super().__init__(message); self.status=status; self.code=code; self.risk=risk; self.retry_after=retry_after
def require(ok,message,status=400,code='invalid_request'):
    if not ok: raise Fault(status,message,code)
def unpack(body):
    require(isinstance(body,dict),'Invalid upstream JSON',502,'invalid_upstream')
    require(not body.get('error') and body.get('code') in (None,0,200,'0','200'),'Official API rejected request',502,'upstream_error')
    return body.get('data',body)
def safe_error(e): return {'message':str(e) if isinstance(e,Fault) else 'Request failed','code':getattr(e,'code','request_failed'),'status':getattr(e,'status',502)}
# A key may run this many requests at once unless it says otherwise.
DEFAULT_MAX_CONCURRENCY=2
def key_limits(body,base=None):
    out={'models':[],'rpm':0,'max_concurrency':DEFAULT_MAX_CONCURRENCY}
    out.update(base or {})
    if 'models' in body:
        raw=body['models'] if body['models'] is not None else []
        require(isinstance(raw,list),'models must be a list of model IDs')
        patterns=[]
        for item in raw:
            require(isinstance(item,str),'Model IDs must be strings')
            item=item.strip(); require(0<len(item)<=120,'Invalid model ID')
            if item not in patterns: patterns.append(item)
        require(len(patterns)<=200,'Too many models on one key')
        out['models']=patterns
    for field,ceiling in (('rpm',100000),('max_concurrency',1000)):
        if field in body:
            try: value=int(body[field] or 0)
            except (TypeError,ValueError): raise Fault(400,field+' must be a whole number')
            require(0<=value<=ceiling,field+' must be between 0 and '+str(ceiling))
            out[field]=value
    return out
class Store:
    def __init__(self,root):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        keyfile=self.root/'master.key'
        env=os.environ.get('WB_MASTER_KEY')
        if not keyfile.exists() and not env:
            keyfile.write_text(base64.b64encode(secrets.token_bytes(32)).decode(),encoding='ascii')
        self.key=base64.b64decode(env or keyfile.read_text().strip(),validate=True)
        require(len(self.key)==32,'Invalid master key',500)
        path=self.root/'accounts.enc.json'; self.records={}
        if path.exists():
            b=read(path,{})
            raw=AESGCM(self.key).decrypt(base64.b64decode(b['iv']),base64.b64decode(b['data'])+base64.b64decode(b['tag']),b'workbuddy-proxy-v1')
            rows=json.loads(raw); require(isinstance(rows,list),'Invalid credential store',500)
            self.records={r['id']:r for r in rows}
        self.keys=read(self.root/'api-keys.json',{'version':1,'keys':[]})['keys']
        for k in self.keys: k.update(key_limits({},k))
        self.risks=read(self.root/'risk-state.json',{'version':1,'entries':[]})['entries']
        self.logs=[]
        lp=self.root/'call-logs.jsonl'
        if lp.exists():
            for line in lp.read_text(encoding='utf-8-sig').splitlines():
                try: self.logs.append(json.loads(line))
                except ValueError: pass
        self.logs=self.logs[-10000:]
    def save(self):
        iv=secrets.token_bytes(12); raw=json.dumps(list(self.records.values()),ensure_ascii=False).encode()
        encrypted=AESGCM(self.key).encrypt(iv,raw,b'workbuddy-proxy-v1')
        atomic(self.root/'accounts.enc.json',{'version':1,'iv':base64.b64encode(iv).decode(),'tag':base64.b64encode(encrypted[-16:]).decode(),'data':base64.b64encode(encrypted[:-16]).decode()})
    def put(self,record): self.records[record['id']]=record; self.save()
    def public(self,r): return {'id':r['id'],'region':r['region'],'account':r.get('account',{}),'expires_at':r.get('auth',{}).get('expiresAt'),'refreshable':bool(r.get('auth',{}).get('refreshToken')),'label':r.get('import_label')}
    def save_keys(self): atomic(self.root/'api-keys.json',{'version':1,'keys':self.keys})
    def public_keys(self): return [{k:v for k,v in r.items() if k!='digest'} for r in self.keys]
    def new_key(self,b):
        name=b.get('name','').strip(); scope=b.get('scope','all')
        require(0<len(name)<=80,'Key name required'); require(scope in ('cn','intl','all'),'Invalid scope')
        expiry=b.get('expires_at')
        if expiry:
            try: valid=datetime.fromisoformat(expiry.replace('Z','+00:00')).timestamp()>time.time()
            except (ValueError,TypeError): valid=False
            require(valid,'Invalid key expiration')
        secret='sk-wb-'+secrets.token_urlsafe(32)
        r={'id':str(uuid.uuid4()),'name':name,'scope':scope,'prefix':secret[:14],'digest':hashlib.sha256(secret.encode()).hexdigest(),'enabled':True,'created_at':now(),'expires_at':expiry,'last_used_at':None,**key_limits(b)}
        self.keys.append(r); self.save_keys(); return {**{k:v for k,v in r.items() if k!='digest'},'key':secret}
    def update_key(self,key_id,body):
        row=next((k for k in self.keys if k['id']==key_id),None)
        if row is None: raise Fault(404,'API key not found','key_not_found')
        if 'name' in body:
            name=str(body['name'] or '').strip(); require(0<len(name)<=80,'Key name required'); row['name']=name
        if 'enabled' in body: row['enabled']=bool(body['enabled'])
        if 'expires_at' in body: row['expires_at']=body['expires_at'] or None
        row.update(key_limits(body,row)); self.save_keys()
        return {k:v for k,v in row.items() if k!='digest'}
    def authenticate(self,secret):
        digest=hashlib.sha256(secret.encode()).hexdigest()
        for r in self.keys:
            if secrets.compare_digest(r['digest'],digest) and r.get('enabled') and (not r.get('expires_at') or r['expires_at']>now()):
                if not r.get('last_used_at') or time.time()-datetime.fromisoformat(r['last_used_at'].replace('Z','+00:00')).timestamp()>60:
                    r['last_used_at']=now(); self.save_keys()
                return r
        raise Fault(401,'Invalid or expired API key','invalid_api_key')
    def log(self,row):
        self.logs.append(row); self.logs=self.logs[-10000:]
        with (self.root/'call-logs.jsonl').open('a',encoding='utf-8') as f: f.write(json.dumps(row,ensure_ascii=False)+'\n')
        if len(self.logs)%200==0:
            cutoff=time.time()-30*86400
            self.logs=[r for r in self.logs if datetime.fromisoformat(r['started_at'].replace('Z','+00:00')).timestamp()>cutoff][-10000:]
            (self.root/'call-logs.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in self.logs),encoding='utf-8')
