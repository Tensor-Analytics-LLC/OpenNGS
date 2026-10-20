"""Early, bounded admission without an unbounded thread-pool wait queue."""
import asyncio
import collections
import hashlib
import json
import os
import time
import uuid
from http.cookies import SimpleCookie,CookieError

MESSAGE='Capacity is currently full. Your work is saved. Please try again shortly.'

class Admission:
    def __init__(self,app):
        self.app=app
        self.active=0;self.expensive=0;self.by_tenant=collections.Counter()
        self.windows=collections.OrderedDict()
        self.identities=collections.OrderedDict()
        self.admitted=0;self.rejected=collections.Counter()
        self.limit=int(os.getenv('NGS_API_CONCURRENCY','32'))
        self.expensive_limit=int(os.getenv('NGS_EXPENSIVE_CONCURRENCY','4'))
        self.tenant_limit=int(os.getenv('NGS_TENANT_CONCURRENCY','6'))
    async def reject(self,send,status,reason):
        self.rejected[reason]+=1
        body=json.dumps({'detail':MESSAGE,'reason':reason}).encode()
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),(b'retry-after',b'5'),(b'cache-control',b'no-store')]})
        await send({'type':'http.response.body','body':body})
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        path=scope['path'];headers=dict(scope['headers']);cookie=headers.get(b'cookie',b'')
        tenant=hashlib.sha256(cookie).digest()[:16] if cookie else str(scope.get('client',('anonymous',))[0])
        if cookie and len(cookie)<=4096:
            digest=hashlib.sha256(cookie).digest();cached=self.identities.get(digest)
            if cached and time.monotonic()-cached[1]<30:tenant=cached[0]
            else:
                try:
                    parsed=SimpleCookie();parsed.load(cookie.decode('latin1'));token=parsed.get('ngs_session')
                    if token:
                        from .security import session_for
                        current=await asyncio.to_thread(session_for,token.value)
                        if current:tenant=current['id']
                except CookieError:pass
                self.identities[digest]=(tenant,time.monotonic());self.identities.move_to_end(digest)
                if len(self.identities)>4096:self.identities.popitem(last=False)
        health=path.startswith('/health/')
        expensive=any(x in path for x in ['/export','/index','/plots','/tracks','/probe','/auth/'])
        now=time.monotonic();window=self.windows.get(tenant,(now,0))
        if now-window[0]>=60:window=(now,0)
        self.windows[tenant]=(window[0],window[1]+1);self.windows.move_to_end(tenant)
        if len(self.windows)>4096:self.windows.popitem(last=False)
        if not health and window[1]>=180:return await self.reject(send,429,'request-rate')
        if not health and (self.active>=self.limit or self.by_tenant[tenant]>=self.tenant_limit or (expensive and self.expensive>=self.expensive_limit)):
            return await self.reject(send,503,'concurrency')
        self.active+=1;self.by_tenant[tenant]+=1;self.expensive+=int(expensive);self.admitted+=1
        start=time.monotonic();status=500;started=False
        async def observed(event):
            nonlocal status,started
            if event['type']=='http.response.start':
                started=True;status=event['status']
                event['headers'].extend([(b'x-request-id',uuid.uuid4().hex.encode()),(b'server-timing',f'app;dur={(time.monotonic()-start)*1000:.2f}'.encode())])
            await send(event)
        try:
            # SSE has its own bounded lifetime; large transfers never occupy this API.
            deadline=65 if path.endswith('/events') else (30 if expensive else 8)
            async with asyncio.timeout(deadline):await self.app(scope,receive,observed)
        except TimeoutError:
            if not started:await self.reject(send,503,'deadline')
        finally:
            self.active-=1;self.by_tenant[tenant]-=1;self.expensive-=int(expensive)
            if not self.by_tenant[tenant]:del self.by_tenant[tenant]
            # No URL query, cookies, bodies, email, variants or secret values.
            if path.startswith('/api/') and (status>=400 or hash(tenant)%20==0):
                print(json.dumps({'at':time.time(),'severity':'warning' if status>=400 else 'info','event':'http.request','status':status,'duration_ms':round((time.monotonic()-start)*1000,2)}),flush=True)
