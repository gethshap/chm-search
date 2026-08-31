import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("chm_search.py")
SPEC = importlib.util.spec_from_file_location("chm_search", MODULE_PATH)
chm_search = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chm_search)


class ChmSearchTests(unittest.TestCase):
    def test_unified_index_searches_chinese_and_short_terms(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            page = root / "page.html"
            page.write_text("<title>云AP无法上线</title><p>备用控制器接管，检查NETCONF连接。</p><pre>display ap all</pre>", encoding="utf-8")
            connection = chm_search.connect(root / "index.sqlite3")
            try:
                chm_search.ensure_schema(connection)
                cursor = connection.execute(
                    "INSERT INTO documents(name,source_path,source_size,source_mtime_ns,html_root,indexed_at) VALUES(?,?,?,?,?,?)",
                    ("manual", "manual.chm", 1, 1, str(root), "now"),
                )
                title, body, commands = chm_search.parse_page(page)
                connection.execute(
                    "INSERT INTO pages(doc_id,relative_path,html_path,title,body,commands) VALUES(?,?,?,?,?,?)",
                    (cursor.lastrowid, "page.html", str(page), title, body, commands),
                )
                connection.commit()
                self.assertEqual(len(chm_search.search(connection, "备用控制器 NETCONF", 10, "all", None)), 1)
                self.assertEqual(len(chm_search.search(connection, "无法", 10, "all", None)), 1)
            finally:
                connection.close()

    def test_parser_excludes_script_and_collects_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "x.htm"
            page.write_text("<title>T</title><script>secret</script><body>Visible<code>display x</code></body>", encoding="utf-8")
            title, body, commands = chm_search.parse_page(page)
            self.assertEqual(title, "T")
            self.assertNotIn("secret", body)
            self.assertIn("display x", commands)


if __name__ == "__main__":
    unittest.main()
