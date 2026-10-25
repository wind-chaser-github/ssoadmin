import json
from pathlib import Path
import tempfile
import unittest
from deploy.english_names import EnglishNames


class EnglishNamesTests(unittest.TestCase):
    def test_identity_based_names_and_no_private_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mappings = {'version': 1, 'mappings': [
                {'centralId': 12, 'localId': 'parent', 'token': 'SECRET'},
                {'centralId': 13, 'localId': 'child', 'token': 'SECRET'},
                {'centralId': 14, 'localId': 'other'},
            ]}
            family = {'version': 1, 'learners': [
                {'ownerUserId': 'parent', 'boundStudentUserId': 'child', 'displayName': 'Alex', 'progress': ['PRIVATE']},
                {'ownerUserId': 'parent', 'displayName': 'Emma'},
                {'ownerUserId': 'other', 'displayName': 'Alex'},
                {'ownerUserId': 'other', 'displayName': '小学习者'},
            ]}
            (root / 'unified-identity.json').write_text(json.dumps(mappings), encoding='utf-8')
            (root / 'family.json').write_text(json.dumps(family), encoding='utf-8')
            names = EnglishNames(root)
            self.assertEqual(names(), {12: ['Alex', 'Emma'], 13: ['Alex'], 14: ['Alex']})
            self.assertNotIn('SECRET', json.dumps(names()))
            self.assertNotIn('PRIVATE', json.dumps(names()))
            (root / 'family.json').write_text('broken', encoding='utf-8')
            self.assertEqual(names(), {})

    def test_missing_state_does_not_block_account_list(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(EnglishNames(directory)(), {})
