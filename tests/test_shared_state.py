import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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
        with main._db_connection() as conn:
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
        with main._db_connection() as conn:
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
        with main._db_connection() as conn:
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

    def test_db_connection_closes_after_context(self):
        with main._db_connection() as conn:
            conn.execute("SELECT 1")

        with self.assertRaisesRegex(Exception, "closed database"):
            conn.execute("SELECT 1")

    def test_plex_server_is_reused_and_closed(self):
        plex = Mock()
        main._plex_server_instance = None
        with patch("main.PlexServer", return_value=plex) as constructor:
            self.assertIs(main._plex_server(), plex)
            self.assertIs(main._plex_server(), plex)
            constructor.assert_called_once()

        main._close_plex_server()
        plex._session.close.assert_called_once_with()
        self.assertIsNone(main._plex_server_instance)

    async def test_automation_plays_queue_before_timer_random(self):
        main._automation_lock = __import__("asyncio").Lock()
        with main._db_connection() as conn:
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

    async def test_play_media_ensures_tv_is_on_before_starting_htpc(self):
        item = Mock(title="Selected Movie", type="movie", ratingKey=404)
        client = Mock()
        idle = {"playing": False, "rating_key": None}
        startup_order = []

        with patch("main._ensure_tv_on", new=AsyncMock(
                 side_effect=lambda: startup_order.append("tv") or True,
             )) as ensure_tv, \
             patch("main._ensure_plex_htpc_running",
                   side_effect=lambda: startup_order.append("htpc") or False) as ensure_htpc, \
             patch("main._plex_server", return_value=object()), \
             patch("main._plex_client", return_value=client), \
             patch("main._plex_now_playing", return_value=idle), \
             patch("main._resolve_play_item", new=AsyncMock(return_value=item)), \
             patch("main._send_play_command", new=AsyncMock(return_value=False)):
            result = await main._play_plex_media(404)

        ensure_tv.assert_awaited_once_with()
        ensure_htpc.assert_called_once_with()
        self.assertEqual(startup_order, ["tv", "htpc"])
        self.assertTrue(result["tv_powered_on"])

    async def test_ensure_tv_on_sends_command_even_when_query_reports_on(self):
        main._tv_status = None
        with patch("main._query_tv_power", new=AsyncMock(return_value="on")), \
             patch("main._cec") as cec:
            powered_on = await main._ensure_tv_on()

        cec.assert_called_once_with("on 0")
        self.assertFalse(powered_on)
        self.assertEqual(main._tv_status, "on")

    async def test_plex_seek_converts_percentage_to_playback_offset(self):
        client = Mock()
        now_playing = {"playing": True, "duration": 7200000}
        with patch("main._plex_server", return_value=object()), \
             patch("main._plex_now_playing", return_value=now_playing), \
             patch("main._plex_client", return_value=client):
            result = await main.plex_seek(main.SeekUpdate(percent=25))

        client.seekTo.assert_called_once_with(1800000, mtype="video")
        self.assertEqual(result["percent"], 25)
        self.assertEqual(result["offset"], 1800000)

    async def test_playback_recovers_once_from_plex_connectivity_failure(self):
        failed = main.HTTPException(
            status_code=502,
            detail="Failed to send play command: HTTPConnectionPool timed out",
        )
        with patch("main._play_plex_media_once", new=AsyncMock(side_effect=[failed, {"status": "ok"}])) as play_once, \
             patch("main._recover_plex_and_retry_playback", new=AsyncMock(return_value={"status": "ok"})) as recover:
            result = await main._play_plex_media(404)

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(play_once.await_count, 1)
        recover.assert_awaited_once_with(404, startup_timeout=main.PLEX_CLIENT_STARTUP_TIMEOUT, random_movie_or_show=False)

    async def test_recovery_state_is_returned_with_playback_state(self):
        main._set_plex_recovery_state(True, "Waiting for Plex to reconnect")
        state = main._playback_state(main._empty_now_playing())

        self.assertEqual(state["recovery"], {"active": True, "stage": "Waiting for Plex to reconnect"})


if __name__ == "__main__":
    unittest.main()
