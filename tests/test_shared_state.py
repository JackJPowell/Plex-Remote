import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main


class SharedStateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_db_path = main.STATE_DB_PATH
        main.STATE_DB_PATH = Path(self.tempdir.name) / "state.sqlite3"
        main._init_state_db()

    def tearDown(self):
        main.STATE_DB_PATH = self.old_db_path
        self.tempdir.cleanup()

    def test_queue_persists_and_reorders_after_delete(self):
        with main._db() as conn:
            conn.execute(
                """
                INSERT INTO playback_queue
                    (position, media_id, title, media_type, artwork_url, created_at)
                VALUES
                    (1, 101, 'First', 'movie', NULL, ?),
                    (2, 202, 'Second', 'show', NULL, ?)
                """,
                (main._now_ts(), main._now_ts()),
            )

        self.assertEqual([item["title"] for item in main._queue_items()], ["First", "Second"])
        main._delete_queue_item(main._queue_items()[0]["id"])
        remaining = main._queue_items()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["title"], "Second")
        self.assertEqual(remaining[0]["position"], 1)

    def test_timer_state_counts_down_and_expires(self):
        with main._db() as conn:
            main._set_setting(conn, "timer_expires_at", str(main._now_ts() + 3600))
            active = main._timer_state(conn)
            main._set_setting(conn, "timer_expires_at", str(main._now_ts() - 1))
            expired = main._timer_state(conn)

        self.assertTrue(active["active"])
        self.assertGreater(active["remaining_seconds"], 0)
        self.assertFalse(expired["active"])
        self.assertEqual(expired["remaining_seconds"], 0)

    def test_active_messages_filter_by_window_and_enabled(self):
        now = main._now_ts()
        with main._db() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (text, starts_at, ends_at, enabled, created_at, updated_at)
                VALUES
                    ('Active', ?, ?, 1, ?, ?),
                    ('Disabled', ?, ?, 0, ?, ?),
                    ('Future', ?, ?, 1, ?, ?)
                """,
                (
                    now - 60, now + 60, now, now,
                    now - 60, now + 60, now, now,
                    now + 60, now + 120, now, now,
                ),
            )

        self.assertEqual([message["text"] for message in main._active_messages()], ["Active"])

    async def test_automation_plays_queue_before_timer_random(self):
        main._automation_lock = __import__("asyncio").Lock()
        with main._db() as conn:
            main._set_setting(conn, "timer_expires_at", str(main._now_ts() + 3600))
            conn.execute(
                """
                INSERT INTO playback_queue
                    (position, media_id, title, media_type, artwork_url, created_at)
                VALUES (1, 303, 'Queued', 'movie', NULL, ?)
                """,
                (main._now_ts(),),
            )

        idle = {"playing": False, "rating_key": None}
        with patch("main._plex_server", return_value=object()), \
             patch("main._plex_now_playing", return_value=idle), \
             patch("main._play_plex_media", new=AsyncMock()) as play:
            result = await main._advance_automation(idle, last_seen_playing=True)

        self.assertTrue(result)
        play.assert_awaited_once_with(303)
        self.assertEqual(main._queue_items(), [])


if __name__ == "__main__":
    unittest.main()
