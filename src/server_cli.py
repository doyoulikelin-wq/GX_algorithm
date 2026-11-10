#!/usr/bin/env python3
"""Audited administrator CLI. No public listener; isolated native execution."""
from pathlib import Path
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid

ROOT = Path('/opt/bazi-knowledge/current')
DATA = Path('/var/lib/bazi-knowledge')
IMAGE = 'public.ecr.aws/docker/library/python@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)


def audit(event):
    fd = os.open(DATA / 'audit.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n')
        f.flush()
        os.fsync(f.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    analyze = sub.add_parser('analyze')
    analyze.add_argument('request', help='JSON file path, or - for stdin')
    search = sub.add_parser('search')
    search.add_argument('query')
    passage = sub.add_parser('passage')
    passage.add_argument('passage_id')
    sub.add_parser('stats')
    args = parser.parse_args()
    DATA.mkdir(mode=0o700, exist_ok=True)
    (DATA / 'runs').mkdir(mode=0o700, exist_ok=True)
    request_id = uuid.uuid4().hex
    received = {'request_id': request_id, 'action': args.action, 'actor_uid': os.getuid(),
                'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    audit(dict(received, event='received'))
    payload = b''
    action_args = [args.action]
    try:
        if args.action == 'analyze':
            if args.request == '-':
                payload = sys.stdin.buffer.read(131073)
            else:
                with open(args.request, 'rb') as f:
                    payload = f.read(131073)
            if len(payload) > 131072:
                raise ValueError('input exceeds 128 KiB')
            action_args += ['-']
        elif args.action == 'search':
            action_args += [args.query]
        elif args.action == 'passage':
            action_args += [args.passage_id]
        release = ROOT.resolve(strict=True)
    except (OSError, ValueError) as exc:
        result = dict(received, status='invalid_input', reason=str(exc))
        audit(dict(result, event='rejected', received_prefix_sha256=digest(payload)))
        print(json.dumps(result, ensure_ascii=False))
        return 2
    # Resolve release and its matching catalogue as one versioned pair.
    # The fallback keeps rollback to the original v1 layout possible.
    catalog = (release / 'catalog.sqlite').resolve() if (release / 'catalog.sqlite').exists() else DATA / 'catalog.sqlite'
    name = 'bazi-knowledge-' + request_id
    command = ['docker', 'run', '--rm', '-i', '--name', name,
               '--network', 'none', '--ipc', 'none', '--read-only',
               '--user', '65534:65534', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--cpus', '1',
               '--memory', '1g', '--memory-swap', '1g', '--pids-limit', '64',
               '--tmpfs', '/tmp:rw,noexec,nosuid,size=96m',
               '--mount', 'type=bind,src=' + str(release) + ',dst=/release,readonly',
               '--mount', 'type=bind,src=' + str(catalog) + ',dst=/catalog.sqlite,readonly',
               IMAGE, 'python3', '-B', '/release/app/pipeline.py',
               '--release', '/release', '--db', '/catalog.sqlite', *action_args]
    started = time.monotonic()
    audit_payload = payload if args.action == 'analyze' else json.dumps(
        {'action': args.action, 'arguments': action_args[1:]}, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()
    write_private(DATA / 'runs' / (request_id + '.request.json'), audit_payload)
    event = {'request_id': request_id, 'action': args.action,
             'at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'request_sha256': digest(audit_payload), 'release': str(release),
             'release_lock_sha256': digest((release / 'release-lock.json').read_bytes()),
             'actor_uid': os.getuid(), 'isolation': 'offline_nonroot_readonly_container'}
    audit(dict(event, event='started'))
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=130)
        output = result.stdout
        code = result.returncode
        if result.stderr:
            write_private(DATA / 'runs' / (request_id + '.stderr.txt'), result.stderr)
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            parsed['request_id'] = request_id
            output = (json.dumps(parsed, ensure_ascii=False, indent=2) + '\n').encode()
        status = parsed.get('status') if isinstance(parsed, dict) else 'returned'
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        code = 3
        status = 'runtime_error'
        output = (json.dumps({'status': status, 'reason': str(exc), 'request_id': request_id}, ensure_ascii=False) + '\n').encode()
    write_private(DATA / 'runs' / (request_id + '.response.json'), output)
    audit(dict(event, event='finished', status=status, exit_code=code,
               duration_seconds=round(time.monotonic() - started, 3), response_sha256=digest(output)))
    sys.stdout.buffer.write(output)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
