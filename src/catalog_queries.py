#!/usr/bin/env python3
"""Bounded, read-only catalogue queries; retrieval never chooses a BaZi rule."""
import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3

MAX_LIMIT = 50


@contextlib.contextmanager
def connect(database):
    path = Path(database).resolve(strict=True)
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        yield con
    finally:
        con.close()


def _limit(value):
    if type(value) is not int or value < 1 or value > MAX_LIMIT:
        raise ValueError("limit must be an integer between 1 and 50")
    return value


def _query(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise ValueError("query must contain 1 to 200 characters")
    return value.strip()


def _like(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _passage(row):
    data = dict(row)
    actual = hashlib.sha256(data["text"].encode("utf-8")).hexdigest()
    if actual != data["text_sha256"]:
        raise ValueError("indexed passage integrity failure: " + data["passage_id"])
    data["locator"] = json.loads(data.pop("locator_json"))
    data["original_record"] = json.loads(data.pop("raw_json"))
    if "source_json" in data:
        data["source"] = json.loads(data.pop("source_json"))
    return data


PASSAGE_SELECT = """SELECT p.*, s.raw_json AS source_json
 FROM passages p JOIN sources s USING(source_id) """


def metadata(database):
    with connect(database) as con:
        return {row["key"]: json.loads(row["value"]) for row in con.execute("SELECT * FROM meta")}


def get_passage(database, passage_id):
    with connect(database) as con:
        row = con.execute(PASSAGE_SELECT + "WHERE p.passage_id=?", (passage_id,)).fetchone()
        return _passage(row) if row else None


def search_passages(database, query, limit=10):
    query, limit = _query(query), _limit(limit)
    with connect(database) as con:
        search_meta = json.loads(con.execute("SELECT value FROM meta WHERE key='passage_search'").fetchone()[0])
        ids = []
        if search_meta["fts_tokenizer"] and len(query) >= 3:
            # A literal phrase prevents callers from supplying the FTS grammar.
            phrase = '"' + query.replace('"', '""') + '"'
            ids = [row[0] for row in con.execute(
                "SELECT passage_id FROM passages_fts WHERE passages_fts MATCH ? ORDER BY rank LIMIT ?",
                (phrase, limit))]
        if len(ids) < limit:
            for row in con.execute("SELECT passage_id FROM passages WHERE text LIKE ? ESCAPE '\\' ORDER BY passage_id LIMIT ?",
                                   (_like(query), limit)):
                if row[0] not in ids:
                    ids.append(row[0])
                if len(ids) == limit:
                    break
        hits = []
        for identity in ids:
            row = con.execute(PASSAGE_SELECT + "WHERE p.passage_id=?", (identity,)).fetchone()
            hits.append(_passage(row))
        return {"query": query, "hits": hits,
                "purpose": "source_discovery_only", "establishes_rule_support": False}


def bindings_for_method(database, native_method_exact):
    if not isinstance(native_method_exact, str) or not native_method_exact:
        raise ValueError("exact native method identifier required")
    with connect(database) as con:
        return [json.loads(row[0]) for row in con.execute(
            "SELECT raw_json FROM route_bindings WHERE native_method_exact=? ORDER BY binding_id", (native_method_exact,))]


def evidence_for_rule(database, rule_id):
    with connect(database) as con:
        rule = con.execute("SELECT raw_json FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
        if not rule:
            return None
        records = []
        for edge in con.execute("SELECT * FROM rule_passages WHERE rule_id=? ORDER BY passage_id,support_kind", (rule_id,)):
            passage = con.execute(PASSAGE_SELECT + "WHERE p.passage_id=?", (edge["passage_id"],)).fetchone()
            records.append({"link": json.loads(edge["raw_json"]), "passage": _passage(passage)})
        return {"rule": json.loads(rule[0]), "evidence": records}


def find_asset_paths(database, query, limit=10):
    query, limit = _query(query), _limit(limit)
    with connect(database) as con:
        rows = con.execute("""SELECT p.path,p.kind,p.size,p.sha256,p.format_hint,p.semantic_status,
                 a.relative_path AS object_relative_path
            FROM asset_paths p LEFT JOIN assets a USING(sha256)
            WHERE p.path LIKE ? ESCAPE '\\' ORDER BY p.path LIMIT ?""", (_like(query), limit))
        return [dict(row) for row in rows]


def find_registrations(database, registration_id=None, kind=None, limit=10):
    limit = _limit(limit)
    allowed = {"method", "route", "research_topic", "research_rule"}
    if kind is not None and kind not in allowed:
        raise ValueError("unknown registration kind")
    clauses, values = [], []
    if registration_id is not None:
        clauses.append("r.registration_id=?")
        values.append(registration_id)
    if kind is not None:
        clauses.append("r.kind=?")
        values.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = """SELECT r.*,s.source_path,s.source_sha256,s.document_metadata_json
      FROM registrations r JOIN catalog_sources s USING(source_key)""" + where + " ORDER BY r.kind,r.registration_id,r.source_key LIMIT ?"
    with connect(database) as con:
        answer = []
        for row in con.execute(sql, (*values, limit)):
            item = dict(row)
            item["original_record"] = json.loads(item.pop("raw_json"))
            item["document_metadata"] = json.loads(item.pop("document_metadata_json"))
            item["product_runtime_adoption_inferred"] = False
            answer.append(item)
        return answer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("meta")
    for command in ("search", "paths"):
        cmd = sub.add_parser(command)
        cmd.add_argument("query")
        cmd.add_argument("--limit", type=int, default=10)
    for command in ("passage", "method", "rule"):
        sub.add_parser(command).add_argument("identifier")
    reg = sub.add_parser("registrations")
    reg.add_argument("--id")
    reg.add_argument("--kind")
    reg.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    if args.command == "meta":
        result = metadata(args.db)
    elif args.command == "search":
        result = search_passages(args.db, args.query, args.limit)
    elif args.command == "paths":
        result = find_asset_paths(args.db, args.query, args.limit)
    elif args.command == "passage":
        result = get_passage(args.db, args.identifier)
    elif args.command == "method":
        result = bindings_for_method(args.db, args.identifier)
    elif args.command == "rule":
        result = evidence_for_rule(args.db, args.identifier)
    else:
        result = find_registrations(args.db, args.id, args.kind, args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
