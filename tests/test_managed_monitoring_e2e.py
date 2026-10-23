"""Disposable real VM/vmagent/vmalert lifecycle with synthetic HTTP services.

Opt in with MONITORING_MANAGED_E2E=1. Images are pinned to the workspace's
VictoriaMetrics release. Every container/network belongs to this invocation;
the exporter, Proxy replies and Perses server contain only synthetic data.
"""
import asyncio
import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

import httpx
import pytest

from monitoring.controller import Controller, Publisher, Settings
from monitoring.probe_worker import ManagedProbes
from monitoring.provisioning import atomic_json
from monitoring import api, probes, source_registry

pytestmark = pytest.mark.skipif(os.environ.get('MONITORING_MANAGED_E2E') != '1',
                               reason='Opt-in disposable managed monitoring stack required')
ROOT = Path(__file__).resolve().parents[1]
VERSION = 'v1.151.0'


class SyntheticServices:
    def __init__(self):
        self.password = 'first-password'
        self.resources = {}
        self.generations = []
        self.authenticated_reads = 0
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body, content_type='application/json', headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith('/api/v1/'):
                    with owner.lock:
                        value = ([copy.deepcopy(value) for key, value in owner.resources.items()
                                  if key.rsplit('/', 1)[0] == self.path]
                                 if self.path.endswith(('/dashboards', '/datasources')) else copy.deepcopy(owner.resources.get(self.path)))
                    if isinstance(value, list):
                        self.reply(200, value)
                        return
                    self.reply(200 if value else 404, value or {'error':'not_found'})
                    return
                if self.path.startswith('/alpha/') and self.path.endswith('/metrics'):
                    expected = 'Basic ' + base64.b64encode(('reader:' + owner.password).encode()).decode()
                    if self.headers.get('Authorization') != expected:
                        self.reply(401, {'error':'auth_required'})
                        return
                    owner.authenticated_reads += 1
                if self.path.endswith('/metrics'):
                    self.reply(200, owner.metrics(self.path), 'text/plain; version=0.0.4')
                    return
                self.reply(404, {'error':'not_found'})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or '{}')
                if self.path.endswith('/v1/chat/completions'):
                    name = self.path.split('/')[2]
                    with owner.lock:
                        owner.generations.append((self.path,time.time()))
                    value = {'choices':[{'index':0,'delta':{'content':'synthetic-ok'},'finish_reason':'stop'}]}
                    self.reply(200, ('data: '+json.dumps(value)+'\n\ndata: [DONE]\n\n').encode(),
                               'text/event-stream', {'X-Aigate-Backend':'router-'+name})
                    return
                if self.path.startswith('/api/v1/'):
                    key = self.path + '/' + body['metadata']['name']
                    with owner.lock:
                        owner.resources[key] = body
                    self.reply(200, body)
                    return
                self.reply(404, {'error':'not_found'})

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or '{}')
                with owner.lock:
                    owner.resources[self.path] = body
                self.reply(200, body)

            def do_DELETE(self):
                with owner.lock:
                    value = owner.resources.pop(self.path, None)
                self.reply(200 if value else 404, {})

        self.server = ThreadingHTTPServer(('0.0.0.0',0),Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def metrics(self, path):
        count = int(time.time())
        if path.startswith('/proxy/'):
            return ('aigate_requests_routed_total{request_scope="all",backend="fixture",model="fixture"} '
                    + str(count) + '\naigate_profile_counter_start_time_seconds 1\n').encode()
        lines = [f'vllm:request_success_total {count}', f'vllm:prompt_tokens_total {count*10}',
                 f'vllm:generation_tokens_total {count*20}', f'vllm:num_requests_running 1']
        for metric in ('time_to_first_token_seconds','inter_token_latency_seconds','e2e_request_latency_seconds'):
            for bound, value in (('0.1',count//4),('1',count*9//10),('+Inf',count)):
                lines.append(f'vllm:{metric}_bucket{{le="{bound}"}} {value}')
            lines.extend([f'vllm:{metric}_count {count}', f'vllm:{metric}_sum {count//2}'])
        return ('\n'.join(lines)+'\n').encode()


class HostRouting(httpx.AsyncBaseTransport):
    """Docker's host alias and the host-side controller reach the same fixture."""
    def __init__(self):
        self.transport = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        if request.url.host == 'host.docker.internal':
            request = httpx.Request(request.method, request.url.copy_with(host='127.0.0.1'),
                                    headers=request.headers, stream=request.stream, extensions=request.extensions)
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        await self.transport.aclose()


class Stack:
    def __init__(self, state):
        self.state = state
        self.prefix = 'monitoring-managed-test-' + uuid.uuid4().hex[:10]
        self.containers = []
        self.logs = {}
        self.identities = {}
        self.network = False

    def docker(self, *args):
        return subprocess.check_output(['docker', *args],text=True,stderr=subprocess.STDOUT).strip()

    def run(self, suffix, image, port, args, *, mounts=(), tmpfs=()):
        name = self.prefix + '-' + suffix
        self.docker('run','-d','--name',name,'--network',self.prefix,'--cpus','1','--memory','512m',
                    '--label','monitoring.purpose=managed-e2e','-p',f'127.0.0.1::{port}',
                    *[part for mount in mounts for part in ('-v',mount)],
                    *[part for mount in tmpfs for part in ('--tmpfs',mount)],image,*args)
        self.containers.append(name)
        value = json.loads(self.docker('inspect',name))[0]
        self.identities[suffix] = {'image':value['Image'],'reference':image}
        mapped = value['NetworkSettings']['Ports'][str(port)+'/tcp'][0]['HostPort']
        return name, 'http://127.0.0.1:'+mapped

    def start(self):
        for repository in ('victoria-metrics','vmagent','vmalert'):
            self.docker('image','inspect','victoriametrics/'+repository+':'+VERSION)
        self.docker('network','create',self.prefix)
        self.network = True
        self.vm_name,self.vm = self.run('vm','victoriametrics/victoria-metrics:'+VERSION,8428,
            ['-httpListenAddr=:8428','-storageDataPath=/storage','-retentionPeriod=1d',
             '-memory.allowedBytes=256MiB','-storage.minFreeDiskSpaceBytes=16MiB'],
            tmpfs=['/storage:rw,nosuid,nodev,size=268435456'])
        atomic_json(self.state/'scrape.json',{'global':{'scrape_interval':'5s'},'scrape_configs':[]})
        atomic_json(self.state/'rules.json',{'groups':[]})
        mount=str(self.state)+':'+str(self.state)+':ro'
        self.agent_name,self.agent = self.run('agent','victoriametrics/vmagent:'+VERSION,8429,
            ['-httpListenAddr=:8429','-promscrape.config='+str(self.state/'scrape.json'),
             '-remoteWrite.url=http://'+self.vm_name+':8428/api/v1/write','-remoteWrite.tmpDataPath=/buffer',
             '-remoteWrite.flushInterval=1s','-promscrape.suppressScrapeErrors','-memory.allowedBytes=128MiB'],
            mounts=[mount],tmpfs=['/buffer:rw,nosuid,nodev,size=67108864'])
        self.alert_name,self.alert = self.run('alert','victoriametrics/vmalert:'+VERSION,8880,
            ['-httpListenAddr=:8880','-rule='+str(self.state/'rules.json'),
             '-datasource.url=http://'+self.vm_name+':8428','-remoteWrite.url=http://'+self.vm_name+':8428',
             '-remoteWrite.flushInterval=1s','-datasource.queryStep=0'],mounts=[mount])
        wrappers=[]
        for component,name,binary in [('agent',self.agent_name,'/vmagent-prod'),('alert',self.alert_name,'/vmalert-prod')]:
            path=self.state/('validate-'+component)
            path.write_text('#!/bin/sh\nexec docker exec '+name+' '+binary+' "$@"\n')
            path.chmod(0o700)
            wrappers.append(str(path))
        self.agent_binary,self.alert_binary=wrappers

    def close(self):
        for name in reversed(self.containers):
            try:
                self.logs[name]=self.docker('logs',name)
            finally:
                self.docker('rm','-f',name)
        if self.network:
            self.docker('network','rm',self.prefix)
        atomic_json(self.state/'container-logs.json',self.logs)


def desired(port, revision=1, password='first-password'):
    foundations=[]
    gateways=[]
    for name in ('alpha','beta'):
        base='http://host.docker.internal:'+str(port)
        foundations.append({'id':name,'name':name,'monitoring_source':'foundation-'+name,
            'monitoring_adapter':'a3-vllm-v1','enabled':True,
            'resource_nodes':[{'name':name+'-'+role,'role':role} for role in ('prefill','decode')],
            'monitoring_endpoints':[{'id':name+'-'+role,'name':role,'type':'inference','node':name+'-'+role,
                'role':role,'url':base+'/'+name+'/'+role+'/metrics',
                'auth':{'type':'basic','username':'reader','password':password} if name=='alpha' else {'type':'none'}}
                for role in ('prefill','decode')],
            'router':{'id':'router-'+name,'proxy_url':base+'/proxy/'+name,
                      'profile_url':base+'/proxy/'+name,'maintenance':False}})
        gateways.append({'id':'entry-'+name,'name':'entry-'+name,'business_url':base+'/entry/'+name,
                         'router_id':'router-'+name,'enabled':True,'admission':True})
    return {'schema_version':1,'revision':revision,'foundations':foundations,'gateways':gateways}


async def wait_until(check, timeout=60):
    deadline=time.monotonic()+timeout
    last=None
    while time.monotonic()<deadline:
        try:
            last=await check()
            if last:return last
        except (httpx.HTTPError,ValueError,KeyError):
            pass
        await asyncio.sleep(.25)
    raise AssertionError('Managed E2E condition timed out: '+str(last))


@pytest.mark.asyncio
async def test_managed_end_to_end_publish_rotate_pause_disable(tmp_path,monkeypatch):
    state=tmp_path/'managed';state.mkdir()
    stack=Stack(state);services=SyntheticServices()
    publisher=None;worker=None;worker_task=None;heartbeat_task=None
    try:
        await asyncio.to_thread(stack.start)
        async with httpx.AsyncClient(trust_env=False) as client:
            for url in (stack.vm,stack.agent,stack.alert):
                async def ready(base=url):
                    return (await client.get(base+'/health')).status_code==200
                await wait_until(ready,30)
        monkeypatch.setenv('STATE_DIR',str(state))
        monkeypatch.setenv('MONITORING_CATALOG_FILE',str(state/'catalog.json'))
        monkeypatch.setenv('MONITORING_PROBE_CATALOG_FILE',str(state/'probes-catalog.json'))
        monkeypatch.setenv('MONITORING_PROBE_CONTROL_FILE',str(state/'probes-control.json'))
        monkeypatch.setenv('MONITORING_PROBE_STATUS_FILE',str(state/'probes-runtime.json'))
        settings=Settings(state=state,catalog=state/'catalog.json',control=state/'probes-control.json',
            probe_status=state/'probes-runtime.json',token='synthetic-token-'*3,allowed_clients={'127.0.0.1'},
            templates=ROOT/'perses',vmagent=stack.agent,vmalert=stack.alert,vm=stack.vm,
            perses='http://127.0.0.1:'+str(services.port),vmagent_binary=stack.agent_binary,
            vmalert_binary=stack.alert_binary,require_tls=False)
        publisher=Publisher(settings,httpx.AsyncClient(transport=HostRouting(),trust_env=False,timeout=10))
        controller=Controller(settings,publisher)
        body=desired(services.port)
        await controller.accept(body)
        offset=(int(time.time())+3)%60
        atomic_json(state/'probe-offsets.json',{kind+':'+identity:offset for kind,identity in
            [('foundation','alpha'),('foundation','beta'),('gateway','entry-alpha'),('gateway','entry-beta')]})
        worker=ManagedProbes(publisher.client,stack.vm)
        worker_task=asyncio.create_task(worker.run())
        async def heartbeat():
            while True:
                await controller.accept(body)
                await asyncio.sleep(10)
        heartbeat_task=asyncio.create_task(heartbeat())
        async def applied():
            await controller.reconcile()
            return controller.status() if controller.status()['applied_revision']==body['revision'] else None
        await wait_until(applied,60)
        dashboards=[v for v in services.resources.values() if v['kind']=='Dashboard']
        dashboard_count=len(dashboards)
        assert dashboard_count>=8
        assert {v['metadata']['project'] for v in dashboards}=={'foundation-alpha','foundation-beta'}
        assert 'first-password' not in (state/'desired.json').read_text()
        assert 'first-password' not in (state/'catalog.json').read_text()
        async def recorded():
            response=await publisher.client.get(stack.vm+'/api/v1/query',params={
                'query':'monitoring_native_backend_requests_per_second{environment=~"foundation-(alpha|beta)"}',
                'nocache':1})
            rows=response.json()['data']['result']
            return rows if {r['metric']['environment'] for r in rows}=={'foundation-alpha','foundation-beta'} else None
        rows=await wait_until(recorded,60)
        assert all(float(row['value'][1])>0 for row in rows)
        observations=await probes.history(publisher.client,stack.vm)
        assert len(observations['services'])==4
        async def probe_results():
            value=await probes.history(publisher.client,stack.vm)
            return value if all(service['latest'] for service in value['services']) else None
        observations=await wait_until(probe_results,30)
        assert all(service['latest']['status']=='success' for service in observations['services'])
        assert {service['probe_kind'] for service in observations['services']}=={'foundation','gateway'}
        # Real scrape and query paths consume the same arbitrary source directory.
        assert set(source_registry.sources())=={'foundation-alpha','foundation-beta'}
        service=api.Service('foundation-alpha')
        monkeypatch.setattr(api,'VM',stack.vm)
        try:
            async def api_fresh():
                await service.cycle()
                value=service.latest['nodes']['prefill']['metrics']['data']['rates']['requests']
                return value is not None and value>0
            await wait_until(api_fresh,40)
            assert service.latest['nodes']['prefill']['metrics']['data']['rates']['requests']>0
        finally:
            await service.close()
        # Rotation must invalidate scrape configuration and continue writing.
        services.password='  second-password \t\r\n '
        body=desired(services.port,2,password=services.password)
        await controller.accept(body)
        await wait_until(applied,60)
        async def all_exporters_up():
            current=(await publisher.client.get(stack.agent+'/api/v1/targets')).json()['data']['activeTargets']
            rows=[row for row in current if row['labels'].get('environment')=='foundation-alpha']
            return rows if rows and all(row['health']=='up' for row in rows) else None
        targets=await wait_until(all_exporters_up,15)
        inference=[row for row in targets if row['labels']['job']=='vllm-a3']
        assert {row['labels']['instance'].split(str(services.port),1)[1] for row in inference}=={
            '/alpha/prefill/metrics','/alpha/decode/metrics'}
        secret=controller.desired['catalog']['foundations'][0]['monitoring_endpoints'][0]['auth']['authorization_file']
        assert base64.b64decode(Path(secret).read_bytes())==('reader:'+services.password).encode('utf-8')
        assert services.authenticated_reads>=4
        assert 'second-password' not in json.dumps(controller.status())
        assert 'second-password' not in (state/'scrape.json').read_text()
        # Maintenance acknowledges stopped dispatch through the real scheduler.
        acknowledgement=await controller.pause('router-alpha','maintenance-1')
        assert acknowledgement['dispatch_stopped']
        assert worker.targets['foundation:alpha']['pause_reason']=='maintenance'
        await controller.resume('router-alpha','restore-1')
        async def resumed():
            return worker.targets['foundation:alpha']['dispatch_status']=='active'
        await wait_until(resumed,5)
        # Retire all project content, including custom dashboards, preserving VM history.
        custom = copy.deepcopy(next(v for v in services.resources.values()
                                    if v['kind'] == 'Dashboard' and v['metadata']['project'] == 'foundation-alpha'))
        custom['metadata']['name'] = 'manual-retirement-check'
        services.resources['/api/v1/projects/foundation-alpha/dashboards/manual-retirement-check'] = custom
        body=desired(services.port,3,password=services.password)
        body['foundations'][0]['enabled']=False
        await controller.accept(body)
        await wait_until(applied,60)
        current=(await publisher.client.get(stack.agent+'/api/v1/targets')).json()['data']['activeTargets']
        assert all(row['labels'].get('environment')!='foundation-alpha' for row in current)
        assert set(source_registry.sources())=={'foundation-beta'}
        assert not any(key.startswith('/api/v1/projects/foundation-alpha') for key in services.resources)
        assert {v['metadata']['project'] for v in services.resources.values() if v['kind']=='Dashboard'} == {'foundation-beta'}
        assert 'foundation:alpha' not in worker.targets and 'gateway:entry-alpha' not in worker.targets
        assert {s['probe_id'] for s in (await probes.history(publisher.client,stack.vm))['services']} == {'foundation:beta','gateway:entry-beta'}
        history=(await publisher.client.get(stack.vm+'/api/v1/query',params={
            'query':'count_over_time(vllm:request_success_total{environment="foundation-alpha"}[10m])','nocache':1})).json()
        assert history['data']['result']
        # Removing the remaining source also converges to an empty active catalog.
        body={'schema_version':1,'revision':4,'foundations':[],'gateways':[]}
        await controller.accept(body)
        await wait_until(applied,60)
        assert source_registry.sources()=={}
        assert not services.resources
        atomic_json(state/'e2e-result.json',{'status':'passed','revisions':4,'dashboards':dashboard_count,
            'synthetic_generations':len(services.generations),'authenticated_reads':services.authenticated_reads,
            'vm':stack.vm,'vmagent':stack.agent,'vmalert':stack.alert,'images':stack.identities,
            'controller':controller.status()})
    finally:
        for task in (heartbeat_task,worker_task):
            if task:task.cancel()
        await asyncio.gather(*[t for t in (heartbeat_task,worker_task) if t],return_exceptions=True)
        if publisher:await publisher.close()
        services.close()
        await asyncio.to_thread(stack.close)
