"""Run the author's pinned v0.4.0 CLI without changing its rule semantics.

The caller must put execution inside its resource-limited, offline sandbox.
This module verifies the published installation and calls the native supplied-
pillars entry. It neither implements Bazi rules nor adds source quotations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import sys
import tempfile


ADAPTER_VERSION = "bazi-engine-adapter-0.1"
CORE_VERSION = "core-yongshen-0.4.0"
INSTALLATION_SHA256 = "4f526c5e0e0f82b2db80c172196436bc1a010f5540dd7247763cfcfd2f6fd482"
RELEASE_SHA256 = "5fc94c2b1560ae5e9a357e278b33d482cd7b004f061227168bb34933f473b005"
REPOSITORY_COMMIT = "575cfb3eeef690e52b5f0f7f383d429ef08db689"
MAX_INPUT_BYTES = 131_072
MAX_OUTPUT_BYTES = 32 * 1024 * 1024


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_json_key: " + key)
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("nonfinite_json_number: " + value)


def load_json(raw: bytes):
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("request_exceeds_128_KiB")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=_nonfinite)


def verify_installation(runtime_dir: Path) -> dict:
    """Verify all 76 published program, adoption-policy and calendar hashes.

    Per-component source/rule locks remain checked by the original runtime.
    The deployment layer additionally verifies the entire release archive.
    """
    root = Path(runtime_dir).resolve(strict=True)
    manifest_path = root / "installation.json"
    if manifest_path.is_symlink():
        raise ValueError("installation_manifest_must_not_be_symlink")
    raw = manifest_path.read_bytes()
    if _sha(raw) != INSTALLATION_SHA256:
        raise ValueError("installation_manifest_sha256_mismatch")
    manifest = json.loads(raw)
    if manifest.get("core_version") != CORE_VERSION:
        raise ValueError("unexpected_core_version")
    pins = manifest["program_and_calendar_sha256"]
    if len(pins) != 76:
        raise ValueError("unexpected_program_inventory")
    for relative, expected in pins.items():
        name = PurePosixPath(relative)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("unsafe_program_inventory_path")
        candidate = root.joinpath(*name.parts)
        if candidate.is_symlink() or not candidate.resolve(strict=True).is_relative_to(root):
            raise ValueError("program_path_outside_installation")
        if not candidate.is_file() or _sha(candidate.read_bytes()) != expected:
            raise ValueError("program_sha256_mismatch: " + relative)
    if manifest.get("entry") != "core-yongshen/core.py":
        raise ValueError("unexpected_native_entry")
    return manifest


def _error(status: str, reason: str, provenance: dict) -> dict:
    return {"schema": "bazi-engine-adapter-result-0.1", "status": status,
            "native": None, "reason": reason, "provenance": provenance}


def run_native(request: dict, runtime_dir: Path, timeout: float = 90) -> dict:
    """Return the complete native JSON plus byte hashes; never flatten results.

    `computed` means the original program ran, including an undetermined or
    outside-domain result. Inspect native.natal and every native.timeline row
    before presenting any confirmed auxiliary direction.
    """
    provenance = {"adapter_version": ADAPTER_VERSION,
                  "expected_core_version": CORE_VERSION,
                  "installation_sha256": INSTALLATION_SHA256,
                  "expected_release_archive_sha256": RELEASE_SHA256,
                  "source_repository": "ysun5-byte/bazi-core-handoff",
                  "source_commit": REPOSITORY_COMMIT,
                  "input_hash_encoding": "UTF-8 canonical JSON, no terminal newline",
                  "native_hash_encoding": "exact native stdout bytes"}
    try:
        if not isinstance(request, dict) or request.get("schema") != "core-yongshen-input-0.1":
            raise ValueError("adapter_requires_core_yongshen_input_0_1")
        payload = json.dumps(request, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(payload) > MAX_INPUT_BYTES:
            raise ValueError("request_exceeds_128_KiB")
        provenance["request_sha256"] = _sha(payload)
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        return _error("invalid_input", str(exc), provenance)
    try:
        if timeout <= 0 or timeout > 300:
            raise ValueError("timeout_must_be_between_0_and_300_seconds")
        root = Path(runtime_dir).resolve(strict=True)
        verify_installation(root)
        provenance["program_hashes_verified"] = 76
        command = [sys.executable, "-B", str(root / "core-yongshen/core.py"),
                   "--stdio", "--runtime", str(root / "core-yongshen/runtime")]
        environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ")
                       if key in os.environ}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout,
                                       stderr=stderr, env=environment,
                                       cwd=root, start_new_session=True)
            try:
                process.communicate(payload + b"\n", timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return _error("runtime_error", "native_execution_timeout", provenance)
            provenance["native_exit_code"] = process.returncode
            output_size = stdout.tell()
            if output_size > MAX_OUTPUT_BYTES:
                return _error("runtime_error", "native_output_exceeds_32_MiB", provenance)
            stdout.seek(0)
            raw = stdout.read()
            provenance["native_response_sha256"] = _sha(raw)
            stderr.seek(0)
            error_hash = hashlib.sha256()
            error_size = 0
            for chunk in iter(lambda: stderr.read(64 * 1024), b""):
                error_hash.update(chunk)
                error_size += len(chunk)
            if error_size:
                provenance["native_stderr_sha256"] = error_hash.hexdigest()
                provenance["native_stderr_bytes"] = error_size
            if process.returncode != 0:
                return _error("runtime_error", "native_process_failed", provenance)
            native = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                                parse_constant=_nonfinite)
        if not isinstance(native, dict) or native.get("version") != CORE_VERSION:
            raise ValueError("unexpected_native_response_version")
        provenance["core_version"] = native["version"]
        status = native.get("status")
        return {"schema": "bazi-engine-adapter-result-0.1",
                "status": status if status in ("invalid_input", "runtime_error") else "computed",
                "native": native, "provenance": provenance}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        return _error("runtime_error", str(exc), provenance)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    try:
        with args.request.open("rb") as stream:
            request = load_json(stream.read(MAX_INPUT_BYTES + 1))
        result = run_native(request, args.runtime, args.timeout)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        result = _error("invalid_input", str(exc), {"adapter_version": ADAPTER_VERSION})
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 2 if result["status"] == "invalid_input" else 3 if result["status"] == "runtime_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
