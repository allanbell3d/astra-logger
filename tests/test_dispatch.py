import importlib.util
import unittest
from datetime import datetime,timezone

class Dispatch(unittest.TestCase):
    def test_native_one_shot_rca_is_rearmed_once_not_interval_triggered(self):
        self.assertIsNotNone(importlib.util.find_spec('astra.dispatch'))
        from astra.dispatch import dispatch_once
        jobs={'rca':{'id':'rca','enabled':False,'state':'completed','schedule':{'kind':'once'}},'triage':{'id':'triage','enabled':True,'state':'scheduled'}}
        calls=[]
        def rearm(id,when):calls.append(('rearm',id));jobs[id].update(enabled=True,state='scheduled')
        status={'dispatch_rca_a':True,'dispatch_triage_early':True}
        for _ in range(2):dispatch_once(status,{'rca':'rca','triage':'triage'},jobs.get,lambda id:calls.append(('trigger',id)),rearm,datetime(2026,9,16,tzinfo=timezone.utc))
        self.assertEqual(calls,[('rearm','rca')])

    def test_capture_never_wakes_triage(self):
        from astra.dispatch import dispatch_once
        calls=[]
        jobs={'triage':{'id':'triage','enabled':True,'state':'scheduled'}}
        dispatch_once({'dispatch_triage_early':True},{'triage':'triage'},jobs.get,lambda *x:calls.append(x),lambda *x:calls.append(x))
        self.assertEqual(calls,[])

    def test_operator_pause_and_quiet_work_do_not_dispatch(self):
        from astra.dispatch import dispatch_once
        jobs={'rca':{'id':'rca','enabled':False,'state':'paused','paused_reason':'Operator maintenance','schedule':{'kind':'once'}},'triage':{'id':'triage','enabled':True,'state':'scheduled'}}
        calls=[]
        for status in ({'dispatch_rca_a':True},{}):
            dispatch_once(status,{'rca':'rca','triage':'triage'},jobs.get,lambda *x:calls.append(x),lambda *x:calls.append(x))
        self.assertEqual(calls,[])
