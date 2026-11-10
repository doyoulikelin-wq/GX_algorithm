#!/usr/bin/env python3
"""Build a derived, read-only-consumable catalogue without rewriting research.

The original archive manifest is imported as declared metadata, not silently
re-verified content. Only explicitly curated rule/passsage edges can serve as
citations; research registrations are kept separate from publication evidence.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import sqlite3
import tempfile

SCHEMA_VERSION = "bazi-evidence-catalog-0.1"
ARCHIVE_REGISTRATIONS = {
    "method": ("outputs/Part1_研究成果_统一可复现交付/当前95方法_运行与来源登记.json",
               "87fef9e0bd104c8545c0549374ef4f87d968184de7ad4709d1ab1f4bd4a366af"),
    "route": ("outputs/Part1_研究成果_统一可复现交付/route_manifest.json",
              "d15b951c94f7c2d671c0294497e7ce71fd2f14df28fbb0a539713c35d0a7d3ff"),
}


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_safe(row):
    return {k: ({"encoding": "base64", "value": base64.b64encode(v).decode("ascii")}
                if isinstance(v, bytes) else v) for k, v in dict(row).items()}


def open_readonly(path):
    path = Path(path).resolve(strict=True)
    for suffix in ("-wal", "-journal"):
        if Path(str(path) + suffix).exists():
            raise ValueError("unsealed SQLite source has a sidecar: " + suffix)
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    return con


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE assets(
 sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, relative_path TEXT NOT NULL,
 verified_sha256 TEXT, verified_at TEXT, raw_json TEXT NOT NULL);
CREATE TABLE asset_paths(
 path TEXT PRIMARY KEY, kind TEXT NOT NULL, size INTEGER, sha256 TEXT,
 format_hint TEXT, semantic_status TEXT NOT NULL DEFAULT 'unreviewed', raw_json TEXT NOT NULL);
CREATE INDEX paths_sha ON asset_paths(sha256);
CREATE INDEX paths_format ON asset_paths(format_hint);
CREATE TABLE archive_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE catalog_sources(
 source_key TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_sha256 TEXT NOT NULL,
 source_kind TEXT NOT NULL, document_metadata_json TEXT NOT NULL);
CREATE TABLE registrations(
 kind TEXT NOT NULL, registration_id TEXT NOT NULL, source_key TEXT NOT NULL,
 status TEXT, raw_json TEXT NOT NULL,
 PRIMARY KEY(kind,registration_id,source_key),
 FOREIGN KEY(source_key) REFERENCES catalog_sources(source_key));
CREATE INDEX registrations_id ON registrations(registration_id);
CREATE TABLE sources(
 source_id TEXT PRIMARY KEY, title TEXT NOT NULL, edition TEXT, content_sha256 TEXT,
 source_kind TEXT NOT NULL, raw_json TEXT NOT NULL);
CREATE TABLE passages(
 passage_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, text TEXT NOT NULL,
 text_sha256 TEXT NOT NULL, verification_status TEXT NOT NULL,
 locator_json TEXT NOT NULL, raw_json TEXT NOT NULL,
 FOREIGN KEY(source_id) REFERENCES sources(source_id));
CREATE TABLE rules(
 rule_id TEXT PRIMARY KEY, method_id TEXT, rule_kind TEXT NOT NULL,
 status TEXT NOT NULL, raw_json TEXT NOT NULL);
CREATE TABLE rule_passages(
 rule_id TEXT NOT NULL, passage_id TEXT NOT NULL, support_kind TEXT NOT NULL,
 raw_json TEXT NOT NULL, PRIMARY KEY(rule_id,passage_id,support_kind),
 FOREIGN KEY(rule_id) REFERENCES rules(rule_id),
 FOREIGN KEY(passage_id) REFERENCES passages(passage_id));
CREATE TABLE route_bindings(
 binding_id TEXT PRIMARY KEY, method_id TEXT NOT NULL, native_method_exact TEXT NOT NULL,
 raw_json TEXT NOT NULL);
CREATE INDEX bindings_exact ON route_bindings(native_method_exact);
CREATE TABLE route_binding_rules(
 binding_id TEXT NOT NULL, rule_id TEXT NOT NULL, PRIMARY KEY(binding_id,rule_id),
 FOREIGN KEY(binding_id) REFERENCES route_bindings(binding_id),
 FOREIGN KEY(rule_id) REFERENCES rules(rule_id));
"""


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, dump(value)))


def import_archive(con, archive):
    manifest = Path(archive).resolve(strict=True) / "manifest.sqlite"
    digest_before = sha_file(manifest)
    src = open_readonly(manifest)
    try:
        required = {"objects": {"sha256", "size", "relative_path"},
                    "entries": {"path", "kind", "size", "sha256"}}
        for table, columns in required.items():
            observed = {r[1] for r in src.execute("PRAGMA table_info(" + table + ")")}
            if not columns <= observed:
                raise ValueError("archive schema mismatch: " + table)
        for row in src.execute("SELECT * FROM objects"):
            d = dict(row)
            con.execute("INSERT INTO assets VALUES(?,?,?,?,?,?)", (
                d["sha256"], d["size"], d["relative_path"],
                d.get("verified_sha256"), d.get("verified_at"), dump(json_safe(row))))
        for row in src.execute("SELECT * FROM entries"):
            d = dict(row)
            suffix = Path(d["path"]).suffix.lower() if d["kind"] == "file" else ""
            con.execute("INSERT INTO asset_paths VALUES(?,?,?,?,?,?,?)", (
                d["path"], d["kind"], d["size"], d["sha256"], suffix,
                "unreviewed", dump(json_safe(row))))
        if src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
            for row in src.execute("SELECT key,value FROM meta"):
                con.execute("INSERT INTO archive_meta VALUES(?,?)", tuple(row))
    finally:
        src.close()
    if sha_file(manifest) != digest_before:
        raise ValueError("source manifest changed while building catalogue")
    set_meta(con, "source_manifest", {"path": str(manifest), "sha256": digest_before,
        "object_hashes_recomputed_this_build": False,
        "meaning": "Archive-declared object checks and metadata are preserved; this build does not rehash all objects."})


def registration_status(row):
    for key in ("effective_disposition", "current_status", "status", "stage", "native_scope_kind"):
        if key in row:
            value = row[key]
            return value if isinstance(value, str) else dump(value)
    return None


def import_registration_document(con, path, kind, array_key, id_keys, display_path=None):
    path = Path(path)
    payload_bytes = path.read_bytes()
    payload = json.loads(payload_bytes)
    rows = payload if isinstance(payload, list) else payload.get(array_key, [])
    if not isinstance(rows, list):
        raise ValueError("registration array required: " + str(path))
    relative = display_path or str(path)
    digest = hashlib.sha256(payload_bytes).hexdigest()
    source_key = kind + ":" + hashlib.sha256(relative.encode()).hexdigest()[:24]
    metadata = {k: v for k, v in payload.items() if k != array_key} if isinstance(payload, dict) else {}
    con.execute("INSERT INTO catalog_sources VALUES(?,?,?,?,?)",
                (source_key, relative, digest, kind, dump(metadata)))
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("registration must be an object: " + relative)
        identity = next((str(row[k]) for k in id_keys if row.get(k) is not None), "row:" + str(index))
        con.execute("INSERT INTO registrations VALUES(?,?,?,?,?)", (
            kind, identity, source_key, registration_status(row), dump(row)))
    return len(rows)


def import_archive_registration(con, archive, kind, array_key, id_keys):
    original_path, expected_sha = ARCHIVE_REGISTRATIONS[kind]
    src = open_readonly(Path(archive) / "manifest.sqlite")
    try:
        row = src.execute("""SELECT e.kind,e.size,e.sha256,o.relative_path
          FROM entries e JOIN objects o USING(sha256) WHERE e.path=?""", (original_path,)).fetchone()
    finally:
        src.close()
    if row is None or row["kind"] != "file" or row["sha256"] != expected_sha:
        raise ValueError("expected frozen registration is absent or has changed: " + original_path)
    relative = PurePosixPath(row["relative_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe archive content path")
    archive = Path(archive).resolve(strict=True)
    object_path = archive / relative
    resolved = object_path.resolve(strict=True)
    if archive not in resolved.parents or object_path.is_symlink() or not object_path.is_file():
        raise ValueError("archive registration requires an in-archive regular object")
    if object_path.stat().st_size != row["size"] or sha_file(object_path) != expected_sha:
        raise ValueError("archive registration content hash mismatch: " + original_path)
    return import_registration_document(con, object_path, kind, array_key, id_keys,
                                        "archive:" + original_path)


def import_repo_registrations(con, repo):
    repo = Path(repo).resolve(strict=True)
    research = repo / "research/68项补齐/registry.json"
    if research.exists():
        import_registration_document(con, research, "research_topic", "items", ("id",),
                                     "github:" + research.relative_to(repo).as_posix())
    for path in sorted((repo / "frozen-kernels").rglob("*.json")):
        if "rules" not in path.name and "rulecards" not in path.name:
            continue
        payload = json.loads(path.read_bytes())
        key = "cards" if isinstance(payload, dict) and "cards" in payload else "rules"
        import_registration_document(con, path, "research_rule", key, ("id", "rule_id"),
                                     "github:" + path.relative_to(repo).as_posix())
    set_meta(con, "repository_path", str(repo))


def _identifier(row, key):
    value = row.get(key, row.get("id"))
    if not isinstance(value, str) or not value:
        raise ValueError("missing nonempty identifier: " + key)
    return value


def import_evidence(con, path):
    payload_bytes = Path(path).read_bytes()
    seed = json.loads(payload_bytes)
    if not isinstance(seed, dict):
        raise ValueError("evidence seed must be an object")
    for row in seed.get("sources", []):
        con.execute("INSERT INTO sources VALUES(?,?,?,?,?,?)", (
            _identifier(row, "source_id"), row["title"], row.get("edition_label", row.get("edition")),
            row.get("content_sha256", row.get("sha256")), row.get("source_kind", "unclassified"), dump(row)))
    for row in seed.get("passages", []):
        text = row.get("text", row.get("quote"))
        if not isinstance(text, str) or not text:
            raise ValueError("passage must contain nonempty original text")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        expected = row.get("text_sha256", row.get("quote_sha256"))
        if expected and expected != digest:
            raise ValueError("passage text hash mismatch: " + _identifier(row, "passage_id"))
        if row.get("quote_unpunctuated_sha256"):
            actual = hashlib.sha256(row["quote_unpunctuated"].encode("utf-8")).hexdigest()
            if actual != row["quote_unpunctuated_sha256"]:
                raise ValueError("unpunctuated quote hash mismatch")
        locator = row.get("locator", {key: row[key] for key in (
            "section", "pdf_pages", "printed_pages", "page_location") if key in row})
        con.execute("INSERT INTO passages VALUES(?,?,?,?,?,?,?)", (
            _identifier(row, "passage_id"), row["source_id"], text, digest,
            row.get("verification_status", "unreviewed"), dump(locator), dump(row)))
    for row in seed.get("rules", []):
        con.execute("INSERT INTO rules VALUES(?,?,?,?,?)", (
            _identifier(row, "rule_id"), row.get("method_id"), row.get("rule_kind", "unclassified"),
            row.get("status", "unreviewed"), dump(row)))
    for row in seed.get("rule_passages", []):
        con.execute("INSERT INTO rule_passages VALUES(?,?,?,?)", (
            row["rule_id"], row["passage_id"], row["support_kind"], dump(row)))
    for row in seed.get("route_bindings", []):
        identity = _identifier(row, "binding_id")
        native = row.get("native_method_exact")
        if not isinstance(native, str) or not native:
            raise ValueError("binding requires a literal native_method_exact, not fuzzy matching")
        con.execute("INSERT INTO route_bindings VALUES(?,?,?,?)", (
            identity, row.get("method_id", native), native, dump(row)))
        rule_ids = row.get("rule_ids", [row["rule_id"]] if "rule_id" in row else [])
        if not rule_ids:
            raise ValueError("binding requires at least one explicit rule")
        con.executemany("INSERT INTO route_binding_rules VALUES(?,?)", ((identity, rid) for rid in rule_ids))
    set_meta(con, "evidence_seed", {"path": str(Path(path).resolve()),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "document_metadata": {k: v for k, v in seed.items() if k not in (
            "sources", "passages", "rules", "rule_passages", "route_bindings")}})
    set_meta(con, "seed_sha256", hashlib.sha256(payload_bytes).hexdigest())


def build_fts(con):
    try:
        con.execute("CREATE VIRTUAL TABLE passages_fts USING fts5(passage_id UNINDEXED,text,tokenize='trigram')")
        tokenizer = "trigram"
    except sqlite3.OperationalError:
        try:
            con.execute("CREATE VIRTUAL TABLE passages_fts USING fts5(passage_id UNINDEXED,text,tokenize='unicode61')")
            tokenizer = "unicode61"
        except sqlite3.OperationalError:
            tokenizer = None
    if tokenizer:
        con.execute("INSERT INTO passages_fts SELECT passage_id,text FROM passages")
    set_meta(con, "passage_search", {"fts_tokenizer": tokenizer,
        "substring_fallback": True, "retrieval_does_not_establish_rule_support": True})


def build(args):
    output = Path(args.output).absolute()
    if output.exists():
        raise FileExistsError("choose a new versioned database path; existing indexes are never overwritten")
    output.parent.mkdir(parents=True, exist_ok=True)
    source_roots = [Path(args.archive).resolve(), Path(args.repo).resolve()]
    resolved_parent = output.parent.resolve()
    if any(root == resolved_parent or root in resolved_parent.parents for root in source_roots):
        raise ValueError("derived index must be outside source archive and source repository")
    fd, temp_name = tempfile.mkstemp(prefix=".catalog-building-", suffix=".sqlite", dir=output.parent)
    os.close(fd)
    con = sqlite3.connect(temp_name)
    try:
        con.executescript(SCHEMA)
        con.execute("BEGIN")
        set_meta(con, "schema_version", SCHEMA_VERSION)
        set_meta(con, "built_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
        set_meta(con, "policies", {
            "original_archive_unchanged": True, "registration_is_not_automatic_adoption": True,
            "largest_revision_is_not_current_policy": True,
            "research_records_are_not_classical_quotations": True,
            "only_explicit_rule_passage_edges_support_citations": True})
        import_archive(con, args.archive)
        import_repo_registrations(con, args.repo)
        method_count = (import_registration_document(con, args.method_registry, "method", "methods", ("component", "method_id", "id"))
                        if args.method_registry else import_archive_registration(con, args.archive, "method", "methods", ("component", "method_id", "id")))
        route_count = (import_registration_document(con, args.route_registry, "route", "routes", ("route_id", "id"))
                       if args.route_registry else import_archive_registration(con, args.archive, "route", "routes", ("route_id", "id")))
        if method_count != 95 or route_count != 138:
            raise ValueError("this delivery expects exactly 95 method and 138 route registrations")
        import_evidence(con, args.evidence_seed)
        build_fts(con)
        tables = ("assets", "asset_paths", "registrations", "sources", "passages", "rules", "rule_passages", "route_bindings")
        counts = {table: con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in tables}
        counts["registrations_by_kind"] = dict(con.execute("SELECT kind,COUNT(*) FROM registrations GROUP BY kind"))
        counts["paths_by_kind"] = dict(con.execute("SELECT kind,COUNT(*) FROM asset_paths GROUP BY kind"))
        counts["unique_object_bytes"] = con.execute("SELECT SUM(size) FROM assets").fetchone()[0] or 0
        set_meta(con, "counts", counts)
        bad = con.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise ValueError("broken evidence references: " + repr(bad[:10]))
        con.commit()
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
        con.close()
        # Never replace an index another process may have published meanwhile.
        os.link(temp_name, output)
        os.unlink(temp_name)
        output.chmod(0o444)
        result = {"schema_version": SCHEMA_VERSION, "output": str(output),
                  "sha256": sha_file(output), "bytes": output.stat().st_size,
                  "counts": counts, "integrity": "ok"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    except BaseException:
        con.close()
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("archive", "repo", "output", "evidence-seed"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--method-registry", help="Optional alternate registry; omit to verify and use the frozen archive record")
    parser.add_argument("--route-registry", help="Optional alternate registry; omit to verify and use the frozen archive record")
    build(parser.parse_args())


if __name__ == "__main__":
    main()
