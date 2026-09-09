import importlib.util
import os
import pathlib
import subprocess
import unittest
import yaml
ROOT=pathlib.Path(__file__).resolve().parents[2]
HELM=os.environ.get('HELM','helm')
spec=importlib.util.spec_from_file_location('dev_policy',ROOT/'scripts/development/check_plan.py'); policy=importlib.util.module_from_spec(spec);spec.loader.exec_module(policy)
class DevelopmentTests(unittest.TestCase):
    def render(self,namespace='events-concierge-dev',extra=()):
        return subprocess.run([HELM,'template','ec',str(ROOT/'deploy/helm/events-concierge'),'-n',namespace,'-f',str(ROOT/'deploy/helm/events-concierge/values-development.yaml'),'--set','global.releasePhase=application','--set','global.runtimeProviderReady=true','--set','global.appImage.repository=test/app','--set','global.appImage.digest=sha256:'+'1'*64,'--set','global.frontendImage.repository=test/web','--set','global.frontendImage.digest=sha256:'+'1'*64,*extra],capture_output=True,text=True)
    def test_development_is_private_single_replica_and_unscheduled(self):
        p=self.render();self.assertEqual(p.returncode,0,p.stderr)
        docs=[d for d in yaml.safe_load_all(p.stdout) if d]
        self.assertTrue(any(d['kind']=='Deployment' for d in docs))
        for d in docs:
            self.assertNotIn(d['kind'],{'CronJob','HorizontalPodAutoscaler','Gateway','HTTPRoute'})
            if d['kind']=='Service':self.assertNotEqual(d['spec'].get('type'),'LoadBalancer')
            if d['kind']=='Deployment':
                self.assertEqual(d['spec']['replicas'],1)
                self.assertFalse(d['spec']['template']['spec'].get('initContainers'))
    def test_profile_rejects_other_namespaces_and_public_gateway(self):
        self.assertNotEqual(self.render('production').returncode,0)
        self.assertNotEqual(self.render(extra=('--set','gateway.enabled=true')).returncode,0)
    def test_rejects_managed_services_and_destructive_plans(self):
        for kind, actions in [('google_sql_database_instance',['create']),('google_container_cluster',['delete']),('google_redis_instance',['create'])]:
            p={'resource_changes':[{'address':'bad','type':kind,'change':{'actions':actions,'after':{}}}]}
            self.assertTrue(policy.inspect(p))
    def test_development_plaintext_cannot_escape_test_profile(self):
        from events_concierge.config import Settings
        with self.assertRaises(ValueError):Settings(_env_file=None,env='production',mock_cloud=True,database_connection_mode='development_plaintext')
        with self.assertRaises(ValueError):Settings(_env_file=None,env='development',mock_cloud=False,database_connection_mode='development_plaintext')
