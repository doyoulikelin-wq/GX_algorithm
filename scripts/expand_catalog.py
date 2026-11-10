"""Expand only curated evidence; reuse all already-built archive/registry rows."""
from pathlib import Path
import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import tempfile


def sha_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--base-db', required=True, type=Path)
    p.add_argument('--base-sha256', required=True)
    p.add_argument('--seed', required=True, type=Path)
    p.add_argument('--app', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    if args.output.exists() or sha_file(args.base_db) != args.base_sha256:
        raise ValueError('existing output or base index checksum mismatch')
    sys.path.insert(0, str(args.app))
    import catalog_builder as builder
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.evidence-expansion-', suffix='.sqlite', dir=args.output.parent)
    os.close(fd)
    src = sqlite3.connect(args.base_db.resolve().as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(temporary)
    try:
        src.backup(dst)
        src.close()
        old = {k: json.loads(v) for k, v in dst.execute('SELECT key,value FROM meta')}
        dst.execute('PRAGMA foreign_keys=ON')
        dst.execute('BEGIN')
        for name in ['route_binding_rules', 'rule_passages', 'route_bindings', 'rules', 'passages', 'sources']:
            dst.execute('DELETE FROM ' + name)
        dst.execute('DROP TABLE IF EXISTS passages_fts')
        builder.import_evidence(dst, args.seed)
        builder.build_fts(dst)
        counts = dict(old['counts'])
        for name in ['sources', 'passages', 'rules', 'rule_passages', 'route_bindings']:
            counts[name] = dst.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
        for name in ['assets', 'asset_paths', 'registrations']:
            if dst.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0] != old['counts'][name]:
                raise ValueError('existing catalogue changed unexpectedly')
        builder.set_meta(dst, 'counts', counts)
        builder.set_meta(dst, 'built_at', datetime.datetime.now(datetime.timezone.utc).isoformat())
        builder.set_meta(dst, 'evidence_expansion', {'base_catalog_sha256': args.base_sha256,
            'base_built_at': old['built_at'], 'assets_and_registrations_reused': True,
            'archive_objects_reparsed': False, 'calculation_algorithms_reimplemented': False})
        if dst.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('broken evidence links')
        dst.commit()
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('index integrity failed')
        dst.close()
        os.link(temporary, args.output)
        os.unlink(temporary)
        args.output.chmod(0o444)
        if sha_file(args.base_db) != args.base_sha256:
            raise ValueError('base index changed during expansion')
        print(json.dumps({'output': str(args.output), 'sha256': sha_file(args.output),
                          'bytes': args.output.stat().st_size, 'counts': counts,
                          'existing_metadata_reused': True}, ensure_ascii=False, indent=2))
    except BaseException:
        dst.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


if __name__ == '__main__':
    main()
