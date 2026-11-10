#!/usr/bin/env python3
"""Export rule/source metadata, never source text, binary data or private traces.

project_seed is the canonical allowlist projection used by the private-data
materializer. Pass the SHA-256 of the original seed bytes, not its JSON encoding.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

PUBLIC_SCHEMA = 'gx-public-rule-catalog-0.1'
PINNED_SEED_SHA256 = '34b6ce379dd78a5d482fa81bf267f6eb47e4095e090f2abf572c600d373e2594'
SECTIONS = ('sources', 'passages', 'rules', 'rule_passages', 'route_bindings')
EXPECTED_COUNTS = dict(zip(SECTIONS, (13, 43, 38, 45, 35)))

SOURCE_FIELDS = '''source_id title edition_label edition_note source_profile
content_sha256 content_bytes media_type deployment_relative_path archive_original_path
object_relative_path pdf_page_count pdf_page_numbering source_kind verification
book_pdf_sha256 source_pdf_page printed_page runtime_archive_member_path
runtime_archive_sha256 content_identity_policy github_commit source_url'''.split()

PASSAGE_FIELDS = '''passage_id source_id source_kind section pdf_pages printed_pages
page_location quote_sha256 quote_unpunctuated_sha256 transcription_note
verification_status rechecked_on support_scope does_not_support claim_role
section_name_verification context_policy applied_rule_claim source_layer_attribution
line_start line_end rendered_page_sha256'''.split()

RULE_FIELDS = '''rule_id original_rule_id method_id component module rule_kind label
source_locator source_file source_file_sha256 review_source_file_sha256
support_scope does_not_support status claim_role applied_rule_claim application_note
method_version source_profile source_profile_note github_commit reference_kind
original_reference_status original_reference_field_names original_contract_rule_id
original_contract_status source_sha256'''.split()

# These are existing rule declarations. The original source text and historical
# review prose are deliberately not copied from original_rule_record.
CONDITION_FIELDS = '''id name title rule proposition named_question preconditions
conditions exclusions exclusions_and_failure entry goal repair limit boundary
algorithm algorithm_and_explanation rule_and_algorithm rule_and_algorithm_explanation
rule_expression expression judgment_method method method_role output_meaning
change_conditions change_rule changes time_or_input_change conflict
conflict_and_change_policy conflict_policy conflict_priority conflict_resolution
question_scope remaining_original_scope doctrinal_scope tradition_scope
4_conditions_and_grounds 5_exclusions 6_triggers 7_conflicts_and_reason
8_formula_or_algorithm 3_method_role source_prediction_validated'''.split()

EDGE_FIELDS = '''rule_id passage_id support_kind claim_role scope_note does_not_support
relationship_status applied_rule_claim applied_rule_claim_requires'''.split()

BINDING_FIELDS = '''binding_id method_id native_method_exact component rule_ids
allowed_result_states version binding_policy claim_role applied_rule_claim
require_selected_method ambiguous_method_policy incomplete_result_policy
source_profile github_commit module usage_context required_result_predicates'''.split()

REFERENCE_FIELDS = '''path original_archive_or_runtime_path sha256 status original_id
archive native_pin native_pin_verified bytes runtime_snapshot_sha256 github_commit'''.split()

INVENTORY_FIELDS = '''module label method current_method component source_readiness
source_basis_status verified_passage_ids known_limits case_ids coverage_note
rule_coverage_note original_algorithms_reused'''.split()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def select(value, fields):
    return {name: copy.deepcopy(value[name]) for name in fields if name in value}


def is_absolute(value):
    return isinstance(value, str) and (value.startswith(('/', '~/', 'file://')) or bool(re.match(r'^[A-Za-z]:[\\/]', value)))


def reference(value):
    """Keep a relative original identity; drop local extraction/cache locations."""
    result = select(value, REFERENCE_FIELDS)
    if is_absolute(result.get('path')):
        alternative = result.get('original_archive_or_runtime_path')
        if alternative and not is_absolute(alternative):
            result['path'] = alternative
        else:
            result.pop('path')
    for name in list(result):
        if is_absolute(result[name]):
            result.pop(name)
    return result


def project_seed(private_seed, deployment_seed_sha256=PINNED_SEED_SHA256):
    """Pure deterministic projection. Unknown keys are not exported."""
    if not re.fullmatch(r'[0-9a-f]{64}', deployment_seed_sha256):
        raise ValueError('invalid deployment seed SHA-256')
    sources = [select(row, SOURCE_FIELDS) for row in private_seed['sources']]
    passages = [select(row, PASSAGE_FIELDS) for row in private_seed['passages']]
    for row in passages:
        row['text_included'] = False
    rules = []
    for row in private_seed['rules']:
        projected = select(row, RULE_FIELDS)
        for key in ('adoption', 'runtime_reference', 'mandatory_source_correction', 'effective_rule_cards'):
            if isinstance(row.get(key), dict):
                projected[key] = reference(row[key])
        raw_rule = row.get('original_rule_record')
        if raw_rule is not None:
            projected['original_rule_record_canonical_sha256'] = sha_bytes(canonical(raw_rule))
        if isinstance(raw_rule, dict):
            projected['original_rule_conditions'] = select(raw_rule, CONDITION_FIELDS)
        rules.append(projected)
    output = {
        'schema_version': PUBLIC_SCHEMA,
        'deployment_seed_sha256': deployment_seed_sha256,
        'seed_id': private_seed['seed_id'],
        'created_on': private_seed['created_on'],
        'github_commit': private_seed['github_commit'],
        'sources': sources,
        'passages': passages,
        'rules': rules,
        'rule_passages': [select(row, EDGE_FIELDS) for row in private_seed['rule_passages']],
        'route_bindings': [select(row, BINDING_FIELDS) for row in private_seed['route_bindings']],
        'publication_policy': {
            'source_text_included': False,
            'raw_data_or_runtime_archives_included': False,
            'source_metadata_is_not_source_bytes_verification': True,
            'rule_conditions_are_existing_declarations_not_new_algorithms': True,
            'native_result_is_authoritative': True,
            'retrieval_alone_establishes_rule_support': False,
            'unresolved_diagnostics_override_public_result': False,
            'original_rule_status_is_not_current_adoption_status': True,
            'quote_materialization_requires_private_seed_sha256': deployment_seed_sha256,
        },
    }
    validate_projection(output)
    return output


def walk(value, path=''):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + '/' + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + '/' + str(index))
    else:
        yield path, value


def validate_projection(document):
    """Fail closed for unsafe exported paths and broken semantic links."""
    for path, value in walk(document):
        if not isinstance(value, str):
            continue
        if re.search(r'/(?:Users|home|root|var/lib|var/tmp|opt)/|file://|(?:ssh|sftp)\s+\S+@', value):
            raise ValueError('private machine location at ' + path)
        if re.search(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', value):
            raise ValueError('IPv4 literal at ' + path)
        if path.rsplit('/', 1)[-1] in ('local_path', 'quote', 'quote_unpunctuated', 'native_result', 'trace'):
            raise ValueError('private payload field at ' + path)
    for section, count in EXPECTED_COUNTS.items():
        if len(document[section]) != count:
            raise ValueError('unexpected ' + section + ' inventory')
    sources = {r['source_id'] for r in document['sources']}
    passages = {r['passage_id'] for r in document['passages']}
    rules = {r['rule_id'] for r in document['rules']}
    if len(sources) != len(document['sources']) or len(passages) != len(document['passages']) or len(rules) != len(document['rules']):
        raise ValueError('duplicate identity')
    for row in document['passages']:
        if row['source_id'] not in sources or not re.fullmatch('[0-9a-f]{64}', row['quote_sha256']):
            raise ValueError('missing passage source or quote hash')
    for row in document['rule_passages']:
        if row['rule_id'] not in rules or row['passage_id'] not in passages:
            raise ValueError('broken rule-to-source link')
    for row in document['route_bindings']:
        if not row['native_method_exact'] or not row['rule_ids'] or not set(row['rule_ids']) <= rules:
            raise ValueError('broken exact method binding')
        for predicate in row.get('required_result_predicates', []):
            if set(predicate) != {'path', 'operator', 'value'} or predicate['operator'] not in ('equals', 'startswith'):
                raise ValueError('unsupported native condition predicate')
    return True


def project_inventory(rows):
    projected = []
    for row in rows:
        item = select(row, INVENTORY_FIELDS)
        # Intermediate test notes are superseded by separately published final
        # verification summaries, not silently treated as current acceptance.
        item.pop('coverage_note', None)
        for key in ('adoption', 'supplemental_natal_adoption', 'current_handoff_scope_document', 'repository_reading_copy_adoption'):
            if isinstance(row.get(key), dict):
                item[key] = reference(row[key])
        projected.append(item)
    if len(projected) != 28 or len({r['module'] for r in projected}) != 28:
        raise ValueError('expected 28 distinct additional modules')
    for path, value in walk(projected):
        if isinstance(value, str) and re.search(r'/(?:Users|home|root|var/lib|var/tmp|opt)/|file://', value):
            raise ValueError('private path in module inventory: ' + path)
    return projected


def project_cases(rows):
    result = []
    for row in rows:
        item = select(row, ('case_id', 'module', 'kind', 'request', 'expected'))
        if item['request'].get('input_origin') != 'constructed_test':
            raise ValueError('only constructed test requests are publishable')
        allowed = {'schema', 'input_origin', 'pillars', 'samples'}
        if not set(item['request']) <= allowed:
            raise ValueError('unexpected request fields require review')
        result.append(item)
    return result


def markdown(document):
    source_by_id = {r['source_id']: r for r in document['sources']}
    passage_by_id = {r['passage_id']: r for r in document['passages']}
    def cell(value):
        return str(value).replace('|', '&#124;').replace('\n', '<br>')
    lines = [
        '# 规则与来源对应表', '',
        '公开内容只有规则声明、精确方法绑定、出处定位和哈希。原书正文、扫描件、完整研究档案和原生运行 trace 不随仓库发布。', '',
        '这 38 个条目是现有方法的规则／具名来源字段，不是一套新写的命理算法。数值计算与最终裁决继续调用锁定版本的原生算核。', '',
        '规则条件中如果出现工程比例，它属于原方法的工程定义，不应写成古籍规定。未决、域外、候选方法和来源诊断保持各自原状态。', '',
        '## 文件合同', '',
        '- `rule_catalog.json`：13 个来源、43 个片段定位、38 个规则／来源字段、45 个关联、35 个方法绑定。',
        '- `method_inventory.json`：28 个新增模块的实际采用记录定位与 SHA；不把历史规则卡内的旧状态冒充当前采用状态。',
        '- `constructed_cases.json`：69 份构造请求，只含输入和测试条件，不含私人信息或完整响应。',
        '- 需要原文时，在受控环境校验私有种子的固定 SHA，再验证 `project_seed` 的全部投影一致；检索命中本身不能建立规则支持。', '',
        '## 精确接入规则', '',
        '| 绑定 | 原生方法 | 可用状态 | 原生条件 | 规则 |',
        '|---|---|---|---|---|',
    ]
    for b in document['route_bindings']:
        conditions = '; '.join(p['path'] + ' ' + p['operator'] + ' ' + str(p['value']) for p in b.get('required_result_predicates', [])) or '依精确方法与状态匹配'
        lines.append('| ' + ' | '.join(cell(x) for x in (b['binding_id'], b['native_method_exact'], ', '.join(b['allowed_result_states']), conditions, '<br>'.join(b['rule_ids']))) + ' |')
    lines.extend(['', '## 规则到出处', '', '| 既有规则 ID | 内容／适用范围 | 片段与出处 |', '|---|---|---|'])
    for rule in document['rules']:
        locators = []
        for edge in document['rule_passages']:
            if edge['rule_id'] != rule['rule_id']:
                continue
            passage = passage_by_id[edge['passage_id']]
            source = source_by_id[passage['source_id']]
            location = ','.join(map(str, passage.get('pdf_pages', []))) or ('line ' + str(passage.get('line_start', '?')) + '-' + str(passage.get('line_end', '?')))
            layer = passage.get('source_layer_attribution') or source['source_kind']
            locators.append(passage['passage_id'] + ' / ' + source['title'] + ' / ' + location + ' / ' + str(layer))
        lines.append('| ' + ' | '.join(cell(x) for x in (rule['rule_id'], rule['label'] + '<br>' + rule.get('support_scope', ''), '<br>'.join(locators))) + ' |')
    lines.extend(['', '## 复核边界', '',
                  '引用定位和文件哈希保留来源身份，但仓库内没有这些源文件。固定页图的 `content_sha256` 是页图身份；`book_pdf_sha256` 若只来自已采用的来源卡，其含义仍受 `verification` 与 `content_identity_policy` 限制。', '',
                  '所有引用默认说明所选方法的传统依据；具体喜用、完整方案的组内共同需要／组间可替代、主次顺序和时点变化，以原生程序输出为准。', ''])
    return '\n'.join(lines)


def write_json(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=Path, required=True)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New directory; existing outputs are never overwritten')
    args = parser.parse_args()
    raw = args.seed.read_bytes()
    digest = sha_bytes(raw)
    if digest != PINNED_SEED_SHA256:
        raise ValueError('private evidence seed does not match the accepted deployment')
    private_seed = json.loads(raw)
    public = project_seed(private_seed, digest)
    inventory = project_inventory(json.loads(args.inventory.read_text()))
    cases = project_cases(json.loads(args.cases.read_text()))
    encoded = json.dumps(public, ensure_ascii=False)
    for passage in private_seed['passages']:
        for key in ('quote', 'quote_unpunctuated'):
            text = passage.get(key, '')
            if len(text) >= 16 and text in encoded:
                raise ValueError('a complete source quotation escaped the allowlist: ' + passage['passage_id'])
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / 'rule_catalog.json', public)
    write_json(args.output / 'method_inventory.json', inventory)
    write_json(args.output / 'constructed_cases.json', cases)
    (args.output / 'RULES.md').write_text(markdown(public), encoding='utf-8')
    print(json.dumps({'schema_version': PUBLIC_SCHEMA, 'counts': EXPECTED_COUNTS,
                      'modules': len(inventory), 'constructed_cases': len(cases),
                      'catalog_sha256': sha_bytes((args.output / 'rule_catalog.json').read_bytes()),
                      'source_text_included': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
