#!/usr/bin/env python3
"""Fast, persistent, dependency-free search for Compiled HTML Help files."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1
HTML_SUFFIXES = {".htm", ".html"}
HIDDEN_TAGS = {"script", "style", "noscript", "template", "svg"}
CODE_TAGS = {"pre", "code", "kbd", "samp"}
SPACE_RE = re.compile(r"[\t\r\f\v ]+")
CHARSET_RE = re.compile(br"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", re.I)
INVALID_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = self.code_depth = self.title_depth = 0
        self.text: list[str] = []
        self.code: list[str] = []
        self.title: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        self.hidden_depth += tag in HIDDEN_TAGS
        self.code_depth += tag in CODE_TAGS
        self.title_depth += tag == "title"

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
        if tag in CODE_TAGS and self.code_depth:
            self.code_depth -= 1
        if tag == "title" and self.title_depth:
            self.title_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.hidden_depth or not data.strip():
            return
        self.text.append(data)
        if self.code_depth:
            self.code.append(data)
        if self.title_depth:
            self.title.append(data)


def clean_text(parts: Iterable[str]) -> str:
    lines = []
    for part in parts:
        for line in html.unescape(part).replace("\xa0", " ").splitlines():
            line = SPACE_RE.sub(" ", line).strip()
            if line:
                lines.append(line)
    return "\n".join(lines)


def decode_html(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    match = CHARSET_RE.search(raw[:8192])
    candidates = [match.group(1).decode("ascii", "ignore")] if match else []
    candidates += ["utf-8", "gb18030", "big5", "cp1252"]
    for encoding in candidates:
        if encoding:
            try:
                return raw.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                pass
    return raw.decode("utf-8", errors="replace")


def parse_page(path: Path) -> tuple[str, str, str]:
    parser = VisibleTextParser()
    parser.feed(decode_html(path))
    return clean_text(parser.title) or path.stem, clean_text(parser.text), clean_text(parser.code)


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS documents(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, source_path TEXT NOT NULL UNIQUE,
          source_size INTEGER NOT NULL, source_mtime_ns INTEGER NOT NULL,
          html_root TEXT NOT NULL, indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pages(
          id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          relative_path TEXT NOT NULL, html_path TEXT NOT NULL, title TEXT NOT NULL,
          body TEXT NOT NULL, commands TEXT NOT NULL, UNIQUE(doc_id, relative_path)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
          title, body, commands, content='pages', content_rowid='id', tokenize='trigram'
        );
        CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
          INSERT INTO pages_fts(rowid,title,body,commands) VALUES(new.id,new.title,new.body,new.commands);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
          INSERT INTO pages_fts(pages_fts,rowid,title,body,commands)
          VALUES('delete',old.id,old.title,old.body,old.commands);
        END;
        """
    )
    connection.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    connection.commit()


def database_path(library: Path) -> Path:
    return library.resolve() / "chm-search.sqlite3"


def find_7zip() -> str:
    candidates = []
    if os.name == "nt":
        candidates.append(r"C:\Program Files\7-Zip\7z.exe")
    candidates.extend(filter(None, (shutil.which(name) for name in ("7z", "7zz", "7za"))))
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("7-Zip not found; install 7z/7zz/7za or add it to PATH")


def safe_name(name: str) -> str:
    return (INVALID_NAME_RE.sub("_", name).strip(" .") or "chm-document")[:100]


def extraction_dir(library: Path, source: Path) -> Path:
    digest = hashlib.sha256(str(source).lower().encode("utf-8")).hexdigest()[:10]
    return library / "html" / f"{safe_name(source.stem)}-{digest}"


def build(source: Path, library: Path, force: bool = False) -> dict:
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"CHM file not found: {source}")
    stat = source.stat()
    database = database_path(library)
    with connect(database) as connection:
        ensure_schema(connection)
        old = connection.execute("SELECT * FROM documents WHERE source_path=?", (str(source),)).fetchone()
        if old and not force and old["source_size"] == stat.st_size and old["source_mtime_ns"] == stat.st_mtime_ns:
            count = connection.execute("SELECT count(*) FROM pages WHERE doc_id=?", (old["id"],)).fetchone()[0]
            return {"status": "unchanged", "document": old["name"], "pages": count}

    target = extraction_dir(library.resolve(), source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chm-search-", dir=target.parent) as temp_name:
        temp = Path(temp_name)
        process = subprocess.run([find_7zip(), "x", str(source), f"-o{temp}", "-y"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
        if process.returncode:
            raise RuntimeError(process.stderr.strip() or "7-Zip extraction failed")
        files = sorted(p for p in temp.rglob("*") if p.is_file() and p.suffix.lower() in HTML_SUFFIXES)
        if not files:
            raise ValueError("No .htm or .html pages found in the CHM")
        rows = [(p.relative_to(temp).as_posix(), *parse_page(p)) for p in files]
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(temp), str(target))

    now = datetime.now(timezone.utc).isoformat()
    with connect(database) as connection:
        ensure_schema(connection)
        old = connection.execute("SELECT id FROM documents WHERE source_path=?", (str(source),)).fetchone()
        if old:
            connection.execute("DELETE FROM pages WHERE doc_id=?", (old["id"],))
            connection.execute("UPDATE documents SET name=?,source_size=?,source_mtime_ns=?,html_root=?,indexed_at=? WHERE id=?", (source.stem, stat.st_size, stat.st_mtime_ns, str(target), now, old["id"]))
            doc_id = old["id"]
        else:
            cursor = connection.execute("INSERT INTO documents(name,source_path,source_size,source_mtime_ns,html_root,indexed_at) VALUES(?,?,?,?,?,?)", (source.stem, str(source), stat.st_size, stat.st_mtime_ns, str(target), now))
            doc_id = cursor.lastrowid
        connection.executemany("INSERT INTO pages(doc_id,relative_path,html_path,title,body,commands) VALUES(?,?,?,?,?,?)", ((doc_id, rel, str((target / rel).resolve()), title, body, commands) for rel, title, body, commands in rows))
        connection.commit()
    return {"status": "indexed", "document": source.stem, "pages": len(rows), "database": str(database)}


def query_terms(query: str) -> list[str]:
    try:
        return [item for item in shlex.split(query) if item.strip()]
    except ValueError:
        return query.split()


def search(connection: sqlite3.Connection, query: str, limit: int, mode: str, docs: list[str] | None) -> list[dict]:
    tokens = query_terms(query)
    if not tokens:
        return []
    doc_sql, doc_params = "", []
    if docs:
        doc_sql = " AND (" + " OR ".join("d.name LIKE ?" for _ in docs) + ")"
        doc_params = [f"%{name}%" for name in docs]
    joiner = " AND " if mode == "all" else " OR "
    if all(len(token) >= 3 for token in tokens):
        match = joiner.join('"' + token.replace('"', '""') + '"' for token in tokens)
        sql = f"""SELECT p.id page_id,d.name document,p.title,p.html_path,p.relative_path,
          snippet(pages_fts,1,'[',']','...',24) snippet,-bm25(pages_fts,5.0,1.0,3.0) score
          FROM pages_fts JOIN pages p ON p.id=pages_fts.rowid JOIN documents d ON d.id=p.doc_id
          WHERE pages_fts MATCH ?{doc_sql} ORDER BY bm25(pages_fts,5.0,1.0,3.0) LIMIT ?"""
        rows = connection.execute(sql, [match, *doc_params, limit]).fetchall()
    else:
        clauses, params = [], []
        for token in tokens:
            clauses.append("lower(p.title||char(10)||p.body||char(10)||p.commands) LIKE ?")
            params.append(f"%{token.lower()}%")
        sql = f"""SELECT p.id page_id,d.name document,p.title,p.html_path,p.relative_path,
          substr(replace(p.body,char(10),' '),1,260) snippet,0.0 score
          FROM pages p JOIN documents d ON d.id=p.doc_id
          WHERE ({joiner.join(clauses)}){doc_sql} LIMIT ?"""
        rows = connection.execute(sql, [*params, *doc_params, limit]).fetchall()
    return [dict(row) for row in rows]


def list_documents(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute("SELECT d.id,d.name,d.source_path,d.indexed_at,count(p.id) pages FROM documents d LEFT JOIN pages p ON p.doc_id=d.id GROUP BY d.id ORDER BY d.name").fetchall()
    return [dict(row) for row in rows]


def read_page(connection: sqlite3.Connection, page_id: int) -> dict:
    row = connection.execute("SELECT p.id page_id,d.name document,p.title,p.html_path,p.relative_path,p.body,p.commands FROM pages p JOIN documents d ON d.id=p.doc_id WHERE p.id=?", (page_id,)).fetchone()
    if not row:
        raise KeyError(f"Page not found: {page_id}")
    return dict(row)


def emit(value, output_format: str = "json") -> None:
    if output_format == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif output_format == "markdown":
        for item in value:
            print(f"- [{item['title']}](<{item['html_path']}>) — {item['document']} (page {item['page_id']})")
            print(f"  {item['snippet']}")
    else:
        for item in value:
            print(f"[{item.get('page_id', item.get('id', '-'))}] {item.get('title', item.get('name'))}")
            print(f"    {item.get('html_path', item.get('source_path', ''))}")
            if item.get("snippet"):
                print(f"    {item['snippet']}")


def serve(connection: sqlite3.Connection) -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            action = request.get("action")
            if action == "search":
                result = search(connection, request["query"], int(request.get("limit", 10)), request.get("mode", "all"), request.get("docs"))
            elif action == "read":
                result = read_page(connection, int(request["page_id"]))
            elif action == "list":
                result = list_documents(connection)
            else:
                raise ValueError("action must be search, read, or list")
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)


def make_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Build and search fast local CHM indexes")
    sub = root.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build")
    build_cmd.add_argument("--library", type=Path, default=Path.cwd() / ".chm-search")
    build_cmd.add_argument("chm", type=Path, nargs="+")
    build_cmd.add_argument("--force", action="store_true")
    search_cmd = sub.add_parser("search")
    search_cmd.add_argument("--library", type=Path, default=Path.cwd() / ".chm-search")
    search_cmd.add_argument("query")
    search_cmd.add_argument("-n", "--limit", type=int, default=10)
    search_cmd.add_argument("--mode", choices=("all", "any"), default="all")
    search_cmd.add_argument("--doc", action="append")
    search_cmd.add_argument("--format", choices=("json", "text", "markdown"), default="json")
    read_cmd = sub.add_parser("read")
    read_cmd.add_argument("--library", type=Path, default=Path.cwd() / ".chm-search")
    read_cmd.add_argument("page_id", type=int)
    read_cmd.add_argument("--format", choices=("json", "text"), default="text")
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--library", type=Path, default=Path.cwd() / ".chm-search")
    serve_cmd = sub.add_parser("serve")
    serve_cmd.add_argument("--library", type=Path, default=Path.cwd() / ".chm-search")
    return root


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = make_parser().parse_args()
    try:
        if args.command == "build":
            emit([build(path, args.library, args.force) for path in args.chm])
            return 0
        with connect(database_path(args.library)) as connection:
            ensure_schema(connection)
            if args.command == "search":
                emit(search(connection, args.query, max(1, args.limit), args.mode, args.doc), args.format)
            elif args.command == "read":
                result = read_page(connection, args.page_id)
                print(json.dumps(result, ensure_ascii=False, indent=2) if args.format == "json" else result["body"])
            elif args.command == "list":
                emit(list_documents(connection))
            elif args.command == "serve":
                serve(connection)
        return 0
    except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
