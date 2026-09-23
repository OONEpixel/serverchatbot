import unittest
from unittest.mock import patch

from app.games import GameManager
from app.online_board import BoardPlayer, BoardServer, format_duration, render_online_board, _ranked_players


class GamesTest(unittest.TestCase):
    def test_guess_round_is_shared_by_actors_in_one_group(self):
        games = GameManager()
        with patch("app.games.secrets.randbelow", return_value=41):
            opening, announced = games.play("main", "matrix:one", "Alice", "#game guess start")
        self.assertTrue(announced)
        self.assertIn("Alice", opening)
        result, announced = games.play("main", "minecraft:two", "Steve", "#game guess 42")
        self.assertTrue(announced)
        self.assertIn("猜中了", result)
        self.assertNotIn("main", games.rounds)

    def test_games_are_group_scoped_and_throttled(self):
        games = GameManager()
        with patch("app.games.secrets.randbelow", return_value=0):
            games.play("main", "one", "Alice", "#game guess start")
        result, broadcast = games.play("other", "two", "Steve", "#game guess 1")
        self.assertFalse(broadcast)
        self.assertIn("暂无", result)
        result, broadcast = games.play("main", "one", "Alice", "#game roll")
        self.assertFalse(broadcast)
        self.assertIn("太快", result)

    def test_board_is_png_even_with_offline_servers(self):
        png, width, height = render_online_board([
            BoardServer("vanilla", "1.21", "online", 2, (
                BoardPlayer("Steve", 90, True), BoardPlayer("Alex", 3600, False),
            )),
            BoardServer("survival", "生存服", "offline"),
        ])
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(900, width)
        self.assertGreater(height, 400)

    def test_longest_online_player_is_ranked_first(self):
        server = BoardServer("vanilla", "1.21", "online", 2, (
            BoardPlayer("Steve", 90, True), BoardPlayer("Alex", 3600, False),
        ))
        self.assertEqual(["Alex", "Steve"], [p.name for p in _ranked_players(server)])
        self.assertEqual("1小时00分", format_duration(3600))
        self.assertEqual("1天", format_duration(86400))
        self.assertEqual("—", format_duration(None))
