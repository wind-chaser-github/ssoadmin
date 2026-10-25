import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
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
            self.assertEqual(EnglishNames(directory).account_profiles(), {})

    def test_account_names_follow_identity_not_children_and_never_export_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b, c = [str(uuid4()) for _ in range(3)]
            (root / 'unified-identity.json').write_text(json.dumps({'version': 1, 'mappings': [
                {'centralId': 12, 'localId': a, 'token': 'SECRET'},
                {'centralId': 13, 'localId': b}, {'centralId': 14, 'localId': c},
            ]}), encoding='utf-8')
            (root / 'users.json').write_text(json.dumps({'version': 1, 'users': [
                {'id': a, 'displayName': 'Old name', 'passwordHash': 'SECRET'},
                {'id': b, 'displayName': '小学习者'},
                {'id': c, 'displayName': '已有昵称'},
            ]}), encoding='utf-8')
            (root / 'account-profiles').mkdir()
            profile = root / 'account-profiles' / (a + '.json')
            value = {'version': 1, 'userId': a, 'nickname': '小明妈妈', 'avatar': ''}
            profile.write_text(json.dumps(value), encoding='utf-8')
            reader = EnglishNames(root)
            self.assertEqual(reader.account_profiles(), {12: {'nickname': '小明妈妈', 'avatar': ''}, 14: {'nickname': '已有昵称', 'avatar': ''}})
            self.assertNotIn('SECRET', json.dumps(reader.account_profiles()))
            value['userId'] = b
            profile.write_text(json.dumps(value), encoding='utf-8')
            self.assertEqual(reader.account_profiles()[12]['nickname'], 'Old name')
            self.assertNotIn(13, reader.account_profiles())
            profile.write_text('broken', encoding='utf-8')
            self.assertEqual(reader.account_profiles()[14]['nickname'], '已有昵称')
