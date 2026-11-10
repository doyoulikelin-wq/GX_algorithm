#!/usr/bin/env python3
"""Fail closed on private material accidentally staged for this public project."""
from pathlib import Path
import argparse
import ipaddress
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOT = {'README.md', 'NOTICE.md', '.gitignore', '.gitattributes'}
ALLOWED_DIRS = {'src', 'scripts', 'rules', 'docs', 'tests', 'examples', 'verification'}
ALLOWED_SUFFIXES = {'.py', '.md', '.json'}
FORBIDDEN_JSON_KEYS = {'quote', 'quote_unpunctuated', 'native_result', 'raw_json', 'local_path',
                       'rendered_page', 'request_id', 'access_token', 'password', 'private_key'}
PRIVATE_PATH = re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/')
IPV4 = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
SECRET = re.compile(r'(?im)^\s*(?:password|api_key|access_token|private_key)\s*[:=]\s*[\x22\x27]?[^\s\x22\x27]{8,}')


def inspect_json(value, errors, location):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_JSON_KEYS:
                errors.append(location + ': forbidden data field ' + key)
            inspect_json(child, errors, location + '.' + key)
    elif isinstance(value, list):
        for number, child in enumerate(value):
            inspect_json(child, errors, location + '[' + str(number) + ']')


def check(root, staged_only=False):
    command = ['git', '-C', str(root), 'ls-files', '--cached', '-z']
    if not staged_only:
        command += ['--others', '--exclude-standard']
    names = sorted(set(subprocess.check_output(command).decode().split('\0')) - {''})
    errors = []
    total = 0
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            errors.append(name + ': unsafe path'); continue
        path = root / relative
        if path.is_symlink() or not path.is_file():
            errors.append(name + ': not a regular file'); continue
        if not (name in ALLOWED_ROOT or (relative.parts[0] in ALLOWED_DIRS and relative.suffix in ALLOWED_SUFFIXES)):
            errors.append(name + ': file outside publication allowlist')
        if staged_only:
            entry = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-s', '--', name]).decode()
            if not entry.startswith(('100644 ', '100755 ')):
                errors.append(name + ': staged object is not a regular file'); continue
            raw = subprocess.check_output(['git', '-C', str(root), 'show', ':' + name])
        else:
            raw = path.read_bytes()
        total += len(raw)
        if len(raw) > 512 * 1024:
            errors.append(name + ': exceeds 512 KiB publication limit')
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            errors.append(name + ': binary content'); continue
        if '\0' in text:
            errors.append(name + ': NUL byte')
        if PRIVATE_PATH.search(text):
            errors.append(name + ': personal absolute path')
        if SECRET.search(text) or '-----BEGIN ' + 'PRIVATE KEY-----' in text:
            errors.append(name + ': possible credential')
        for value in IPV4.findall(text):
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if not address.is_loopback and not address.is_unspecified:
                errors.append(name + ': deployment IP address')
        if relative.suffix == '.json':
            try:
                inspect_json(json.loads(text), errors, name)
            except ValueError:
                errors.append(name + ': invalid JSON')
    if total > 2 * 1024 * 1024:
        errors.append('repository publication files exceed 2 MiB')
    return {'status': 'passed' if not errors else 'failed', 'files': len(names), 'bytes': total, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged-only', action='store_true')
    args = parser.parse_args()
    result = check(ROOT, args.staged_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
