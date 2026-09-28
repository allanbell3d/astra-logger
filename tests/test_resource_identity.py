import unittest
from astra.classify import classify_event
from astra.fingerprint import build_fingerprint

class ResourceIdentity(unittest.TestCase):
    def test_same_error_for_different_database_paths_does_not_merge(self):
        fingerprints=[]
        for path in ('/srv/a/state.db','/srv/b/state.db'):
            text=f'2026-09-16 12:00:00 ERROR agent.storage: database disk image is malformed: {path}'
            c=classify_event({'text':text,'host':'dell','profile':'same-agent'})
            fingerprints.append(build_fingerprint(c,'dell','same-agent',text)['key'])
        self.assertNotEqual(*fingerprints)
