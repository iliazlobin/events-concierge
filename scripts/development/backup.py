#!/usr/bin/env python3
"""Manual quiesced development backup. Restores are always into disposable local databases."""
import argparse,datetime,hashlib,json,os,pathlib,subprocess,tempfile,uuid
NS='events-concierge-dev'; PROJECT='project-9c8cce04-f94d-40fc-aa6'; ACCOUNT='iliazlobin27@gmail.com'
K=os.environ.get('KUBECTL','kubectl')
def run(args,**kw):return subprocess.run(args,check=True,**kw)
def k(*args,**kw):return run([K,'-n',NS,*args],**kw)
def gc(*args,**kw):return run(['gcloud','storage',*args,'--account='+ACCOUNT,'--project='+PROJECT],**kw)
def backup():
    context=subprocess.check_output([K,'config','current-context'],text=True).strip()
    if context!='gke_'+PROJECT+'_us-west1-a_ec-dev':raise SystemExit('Wrong cluster context')
    ident=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    dest='gs://iz27-ec-dev-backups/'+ident
    deployments=json.loads(k('get','deployments','-o','json',capture_output=True).stdout)['items']
    # This namespace is dedicated to development. Quiesce every writer, including Temporal.
    desired={d['metadata']['name']:d['spec']['replicas'] for d in deployments}
    with tempfile.TemporaryDirectory(prefix='ec-dev-backup-') as tmp:
        os.chmod(tmp,0o700);folder=pathlib.Path(tmp);manifest={'id':ident,'databases':{},'replicas':desired}
        try:
            # Stop application writers first; Temporal servers second.
            for name in sorted(desired,key=lambda n:n.startswith('ec-dev-temporal')):
                k('scale','deployment/'+name,'--replicas=0',stdout=subprocess.DEVNULL)
            for name in desired:k('rollout','status','deployment/'+name,'--timeout=180s',stdout=subprocess.DEVNULL)
            # Explicitly wait for all writer pods to terminate; rollout status alone is insufficient.
            for d in deployments:
                selector=','.join(f'{key}={val}' for key,val in d['spec']['selector']['matchLabels'].items())
                k('wait','--for=delete','pod','-l',selector,'--timeout=180s',stdout=subprocess.DEVNULL)
            for store,user,dbs in [('application','ec_owner',['events']),('temporal','temporal',['temporal','temporal_visibility'])]:
                for db in dbs:
                    name=f'{store}-{db}.dump';path=folder/name
                    with path.open('wb') as f:k('exec',f'ec-dev-{store}-postgres-0','--','pg_dump','-U',user,'-d',db,'-Fc','--no-owner','--no-acl',stdout=f)
                    path.chmod(0o600);manifest['databases'][name]={'store':store,'database':db,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                    gc('cp',str(path),dest+'/'+name,stdout=subprocess.DEVNULL)
            # Copy the current payload snapshot while writers are stopped; source versions remain in the versioned bucket.
            gc('rsync','--recursive','gs://iz27-ec-dev-payloads',dest+'/payloads',stdout=subprocess.DEVNULL)
            manifest['images']={d['metadata']['name']:[c['image'] for c in d['spec']['template']['spec']['containers']] for d in deployments}
            manifest['schema']=k('exec','ec-dev-application-postgres-0','--','psql','-U','ec_owner','-d','events','-Atc','select version_num from alembic_version',capture_output=True).stdout.decode().strip()
            (folder/'manifest.json').write_text(json.dumps(manifest,indent=2));gc('cp',str(folder/'manifest.json'),dest+'/manifest.json',stdout=subprocess.DEVNULL)
            # A completion marker is written last; incomplete prefixes are never restore candidates.
            (folder/'COMPLETE').write_text(ident);gc('cp',str(folder/'COMPLETE'),dest+'/COMPLETE',stdout=subprocess.DEVNULL)
        finally:
            for name,count in desired.items():k('scale','deployment/'+name,'--replicas='+str(count),stdout=subprocess.DEVNULL)
    print('Backup complete:',dest)
def verify(uri):
    if not uri.startswith('gs://iz27-ec-dev-backups/') or '..' in uri:raise SystemExit('Use a development backup prefix')
    with tempfile.TemporaryDirectory(prefix='ec-dev-restore-') as tmp:
        os.chmod(tmp,0o700);folder=pathlib.Path(tmp);gc('cp',uri+'/COMPLETE',tmp,stdout=subprocess.DEVNULL);gc('cp',uri+'/manifest.json',tmp,stdout=subprocess.DEVNULL)
        manifest=json.loads((folder/'manifest.json').read_text())
        for name,info in manifest['databases'].items():
            if pathlib.Path(name).name!=name:raise SystemExit('Invalid manifest path')
            gc('cp',uri+'/'+name,tmp,stdout=subprocess.DEVNULL)
            if hashlib.sha256((folder/name).read_bytes()).hexdigest()!=info['sha256']:raise SystemExit('Backup checksum mismatch')
            container='ec-restore-'+uuid.uuid4().hex[:10]
            try:
                run(['docker','run','-d','--name',container,'--network=none','-e','POSTGRES_HOST_AUTH_METHOD=trust','pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b'],stdout=subprocess.DEVNULL)
                import time
                for attempt in range(60):
                    ready=subprocess.run(['docker','exec',container,'pg_isready','-U','postgres'],capture_output=True)
                    if ready.returncode==0:break
                    time.sleep(1)
                else:raise RuntimeError('Isolated restore database did not become ready')
                run(['docker','exec',container,'psql','-U','postgres','-c','CREATE ROLE ec_app; CREATE ROLE ec_owner; CREATE ROLE temporal;'],stdout=subprocess.DEVNULL)
                run(['docker','exec',container,'createdb','-U','postgres','restore'])
                with (folder/name).open('rb') as f:run(['docker','exec','-i',container,'pg_restore','--exit-on-error','--no-owner','--no-acl','-U','postgres','-d','restore'],stdin=f)
                print('Restored and checksum-verified:',name)
            finally:run(['docker','rm','-f',container],stdout=subprocess.DEVNULL)
    print('Isolated database restore passed; application/workflow recovery must also be tested before activation')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['backup','verify']);p.add_argument('uri',nargs='?');a=p.parse_args()
    if a.action=='backup':backup()
    elif a.uri:verify(a.uri)
    else:p.error('verify needs a backup gs:// prefix')
