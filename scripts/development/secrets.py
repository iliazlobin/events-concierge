#!/usr/bin/env python3
"""Initialize dev-only random credentials in Secret Manager and project store copies to K8s.
No values are accepted from Git, printed, or written to Terraform state. Existing versions are reused.
"""
import argparse, base64, json, os, secrets, subprocess
PROJECT='project-9c8cce04-f94d-40fc-aa6'; ACCOUNT='iliazlobin27@gmail.com'; NS='events-concierge-dev'
def gc(*args,input=None,check=True):
    return subprocess.run(['gcloud',*args,'--account='+ACCOUNT,'--project='+PROJECT,'--quiet'],input=input,capture_output=True,check=check)
def read_or_create(name, factory):
    versions=gc('secrets','versions','list',name,'--filter=state:ENABLED','--format=json')
    if json.loads(versions.stdout):
        enabled=json.loads(versions.stdout)
        if len(enabled)!=1 or enabled[0]['name'].split('/')[-1]!='1':
            raise SystemExit('Secret rotation requires an explicit pinned-version release')
        return gc('secrets','versions','access','1','--secret='+name).stdout.decode()
    value=factory();gc('secrets','versions','add',name,'--data-file=-',input=value.encode());return value
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--project-to-kubernetes',action='store_true');args=parser.parse_args()
    vals={}
    for key in ['postgres-admin','temporal-postgres-admin','app-role-password','redis-password']:
        vals[key]=read_or_create('ec-dev-'+key,lambda:secrets.token_hex(32))
    urls={
      'database-url': 'postgresql+psycopg://ec_app:'+vals['app-role-password']+'@ec-dev-application-postgres:5432/events',
      'migration-url': 'postgresql+psycopg://ec_owner:'+vals['postgres-admin']+'@ec-dev-application-postgres:5432/events',
      'redis-url': 'redis://:'+vals['redis-password']+'@ec-dev-redis:6379/0',
    }
    for key,value in urls.items():
        actual=read_or_create('ec-dev-'+key,lambda v=value:v)
        if actual!=value:raise SystemExit('Existing secret references differ; review rotation instead of overwriting')
    if args.project_to_kubernetes:
        kubectl=os.environ.get('KUBECTL','kubectl')
        context=subprocess.check_output([kubectl,'config','current-context'],text=True).strip()
        if context!='gke_'+PROJECT+'_us-west1-a_ec-dev':
            raise SystemExit('Select the dedicated development cluster context first')
        for name,key in [('ec-dev-application-postgres','postgres-admin'),('ec-dev-temporal-postgres','temporal-postgres-admin'),('ec-dev-redis','redis-password')]:
            document={'apiVersion':'v1','kind':'Secret','metadata':{'name':name,'namespace':NS},'type':'Opaque','data':{'password':base64.b64encode(vals[key].encode()).decode()}}
            subprocess.run([kubectl,'-n',NS,'apply','--server-side','--field-manager=ec-dev-secrets','-f','-'],input=json.dumps(document).encode(),stdout=subprocess.DEVNULL,check=True)
    print('Development credentials initialized; values not displayed')
