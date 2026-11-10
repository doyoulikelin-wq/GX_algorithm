"""Join native conclusions to explicitly reviewed rule/source links."""
from pathlib import Path
import argparse
import hashlib
import json
import sys

import catalog_queries as catalog
from engine_adapter import run_native, load_json, MAX_INPUT_BYTES

VERSION = 'bazi-evidence-chain-0.2'
CLASSICAL_KINDS = {'classical_primary_body', 'classical_commentary'}


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify_release(release, database):
    lock = json.loads((release / 'release-lock.json').read_text())
    for relative, expected in lock['files'].items():
        path = release / relative
        if path.is_symlink() or not path.resolve().is_relative_to(release.resolve()):
            raise ValueError('release_path_escape')
        if sha_file(path) != expected:
            raise ValueError('release_integrity_failure: ' + relative)
    if sha_file(database) != lock['catalog_sha256']:
        raise ValueError('catalog_integrity_failure')
    metadata = catalog.metadata(database)
    if metadata['seed_sha256'] != lock['seed_sha256']:
        raise ValueError('catalog_seed_identity_mismatch')
    return lock, metadata


def lookup_path(value, path):
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def predicate_matches(row, predicate):
    actual = lookup_path(row, predicate.get('path', ''))
    if predicate.get('operator') == 'startswith':
        return isinstance(actual, str) and isinstance(predicate.get('value'), str) and actual.startswith(predicate['value'])
    if predicate.get('operator') == 'equals':
        return actual == predicate.get('value')
    return False


def result_text(row):
    status = row.get('status')
    if status == 'no_additional_need':
        return '在所选方法和本次条件下，已证无需额外辅助。'
    if status != 'determined':
        return '本次没有确定喜用。' + (row.get('reason') or '')
    groups = (row.get('auxiliary') or {}).get('alternatives', [])
    texts = []
    for group in groups:
        primary, secondary = group.get('primary'), group.get('secondary')
        if primary and secondary:
            texts.append(str(primary) + '为主、' + str(secondary) + '为次')
        elif primary:
            texts.append(str(primary) + '为主要辅助')
        else:
            texts.append('、'.join(group.get('elements', [])) + ('共同需要' if len(group.get('elements', [])) > 1 else '为辅助'))
    return '在所选方法和本次条件下，' + '；或采用另一完整方案：'.join(texts) + '。'


def enrich_row(database, method, row):
    # Native auxiliary, priority and change states are copied, never inferred.
    result = {key: row[key] for key in ('at', 'status', 'auxiliary', 'reason', 'comparison_to_previous', 'supplied_time_pillars') if key in row}
    result.update(method=method, classical_citations=[], diagnostic_citations=[], engineering_basis=[], rule_links=[])
    known = row.get('status') in ('determined', 'no_additional_need')
    bindings = catalog.bindings_for_method(database, method) if isinstance(method, str) and method else []
    if not known and not any(b.get('usage_context') == 'source_diagnostic' for b in bindings):
        result['evidence_status'] = 'not_attached_to_unresolved_result'
        result['explanation'] = result_text(row)
        return result
    seen = set()
    for binding in bindings:
        if binding.get('native_method_exact') != method:
            continue
        if row['status'] not in binding.get('allowed_result_states', []):
            continue
        diagnostic = binding.get('usage_context') == 'source_diagnostic'
        if not known and not diagnostic:
            continue
        if not all(predicate_matches(row, p) for p in binding.get('required_result_predicates', [])):
            continue
        for rule_id in binding.get('rule_ids', []):
            evidence = catalog.evidence_for_rule(database, rule_id)
            if not evidence:
                raise ValueError('missing_explicit_rule_evidence: ' + rule_id)
            for edge in evidence['evidence']:
                link, passage = edge['link'], edge['passage']
                predicate = link.get('applied_rule_claim_requires')
                if predicate and not predicate_matches(row, predicate):
                    continue
                original, source = passage['original_record'], passage['source']
                role = link.get('claim_role', link.get('support_kind'))
                if role not in ('method_basis', 'engineering_basis'):
                    continue
                if source.get('source_kind') in CLASSICAL_KINDS and role == 'method_basis':
                    target = 'diagnostic_citations' if diagnostic else 'classical_citations'
                elif source.get('source_kind') == 'engineering_implementation' and role == 'engineering_basis':
                    target = 'engineering_basis'
                else:
                    raise ValueError('unsupported_evidence_layer: ' + str(source.get('source_kind')))
                item = {
                    'passage_id': passage['passage_id'], 'rule_id': rule_id,
                    'binding_id': binding['binding_id'], 'claim_role': role,
                    'applied_rule_claim': bool(predicate),
                    'quote': passage['text'], 'quote_sha256': passage['text_sha256'],
                    'title': source['title'], 'edition': source.get('edition_label'),
                    'source_id': passage['source_id'], 'source_sha256': source['content_sha256'],
                    'source_kind': source['source_kind'], 'source_locator': {
                        key: source[key] for key in (
                            'archive_original_path', 'runtime_archive_member_path',
                            'object_relative_path', 'source_url', 'deployment_relative_path',
                            'parent_pdf_sha256', 'parent_pdf_source', 'book_pdf_sha256',
                            'source_pdf_page', 'printed_page', 'content_identity_policy',
                            'parent_pdf_identity_note', 'verification') if key in source},
                    'section': original.get('section'), 'pdf_pages': original.get('pdf_pages', []),
                    'printed_pages': original.get('printed_pages', []), 'page_location': original.get('page_location'),
                    'transcription_note': original.get('transcription_note'),
                    'source_layer_attribution': original.get('source_layer_attribution'),
                    'verification_status': original.get('verification_status'),
                    'support_scope': original.get('support_scope'),
                    'does_not_support': original.get('does_not_support', [])}
                item['usage_context'] = 'source_diagnostic' if diagnostic else 'selected_method'
                item['attached_to_confirmed_result'] = known and not diagnostic
                if source.get('media_type', '').startswith('image/'):
                    item['source_medium'] = 'pinned_page_image'
                identity = (target, passage['passage_id'], rule_id)
                if identity not in seen:
                    result[target].append(item)
                    result['rule_links'].append({'rule_id': rule_id, 'binding_id': binding['binding_id'],
                                                'claim_role': role, 'rule': evidence['rule']})
                    seen.add(identity)
    result['evidence_status'] = ('verified_method_basis_attached' if result['classical_citations'] else
                                'diagnostic_basis_attached_only' if result['diagnostic_citations'] else
                                'not_attached_to_unresolved_result' if not known else 'evidence_not_indexed')
    result['explanation'] = result_text(row)
    if result['classical_citations']:
        result['explanation'] += '所附原文说明本次所选方法的传统依据；具体辅助组合和主次来自本次原生计算。'
    elif result['diagnostic_citations']:
        result['explanation'] += '所附原文或注解仅说明具名来源诊断，不代表已确定整盘喜用。'
    elif known:
        result['explanation'] += '该方法的原文证据尚未接入本索引。'
    return result


def effective_method(native, row, *, current=False):
    """Read only explicit native dispatch identities; never infer a winner."""
    trace = row.get('trace') or {}
    if current and native.get('current_method') == trace.get('method') and trace.get('method'):
        return trace['method']
    extension = trace.get('supplementary_current_tasks') or {}
    selected = extension.get('result') or {}
    if trace.get('source_authorized_extension') and extension.get('method') and all(
            row.get(k) == selected.get(k) for k in ('status', 'auxiliary', 'reason')):
        return extension['method']
    return native.get('method')


def evidence_contexts(database, native):
    """Keep supplementary sources and adjudication candidates out of the result."""
    contexts = []

    def contextualize(row, origin):
        row['used_for_public_auxiliary'] = False
        for group in ('classical_citations', 'diagnostic_citations', 'engineering_basis'):
            for citation in row[group]:
                citation['usage_context'] = origin
                citation['attached_to_confirmed_result'] = False
                citation['used_for_public_auxiliary'] = False
        if row['classical_citations']:
            row['evidence_status'] = 'verified_candidate_basis_only'
        row['explanation'] += '此处保留独立方法依据，不覆盖公共裁决。'
        return row
    for key, report in (native.get('source_diagnostics') or {}).items():
        if not isinstance(report, dict) or not isinstance(report.get('natal'), dict):
            continue
        method = report.get('method', key)
        contexts.append({'method': method, 'origin': 'native.source_diagnostics',
                         'used_for_public_auxiliary': False, 'scope': report.get('scope'),
                         'natal': contextualize(enrich_row(database, effective_method(report, report['natal']) or method, report['natal']), 'source_diagnostic'),
                         'timeline': [contextualize(enrich_row(database, effective_method(report, row, current=True) or method, row), 'source_diagnostic') for row in report.get('timeline', [])]})
    candidates = (native.get('adjudication') or {}).get('evaluated_candidates') or {}
    natal_views = (native.get('natal', {}).get('trace') or {}).get('views') or {}
    for key, identity in candidates.items():
        method = identity.get('method')
        if key not in natal_views:
            continue
        rows = []
        for row in native.get('timeline', []):
            child = (row.get('trace') or {}).get('views', {}).get(key)
            if isinstance(child, dict):
                rows.append(contextualize(enrich_row(database, effective_method(identity, child, current=True) or method,
                                                     {'at': row.get('at'), **child}), 'candidate_method'))
        contexts.append({'method': method, 'origin': 'native.adjudication.evaluated_candidates',
                         'used_for_public_auxiliary': False, 'scope': identity.get('scope'),
                         'natal': contextualize(enrich_row(database, method, natal_views[key]), 'candidate_method'), 'timeline': rows})
    return contexts


def analyze(request, release, database, lock, metadata):
    if not isinstance(request, dict) or not isinstance(request.get('samples', []), list) or len(request.get('samples', [])) > 32:
        raise ValueError('request must be an object with at most 32 samples')
    engine = run_native(request, release / 'runtime', timeout=90)
    base = {'schema': VERSION, 'engine_status': engine['status'], 'native_result': engine.get('native'),
            'provenance': {**engine.get('provenance', {}), 'catalog_sha256': lock['catalog_sha256'],
                           'seed_sha256': lock['seed_sha256'], 'index_built_at': metadata['built_at']},
            'boundaries': {'classical_citations_are_method_basis': True,
                           'classical_text_proves_engineering_coefficients': False,
                           'location_used_for_traditional_auxiliary': False,
                           'real_prediction_validated': False,
                           'calendar_authenticated': False,
                           'interval_inferred_between_samples': False}}
    if engine['status'] != 'computed':
        base.update(status=engine['status'], reason=engine.get('reason') or (engine.get('native') or {}).get('reason'), natal=None, timeline=[])
        return base
    native = engine['native']
    base['natal'] = enrich_row(database, effective_method(native, native['natal']), native['natal'])
    base['timeline'] = [enrich_row(database, effective_method(native, row, current=True), row) for row in native['timeline']]
    base['evidence_contexts'] = evidence_contexts(database, native)
    base['status'] = native['status']
    base['input_origin'] = native.get('input_origin')
    rows = [base['natal'], *base['timeline']]
    base['chain_summary'] = {
        'computed_points': len(base['timeline']),
        'determined_points': sum(x.get('status') in ('determined', 'no_additional_need') for x in base['timeline']),
        'points_with_verified_method_citations': sum(x['evidence_status'] == 'verified_method_basis_attached' for x in base['timeline']),
        'native_result_preserved': True,
        'all_returned_conclusions_have_method_citations': all(x['evidence_status'] == 'verified_method_basis_attached' for x in rows if x.get('status') in ('determined', 'no_additional_need')) and any(x.get('status') in ('determined', 'no_additional_need') for x in rows)}
    return base


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True, type=Path)
    parser.add_argument('--db', required=True, type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('analyze').add_argument('request')
    sub.add_parser('search').add_argument('query')
    sub.add_parser('passage').add_argument('passage_id')
    sub.add_parser('stats')
    args = parser.parse_args()
    try:
        lock, metadata = verify_release(args.release, args.db)
        if args.action == 'analyze':
            if args.request == '-':
                raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            else:
                with open(args.request, 'rb') as stream:
                    raw = stream.read(MAX_INPUT_BYTES + 1)
            try:
                request = load_json(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                result = {'status': 'invalid_input', 'reason': str(exc)}
            else:
                result = analyze(request, args.release, args.db, lock, metadata)
        elif args.action == 'search':
            result = catalog.search_passages(args.db, args.query)
        elif args.action == 'passage':
            result = catalog.get_passage(args.db, args.passage_id) or {'status': 'not_found'}
        else:
            result = {'status': 'ready', 'schema': VERSION, 'metadata': metadata, 'catalog_sha256': lock['catalog_sha256']}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {'status': 'integrity_or_runtime_error', 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 2 if result.get('status') == 'invalid_input' else 3 if result.get('status') in ('runtime_error', 'integrity_or_runtime_error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
