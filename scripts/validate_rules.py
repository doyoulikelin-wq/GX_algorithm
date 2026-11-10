#!/usr/bin/env python3
"""Validate public rule identities and their explicit evidence relationships."""
from pathlib import Path
import argparse
import json
import re

HASH = re.compile(r'[a-f0-9]{64}')


def validate(catalog):
    if catalog.get('schema_version') != 'gx-public-rule-catalog-0.1':
        raise ValueError('unsupported public catalogue')
    if not HASH.fullmatch(catalog.get('deployment_seed_sha256', '')):
        raise ValueError('missing private seed identity')
    tables = {}
    for table, key in [('sources', 'source_id'), ('passages', 'passage_id'),
                       ('rules', 'rule_id'), ('route_bindings', 'binding_id')]:
        rows = catalog[table]
        mapped = {row[key]: row for row in rows}
        if len(mapped) != len(rows) or not all(isinstance(k, str) and k for k in mapped):
            raise ValueError('duplicate or missing identity: ' + table)
        tables[table] = mapped
    for source in tables['sources'].values():
        if not HASH.fullmatch(source.get('content_sha256', '')):
            raise ValueError('missing source identity')
        if source['source_kind'] not in ('classical_primary_body', 'classical_commentary', 'engineering_implementation'):
            raise ValueError('unrecognized evidence layer')
    for passage in tables['passages'].values():
        if passage['source_id'] not in tables['sources'] or not HASH.fullmatch(passage.get('quote_sha256', '')):
            raise ValueError('broken passage source or hash')
        if 'quote' in passage or 'quote_unpunctuated' in passage:
            raise ValueError('source text belongs in the private seed')
    linked_rules = set()
    edges = set()
    for edge in catalog['rule_passages']:
        identity = edge['rule_id'], edge['passage_id'], edge['support_kind']
        if identity in edges or identity[0] not in tables['rules'] or identity[1] not in tables['passages']:
            raise ValueError('duplicate or broken rule evidence edge')
        edges.add(identity); linked_rules.add(identity[0])
    bound_rules = set()
    for binding in tables['route_bindings'].values():
        if not isinstance(binding.get('native_method_exact'), str) or not binding['native_method_exact']:
            raise ValueError('exact native method required')
        if not binding.get('rule_ids') or not set(binding['rule_ids']) <= linked_rules:
            raise ValueError('method binding lacks explicit evidence')
        if not binding.get('allowed_result_states'):
            raise ValueError('explicit allowed result states required')
        for predicate in binding.get('required_result_predicates', []):
            if set(predicate) != {'path', 'operator', 'value'} or predicate['operator'] not in ('equals', 'startswith'):
                raise ValueError('unsupported native result predicate')
        bound_rules.update(binding['rule_ids'])
    if bound_rules != set(tables['rules']):
        raise ValueError('unbound rule')
    return {'status': 'passed', 'counts': {**{k: len(v) for k, v in tables.items()}, 'rule_passages': len(edges)},
            'source_text_included': False, 'native_algorithms_executed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalog', nargs='?', type=Path, default=Path(__file__).resolve().parents[1] / 'rules/rule_catalog.json')
    args = parser.parse_args()
    print(json.dumps(validate(json.loads(args.catalog.read_text())), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
