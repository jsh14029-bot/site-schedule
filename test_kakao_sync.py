import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kakao_sync import DATA_FIELDS, KAKAO_ROOM_NAME, drain_inbox, enqueue_batch, import_batch, is_target_room, validate_batch


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE schedules (id INTEGER PRIMARY KEY AUTOINCREMENT," + ",".join(field + " TEXT" for field in DATA_FIELDS) + ", completed INTEGER NOT NULL DEFAULT 0, deleted_at TEXT)")
        self.batch = {"version": 1, "source": "test-room", "schedules": [{"schedule_date": "2026-10-23", "category": "타설", "title": "전기실 지붕층 타설", "location": "전기실", "schedule_time": None, "end_time": None, "source_id": "message-1"}]}

    def tearDown(self):
        self.connection.close()

    def test_idempotent_and_unknown_values_blank(self):
        self.assertEqual(len(import_batch(self.connection, self.batch)["inserted"]), 1)
        self.assertEqual(len(import_batch(self.connection, self.batch)["skipped"]), 1)
        row = self.connection.execute("SELECT schedule_time,end_time,quantity,vehicle_count FROM schedules").fetchone()
        self.assertEqual(row, (None, None, "", ""))

    def test_room_name_is_exact(self):
        self.assertTrue(is_target_room("일정공유방☆"))
        for name in ("일정공유방", "일정공유방★", "다른방", "일정공유방☆ "):
            self.assertFalse(is_target_room(name))
        packet = dict(self.batch, room_name=KAKAO_ROOM_NAME)
        validate_batch(packet)
        with self.assertRaises(ValueError):
            validate_batch(dict(packet, room_name="다른방"))

    def test_legacy_existing_schedule_and_deleted_schedule_not_recreated(self):
        record = self.batch["schedules"][0]
        self.connection.execute("INSERT INTO schedules (schedule_date,category,title,location,deleted_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP)", tuple(record[key] for key in ("schedule_date", "category", "title", "location")))
        self.connection.commit()
        self.assertEqual(import_batch(self.connection, self.batch)["inserted"], [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 1)

    def test_all_validated_before_writing(self):
        packet = copy.deepcopy(self.batch)
        invalid = dict(packet["schedules"][0], schedule_date="2026-02-30", source_id="message-2")
        packet["schedules"].append(invalid)
        with self.assertRaises(ValueError):
            import_batch(self.connection, packet)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 0)

    def test_transaction_rolls_back_whole_batch(self):
        packet = copy.deepcopy(self.batch)
        packet["schedules"].append(dict(packet["schedules"][0], title="fail", source_id="message-2"))
        self.connection.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON schedules WHEN NEW.title = 'fail' BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            import_batch(self.connection, packet)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM schedules").fetchone()[0], 0)

    def test_inbox_and_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            path = enqueue_batch(self.batch, folder)
            self.assertTrue(path.exists())
            self.assertEqual(len(drain_inbox(self.connection, folder)[0]["inserted"]), 1)
            self.assertEqual(drain_inbox(self.connection, folder), [])
            enqueue_batch(self.batch, folder)
            self.assertEqual(len(drain_inbox(self.connection, folder)[0]["skipped"]), 1)
            (Path(folder) / "invalid.json").write_text("{}")
            self.assertIn("error", drain_inbox(self.connection, folder)[0])
            self.assertTrue((Path(folder) / "failed" / "invalid.json").exists())

    def test_two_databases_receive_identical_schedule(self):
        second = sqlite3.connect(":memory:")
        second.execute("CREATE TABLE schedules (id INTEGER PRIMARY KEY AUTOINCREMENT," + ",".join(field + " TEXT" for field in DATA_FIELDS) + ")")
        payload = json.dumps(self.batch).encode()
        import_batch(self.connection, payload)
        import_batch(second, payload)
        sql = "SELECT " + ",".join(DATA_FIELDS) + " FROM schedules"
        self.assertEqual(self.connection.execute(sql).fetchall(), second.execute(sql).fetchall())
        second.close()


if __name__ == "__main__":
    unittest.main()
