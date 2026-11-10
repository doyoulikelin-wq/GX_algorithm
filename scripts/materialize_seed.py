#!/usr/bin/env python3
"""Match public rules to an authorized local evidence seed; never fetch data."""
from pathlib import Path
import argparse
import hashlib
import json
import os

from export_rules import project_seed


def materialize(catalog_path, private_path, output):
    catalog = json.loads(Path(catalog_path).read_text())
    payload = Path(private_path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != catalog.get('deployment_seed_sha256'):
        raise ValueError('private_seed_identity_mismatch')
    seed = json.loads(payload)
    if project_seed(seed, digest) != catalog:
        raise ValueError('public_rules_do_not_match_private_seed')
    for passage in seed['passages']:
        if hashlib.sha256(passage['quote'].encode('utf-8')).hexdigest() != passage['quote_sha256']:
            raise ValueError('private_quote_identity_mismatch')
    output = Path(output)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(payload)
    return {'status': 'ready', 'seed_sha256': digest, 'passages': len(seed['passages']),
            'rules': len(seed['rules']), 'source_files_verified': False,
            'note': 'Seed identity verified. Source files remain external and are verified by the release runtime.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=Path(__file__).resolve().parents[1] / 'rules/rule_catalog.json')
    parser.add_argument('--private-seed', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = materialize(args.catalog, args.private_seed, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
