"""Exercise the public index and evidence adapter without a private runtime.

All passages, archive rows, rule IDs and native returns below are synthetic.
They test software contracts, not historical source accuracy or prediction.
The native adapter is mocked only where an end-to-end evidence join needs a
calculation result. No private algorithm or source file is read or executed.
"""
from argparse import Namespace
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import catalog_builder as builder
import catalog_queries as queries
import engine_adapter
import pipeline


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')


def synthetic_seed():
    sources = []
    passages = []
    rules = []
    edges = []
    texts = {
        'alpha': '这是合成证据甲，仅用于检索与引用测试。',
        'diagnostic': '合成诊断乙，不构成公共结论。',
        'engineering': 'Synthetic engineering marker, not a classical source.',
    }
    for name, kind in [('alpha', 'classical_primary_body'),
                       ('diagnostic', 'classical_commentary'),
                       ('engineering', 'engineering_implementation')]:
        sources.append({'source_id': 'S-' + name, 'title': '合成测试资料 ' + name,
                        'edition_label': 'synthetic-fixture-only',
                        'source_kind': kind, 'content_sha256': sha(texts[name].encode()),
                        'deployment_relative_path': 'fixture-sources/' + name + '.txt'})
        passages.append({'passage_id': 'P-' + name, 'source_id': 'S-' + name,
                         'quote': texts[name], 'quote_sha256': sha(texts[name].encode()),
                         'pdf_pages': [1], 'printed_pages': ['fixture-1'],
                         'verification_status': 'synthetic_fixture_only',
                         'source_layer_attribution': 'synthetic-' + kind,
                         'support_scope': 'Software fixture only.',
                         'does_not_support': ['Historical accuracy or actual prediction.']})
        rules.append({'rule_id': 'R-' + name, 'method_id': 'fixture-' + name + '/1',
                      'rule_kind': 'synthetic_fixture', 'status': 'fixture_only'})
        role = 'engineering_basis' if name == 'engineering' else 'method_basis'
        edge = {'rule_id': 'R-' + name, 'passage_id': 'P-' + name,
                'support_kind': role, 'claim_role': role}
        if name == 'engineering':
            edge['applied_rule_claim_requires'] = {
                'path': 'trace.priority.applied', 'operator': 'equals', 'value': True}
        edges.append(edge)
    sources[1].update(media_type='image/png', book_pdf_sha256='b' * 64,
                      source_pdf_page=1, runtime_archive_member_path='fixture/page-1.png',
                      content_identity_policy='Synthetic metadata; not an actual book scan.')
    bindings = []
    for version in (1, 2):
        bindings.append({'binding_id': 'B-alpha-' + str(version),
                         'native_method_exact': 'fixture-alpha/' + str(version),
                         'allowed_result_states': ['determined', 'no_additional_need'],
                         'rule_ids': ['R-alpha'],
                         'required_result_predicates': [
                             {'path': 'trace.route', 'operator': 'equals', 'value': 'alpha'}]})
    bindings.extend([
        {'binding_id': 'B-engineering', 'native_method_exact': 'fixture-alpha/1',
         'allowed_result_states': ['determined'], 'rule_ids': ['R-engineering']},
        {'binding_id': 'B-diagnostic', 'native_method_exact': 'fixture-diagnostic/1',
         'allowed_result_states': ['undetermined'], 'rule_ids': ['R-diagnostic'],
         'usage_context': 'source_diagnostic', 'required_result_predicates': [
             {'path': 'trace.source_diagnostic.task', 'operator': 'equals', 'value': 'A'}]},
    ])
    return {'schema_version': 'synthetic-evidence-fixture-1', 'sources': sources,
            'passages': passages, 'rules': rules, 'rule_passages': edges,
            'route_bindings': bindings}


def determined():
    return {'status': 'determined', 'auxiliary': {'alternatives': [
        {'elements': ['木', '水'], 'primary': '水', 'secondary': '木'},
        {'elements': ['火', '土'], 'primary': None, 'secondary': None},
    ]}, 'reason': 'synthetic calculation fixture', 'trace': {'route': 'alpha'}}


def unresolved():
    return {'status': 'undetermined', 'auxiliary': None,
            'reason': 'synthetic unresolved fixture', 'trace': {}}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gx-evidence-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / 'archive'
        self.repo = self.root / 'empty-upstream-checkout'
        self.archive.mkdir()
        self.repo.mkdir()
        payload = b'synthetic archive object only'
        digest = sha(payload)
        (self.archive / 'object.txt').write_bytes(payload)
        with sqlite3.connect(self.archive / 'manifest.sqlite') as con:
            con.executescript('''
                CREATE TABLE objects(sha256 TEXT PRIMARY KEY,size INTEGER,relative_path TEXT);
                CREATE TABLE entries(path TEXT PRIMARY KEY,kind TEXT,size INTEGER,sha256 TEXT);
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            ''')
            con.execute('INSERT INTO objects VALUES(?,?,?)', (digest, len(payload), 'object.txt'))
            con.execute('INSERT INTO entries VALUES(?,?,?,?)', ('合成目录/fixture.txt', 'file', len(payload), digest))
            con.execute('INSERT INTO entries VALUES(?,?,?,?)', ('合成目录', 'dir', None, None))
            con.execute('INSERT INTO meta VALUES(?,?)', ('fixture', 'not real research data'))
        self.original_manifest_sha = sha((self.archive / 'manifest.sqlite').read_bytes())
        self.seed = synthetic_seed()
        self.seed_file = self.root / 'evidence_seed.json'
        write_json(self.seed_file, self.seed)
        self.methods = self.root / 'methods.json'
        self.routes = self.root / 'routes.json'
        # The existing delivery builder requires these registration counts.
        # These generated IDs have no connection to real accepted methods.
        write_json(self.methods, {'methods': [{'component': 'fixture-method-' + str(i)} for i in range(95)]})
        write_json(self.routes, {'routes': [{'route_id': 'fixture-route-' + str(i)} for i in range(138)]})
        self.database = self.root / 'catalog.sqlite'
        self.args = Namespace(archive=self.archive, repo=self.repo, output=self.database,
                              evidence_seed=self.seed_file, method_registry=self.methods,
                              route_registry=self.routes)
        with redirect_stdout(io.StringIO()):
            self.receipt = builder.build(self.args)

    def row(self, method='fixture-alpha/1', row=None):
        return pipeline.enrich_row(self.database, method, row if row is not None else determined())

    def seal(self):
        release = self.root / 'release'
        release.mkdir()
        (release / 'seed.json').write_bytes(self.seed_file.read_bytes())
        lock = {'files': {'seed.json': sha(self.seed_file.read_bytes())},
                'catalog_sha256': sha(self.database.read_bytes()),
                'seed_sha256': sha(self.seed_file.read_bytes())}
        write_json(release / 'release-lock.json', lock)
        return release, lock

    def test_build_preserves_archive_and_marks_registration_as_not_adoption(self):
        metadata = queries.metadata(self.database)
        self.assertEqual(metadata['counts']['assets'], 1)
        self.assertEqual(metadata['counts']['asset_paths'], 2)
        self.assertEqual(metadata['counts']['registrations'], 233)
        self.assertFalse(metadata['source_manifest']['object_hashes_recomputed_this_build'])
        self.assertEqual(sha((self.archive / 'manifest.sqlite').read_bytes()), self.original_manifest_sha)
        self.assertEqual(queries.find_asset_paths(self.database, '合成目录')[0]['semantic_status'], 'unreviewed')
        rows = queries.find_registrations(self.database, kind='method', limit=1)
        self.assertFalse(rows[0]['product_runtime_adoption_inferred'])
        before = sha(self.database.read_bytes())
        with self.assertRaises(FileExistsError):
            builder.build(self.args)
        self.assertEqual(sha(self.database.read_bytes()), before)

    def test_chinese_search_is_discovery_not_rule_authority(self):
        result = queries.search_passages(self.database, '合成证据甲')
        self.assertEqual([p['passage_id'] for p in result['hits']], ['P-alpha'])
        self.assertFalse(result['establishes_rule_support'])
        self.assertEqual(result['purpose'], 'source_discovery_only')
        self.assertEqual(queries.search_passages(self.database, '诊断')['hits'][0]['passage_id'], 'P-diagnostic')
        self.assertEqual(self.row('fixture-other/1')['classical_citations'], [])
        self.assertEqual(queries.search_passages(self.database, '%')['hits'], [])

    def test_queries_are_read_only_and_limits_are_bounded(self):
        with queries.connect(self.database) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('DELETE FROM passages')
        for limit in (0, 51, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                queries.search_passages(self.database, '合成', limit=limit)

    def test_explicit_method_and_branch_are_required(self):
        expected = self.row()
        self.assertEqual([c['passage_id'] for c in expected['classical_citations']], ['P-alpha'])
        self.assertEqual(expected['evidence_status'], 'verified_method_basis_attached')
        for method in ('fixture-alpha/10', 'fixture-other/1', None):
            with self.subTest(method=method):
                self.assertEqual(self.row(method)['classical_citations'], [])
        wrong = determined()
        wrong['trace']['route'] = 'other'
        self.assertEqual(self.row(row=wrong)['classical_citations'], [])

    def test_unknown_and_outside_results_never_borrow_confirmed_citations(self):
        for state in ('undetermined', 'outside_supported_domain'):
            with self.subTest(status=state):
                raw = unresolved()
                raw.update(status=state, trace={'route': 'alpha'})
                result = self.row(row=raw)
                self.assertEqual(result['status'], state)
                self.assertIsNone(result['auxiliary'])
                self.assertEqual(result['classical_citations'], [])
                self.assertEqual(result['engineering_basis'], [])

    def test_no_additional_need_is_confirmed_and_distinct_from_unknown(self):
        raw = determined()
        raw.update(status='no_additional_need', auxiliary=None)
        result = self.row(row=raw)
        self.assertEqual(result['evidence_status'], 'verified_method_basis_attached')
        self.assertTrue(result['classical_citations'][0]['attached_to_confirmed_result'])
        self.assertIn('无需额外辅助', result['explanation'])

    def test_complete_alternative_groups_and_priority_are_preserved(self):
        raw = determined()
        raw['comparison_to_previous'] = {'status': 'synthetic_changed_auxiliary'}
        before = deepcopy(raw)
        result = self.row(row=raw)
        self.assertEqual(result['auxiliary'], before['auxiliary'])
        self.assertEqual(len(result['auxiliary']['alternatives']), 2)
        self.assertEqual(result['comparison_to_previous'], before['comparison_to_previous'])
        self.assertEqual(raw, before)

    def test_engineering_evidence_requires_actual_marker(self):
        raw = determined()
        self.assertEqual(self.row(row=raw)['engineering_basis'], [])
        raw['trace']['priority'] = {'applied': True}
        result = self.row(row=raw)
        self.assertEqual([c['passage_id'] for c in result['engineering_basis']], ['P-engineering'])
        self.assertTrue(result['engineering_basis'][0]['applied_rule_claim'])
        self.assertEqual([c['passage_id'] for c in result['classical_citations']], ['P-alpha'])
        self.assertFalse(result['classical_citations'][0]['applied_rule_claim'])

    def test_diagnostic_keeps_null_auxiliary_and_source_identity(self):
        raw = unresolved()
        raw['trace']['source_diagnostic'] = {'task': 'A'}
        result = self.row('fixture-diagnostic/1', raw)
        self.assertIsNone(result['auxiliary'])
        self.assertEqual(result['status'], 'undetermined')
        self.assertEqual(result['classical_citations'], [])
        citation = result['diagnostic_citations'][0]
        self.assertFalse(citation['attached_to_confirmed_result'])
        self.assertEqual(citation['source_kind'], 'classical_commentary')
        self.assertEqual(citation['source_layer_attribution'], 'synthetic-classical_commentary')
        self.assertEqual(citation['source_locator']['book_pdf_sha256'], 'b' * 64)
        self.assertEqual(citation['source_locator']['runtime_archive_member_path'], 'fixture/page-1.png')
        raw['trace']['source_diagnostic']['task'] = 'B'
        self.assertEqual(self.row('fixture-diagnostic/1', raw)['diagnostic_citations'], [])

    def test_candidate_context_cannot_promote_public_result(self):
        candidate = determined()
        native = {'method': None, 'status': 'no_current_auxiliary_determined',
                  'input_origin': 'synthetic_test', 'natal': unresolved(), 'timeline': [unresolved()],
                  'adjudication': {'evaluated_candidates': {'candidate': {'method': 'fixture-alpha/1'}}}}
        native['natal']['trace']['views'] = {'candidate': deepcopy(candidate)}
        native['timeline'][0]['trace']['views'] = {'candidate': deepcopy(candidate)}
        before = deepcopy(native)
        with patch.object(pipeline, 'run_native', return_value={'status': 'computed', 'native': native}):
            result = pipeline.analyze({'samples': [{}]}, self.root, self.database,
                                      {'catalog_sha256': 'fixture', 'seed_sha256': 'fixture'}, {'built_at': 'fixture'})
        self.assertEqual(native, before)
        self.assertEqual(result['native_result'], before)
        self.assertIsNone(result['timeline'][0]['auxiliary'])
        self.assertEqual(result['timeline'][0]['classical_citations'], [])
        self.assertEqual(result['chain_summary']['determined_points'], 0)
        self.assertEqual(result['chain_summary']['points_with_verified_method_citations'], 0)
        context = result['evidence_contexts'][0]
        self.assertFalse(context['used_for_public_auxiliary'])
        citation = context['timeline'][0]['classical_citations'][0]
        self.assertEqual(citation['usage_context'], 'candidate_method')
        self.assertFalse(citation['attached_to_confirmed_result'])
        self.assertFalse(citation['used_for_public_auxiliary'])

    def test_source_context_uses_explicit_current_dispatch_without_promotion(self):
        current = determined()
        current['trace']['method'] = 'fixture-alpha/2'
        report = {'method': 'fixture-alpha/1', 'current_method': 'fixture-alpha/2',
                  'natal': determined(), 'timeline': [current]}
        contexts = pipeline.evidence_contexts(self.database, {'source_diagnostics': {'fixture': report}})
        row = contexts[0]['timeline'][0]
        self.assertEqual(row['method'], 'fixture-alpha/2')
        self.assertEqual(row['classical_citations'][0]['binding_id'], 'B-alpha-2')
        self.assertFalse(row['classical_citations'][0]['attached_to_confirmed_result'])

    def test_extension_method_requires_native_authorization_and_matching_result(self):
        raw = determined()
        selected = {key: deepcopy(raw[key]) for key in ('status', 'auxiliary', 'reason')}
        raw['trace']['supplementary_current_tasks'] = {'method': 'fixture-alpha/2', 'result': selected}
        native = {'method': 'fixture-alpha/1'}
        self.assertEqual(pipeline.effective_method(native, raw, current=True), 'fixture-alpha/1')
        raw['trace']['source_authorized_extension'] = True
        self.assertEqual(pipeline.effective_method(native, raw, current=True), 'fixture-alpha/2')
        selected['auxiliary'] = None
        self.assertEqual(pipeline.effective_method(native, raw, current=True), 'fixture-alpha/1')

    def test_corrupt_indexed_passage_is_rejected_on_read(self):
        self.database.chmod(0o600)
        with sqlite3.connect(self.database) as con:
            con.execute("UPDATE passages SET text=? WHERE passage_id=?", ('altered synthetic text', 'P-alpha'))
        with self.assertRaisesRegex(ValueError, 'indexed passage integrity failure'):
            queries.get_passage(self.database, 'P-alpha')

    def test_bad_passage_hash_is_rejected_during_build(self):
        self.seed['passages'][0]['quote'] += 'tampered'
        write_json(self.seed_file, self.seed)
        args = deepcopy(self.args)
        args.output = self.root / 'bad-index.sqlite'
        with self.assertRaisesRegex(ValueError, 'passage text hash mismatch'):
            builder.build(args)
        self.assertFalse(args.output.exists())

    def test_release_seed_tamper_is_rejected(self):
        release, expected = self.seal()
        actual, _ = pipeline.verify_release(release, self.database)
        self.assertEqual(actual, expected)
        (release / 'seed.json').write_bytes(b'altered seed')
        with self.assertRaisesRegex(ValueError, 'release_integrity_failure'):
            pipeline.verify_release(release, self.database)

    def test_release_catalog_tamper_is_rejected(self):
        release, _ = self.seal()
        self.database.chmod(0o600)
        with sqlite3.connect(self.database) as con:
            con.execute("UPDATE meta SET value=? WHERE key='built_at'", (json.dumps('tampered-time'),))
        with self.assertRaisesRegex(ValueError, 'catalog_integrity_failure'):
            pipeline.verify_release(release, self.database)

    def test_catalog_and_seed_pair_cannot_be_mixed(self):
        release, lock = self.seal()
        lock['seed_sha256'] = '0' * 64
        write_json(release / 'release-lock.json', lock)
        with self.assertRaisesRegex(ValueError, 'catalog_seed_identity_mismatch'):
            pipeline.verify_release(release, self.database)

    def test_release_symlink_escape_is_rejected(self):
        release, _ = self.seal()
        (release / 'seed.json').unlink()
        (release / 'seed.json').symlink_to(self.seed_file)
        with self.assertRaisesRegex(ValueError, 'release_path_escape'):
            pipeline.verify_release(release, self.database)


class InputContractTests(unittest.TestCase):
    # Valid JSON and schema form, with no reliance on an invalid-schema fallback.
    VALID = {'schema': 'core-yongshen-input-0.1', 'input_origin': 'constructed_test',
             'pillars': {'year': '甲子', 'month': '丙寅', 'day': '甲子', 'hour': '甲子'},
             'samples': []}

    def test_repeated_identical_schema_is_still_rejected(self):
        remaining = dict(self.VALID)
        schema = json.dumps(remaining.pop('schema')).encode()
        raw = (b'{"schema":' + schema + b',"schema":' + schema + b','
               + json.dumps(remaining, ensure_ascii=False).encode()[1:])
        self.assertEqual(json.loads(raw), self.VALID)
        with self.assertRaisesRegex(ValueError, 'duplicate_json_key: schema'):
            engine_adapter.load_json(raw)

    def test_valid_json_at_limit_is_accepted_and_one_byte_over_is_rejected(self):
        raw = json.dumps(self.VALID, ensure_ascii=False).encode()
        at_limit = raw + b' ' * (engine_adapter.MAX_INPUT_BYTES - len(raw))
        self.assertEqual(engine_adapter.load_json(at_limit), self.VALID)
        too_long = at_limit + b' '
        self.assertEqual(json.loads(too_long), self.VALID)
        with self.assertRaisesRegex(ValueError, 'request_exceeds_128_KiB'):
            engine_adapter.load_json(too_long)

    def test_nonfinite_numeric_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'nonfinite_json_number'):
            engine_adapter.load_json(b'{"fixture":NaN}')


if __name__ == '__main__':
    unittest.main()
