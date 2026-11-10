"""Test publication boundaries and private reattachment with synthetic data."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_publication
import export_rules
import materialize_seed
import validate_rules


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def synthetic_private_seed():
    seed = {'seed_id': 'synthetic-publication-test', 'created_on': '2000-01-01', 'github_commit': 'a' * 40,
            'sources': [], 'passages': [], 'rules': [], 'rule_passages': [], 'route_bindings': [],
            'extra_private_metadata': 'This marker must not be exported.'}
    for i in range(13):
        seed['sources'].append({'source_id': 'S' + str(i), 'title': 'Synthetic source ' + str(i),
                                'source_kind': 'classical_primary_body', 'content_sha256': digest(str(i).encode()),
                                'deployment_relative_path': 'synthetic/' + str(i) + '.txt',
                                'local_path': 'intentionally omitted fixture marker'})
    for i in range(43):
        text = 'Synthetic private passage number ' + str(i)
        seed['passages'].append({'passage_id': 'P' + str(i), 'source_id': 'S' + str(i % 13),
                                 'quote': text, 'quote_sha256': digest(text.encode()), 'pdf_pages': [i + 1]})
    for i in range(38):
        seed['rules'].append({'rule_id': 'R' + str(i), 'label': 'Synthetic rule ' + str(i),
                              'original_rule_record': {'preconditions': ['fixture condition'],
                                                       'source': 'private original record content'}})
    for i in range(45):
        seed['rule_passages'].append({'rule_id': 'R' + str(i % 38), 'passage_id': 'P' + str(i % 43),
                                     'support_kind': 'method_basis'})
    for i in range(35):
        seed['route_bindings'].append({'binding_id': 'B' + str(i), 'native_method_exact': 'fixture/' + str(i),
                                      'allowed_result_states': ['determined'],
                                      'rule_ids': ['R' + str(i)] + (['R35', 'R36', 'R37'] if i == 0 else [])})
    return seed


class PublicContractTests(unittest.TestCase):
    def test_real_public_contract_links_and_counts(self):
        catalog = json.loads((ROOT / 'rules/rule_catalog.json').read_text())
        result = validate_rules.validate(catalog)
        self.assertEqual(result['counts'], {'sources': 13, 'passages': 43, 'rules': 38,
                                           'route_bindings': 35, 'rule_passages': 45})
        errors = []
        check_publication.inspect_json(catalog, errors, 'catalog')
        self.assertEqual(errors, [])

    def test_source_code_matches_deployed_identity(self):
        record = json.loads((ROOT / 'verification/code-provenance.json').read_text())
        for name, expected in record['files'].items():
            self.assertEqual(digest((ROOT / name).read_bytes()), expected, name)

    def test_projection_omits_source_text_and_unknown_private_fields(self):
        seed = synthetic_private_seed()
        public = export_rules.project_seed(seed, digest(encode(seed)))
        serialized = json.dumps(public)
        self.assertNotIn(seed['extra_private_metadata'], serialized)
        self.assertNotIn(seed['passages'][0]['quote'], serialized)
        self.assertNotIn('private original record content', serialized)
        self.assertEqual(public['rules'][0]['original_rule_conditions'], {'preconditions': ['fixture condition']})
        validate_rules.validate(public)

    def test_private_materialization_identity_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); seed = synthetic_private_seed(); raw = encode(seed)
            private = root / 'private.json'; private.write_bytes(raw)
            public = root / 'public.json'; public.write_bytes(encode(export_rules.project_seed(seed, digest(raw))))
            output = root / 'restricted' / 'materialized.json'
            result = materialize_seed.materialize(public, private, output)
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(output.read_bytes(), raw)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                materialize_seed.materialize(public, private, output)

    def test_changed_private_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); seed = synthetic_private_seed(); raw = encode(seed)
            public = root / 'public.json'; public.write_bytes(encode(export_rules.project_seed(seed, digest(raw))))
            private = root / 'private.json'; private.write_bytes(raw + b' ')
            output = root / 'output.json'
            with self.assertRaisesRegex(ValueError, 'private_seed_identity_mismatch'):
                materialize_seed.materialize(public, private, output)
            self.assertFalse(output.exists())

    def test_public_rule_tamper_cannot_reattach_private_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); seed = synthetic_private_seed(); raw = encode(seed)
            altered = export_rules.project_seed(seed, digest(raw))
            altered['route_bindings'][0]['native_method_exact'] = 'different-method'
            public = root / 'public.json'; public.write_bytes(encode(altered))
            private = root / 'private.json'; private.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'public_rules_do_not_match_private_seed'):
                materialize_seed.materialize(public, private, root / 'output.json')

    def test_quote_hash_must_match_even_with_consistent_public_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); seed = synthetic_private_seed()
            seed['passages'][0]['quote'] += ' changed without a new quote hash'
            raw = encode(seed)
            private = root / 'private.json'; private.write_bytes(raw)
            public = root / 'public.json'; public.write_bytes(encode(export_rules.project_seed(seed, digest(raw))))
            with self.assertRaisesRegex(ValueError, 'private_quote_identity_mismatch'):
                materialize_seed.materialize(public, private, root / 'output.json')

    def test_broken_rule_link_and_missing_condition_are_rejected(self):
        public = export_rules.project_seed(synthetic_private_seed())
        bad = deepcopy(public); bad['rule_passages'][0]['rule_id'] = 'missing'
        with self.assertRaises(ValueError):
            validate_rules.validate(bad)
        bad = deepcopy(public); bad['route_bindings'][0]['allowed_result_states'] = []
        with self.assertRaises(ValueError):
            validate_rules.validate(bad)

    def test_publication_checks_staged_bytes_even_when_worktree_is_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'rules').mkdir()
            path = root / 'rules' / 'fixture.json'
            path.write_text(json.dumps({'quote': 'synthetic private payload'}))
            subprocess.run(['git', '-C', str(root), 'add', 'rules/fixture.json'], check=True)
            path.write_text('{}')
            self.assertEqual(check_publication.check(root)['status'], 'passed')
            staged = check_publication.check(root, staged_only=True)
            self.assertEqual(staged['status'], 'failed')
            self.assertTrue(any('forbidden data field' in error for error in staged['errors']))


if __name__ == '__main__':
    unittest.main()
