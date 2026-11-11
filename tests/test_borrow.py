import _bootstrap
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import borrow

SCHEMA = """
CREATE TABLE item (guid TEXT, title TEXT, original_title TEXT, release_date TEXT,
 air_date TEXT, first_air_date TEXT, imdb_id TEXT, tmdb_id INTEGER, overview TEXT,
 runtime INTEGER, vote_average REAL, vote_count INTEGER, backdrops TEXT,
 backdrop_width INTEGER, backdrop_height INTEGER, logos TEXT, posters TEXT,
 poster_width INTEGER, poster_height INTEGER, production_countries TEXT,
 number_of_episodes INTEGER, number_of_seasons INTEGER, type TEXT, parent_guid TEXT);
CREATE TABLE item_tag (item_guid TEXT, tag TEXT, type TEXT);
CREATE TABLE item_person (item_guid TEXT, person_guid TEXT, role TEXT, job TEXT,
 "order" INTEGER, department TEXT);
CREATE TABLE person (guid TEXT, trim_id TEXT, imdb_id TEXT, tmdb_id INTEGER,
 lan TEXT, name TEXT, original_name TEXT, also_know_as TEXT, biography TEXT,
 know_for_department TEXT, profile_path TEXT, gender INTEGER);
"""

OFFICIAL = ("off1", "不能说的秘密", "Secret", "2026-03-04", "", "", "tt26443616",
            0, "官方简介文本", 99, 7.8, 1234, "/f8/04/x.webp", 1920, 1080,
            "", "/f8/04/p.webp", 1200, 1800, '["US"]', 0, 0, "Movie", "")


class BorrowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fnos_borrow_")
        self.db = os.path.join(self.dir, "trimmedia.db")
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO item VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"
                     "?,?,?,?,?,?,?,?,?,?,?,?)", OFFICIAL)
        conn.execute("INSERT INTO item_tag VALUES ('off1','3','Genre')")
        conn.execute("INSERT INTO item_tag VALUES ('off1','4','Genre')")
        conn.execute("INSERT INTO person VALUES ('per1','tp1','nm1',0,'zh-CN',"
                     "'某演员','Someone','','官方 biography','Acting',"
                     "'/0a/17/av.webp',0)")
        conn.execute("INSERT INTO item_person VALUES "
                     "('off1','per1','主角','Actor',1,'')")
        conn.commit()
        conn.close()
        self.patcher = patch.object(borrow, "_db_dir", return_value=self.dir)
        self.patcher.start()
        borrow._items_cache = None
        borrow._cache.clear()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(lambda: setattr(borrow, "_items_cache", None))
        self.addCleanup(borrow._cache.clear)

    def test_enrich_detail_exact_match(self):
        detail = borrow.enrich_detail({"guid": "x", "genres": [],
                                       "vote_average": "0"},
                                      "不能说的秘密", "2026")
        self.assertEqual(detail["imdb_id"], "tt26443616")
        self.assertEqual(detail["backdrops"], "/f8/04/x.webp")
        self.assertEqual(detail["backdrop_width"], 1920)
        self.assertEqual(detail["runtime"], 99)
        self.assertEqual(detail["genres"], [3, 4])
        self.assertEqual(detail["vote_average"], "7.8")
        self.assertEqual(detail["original_title"], "Secret")

    def test_enrich_detail_fuzzy_simplified_traditional(self):
        detail = borrow.enrich_detail({"guid": "x", "genres": []},
                                      "不能說的·秘密", "2026")
        self.assertEqual(detail["imdb_id"], "tt26443616")

    def test_year_gap_blocks_match(self):
        detail = borrow.enrich_detail({"guid": "x", "genres": []},
                                      "不能说的秘密", "2030")
        self.assertEqual(detail["genres"], [])
        self.assertNotIn("imdb_id", detail)

    def test_enrich_keeps_cms_data_when_no_match(self):
        detail = borrow.enrich_detail({"guid": "x", "overview": "CMS 简介",
                                       "genres": []}, "完全不同的片子", "2024")
        self.assertEqual(detail["overview"], "CMS 简介")

    def test_person_rows_with_profile(self):
        rows = borrow.person_rows("不能说的秘密", "2026")
        self.assertIsNotNone(rows)
        self.assertEqual(rows[0]["name"], "某演员")
        self.assertEqual(rows[0]["job"], "Actor")
        self.assertEqual(rows[0]["role"], "主角")
        self.assertEqual(rows[0]["order"], 1)
        self.assertEqual(rows[0]["profile_path"], "/0a/17/av.webp")

    def test_person_rows_none_when_no_match(self):
        self.assertIsNone(borrow.person_rows("完全不同的片子", "2024"))


if __name__ == "__main__":
    unittest.main()
