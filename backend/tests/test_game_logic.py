"""Run with: python -m unittest discover -s tests -v"""

import unittest

from game_logic import GameState


class GameStateTests(unittest.TestCase):
    def test_fifteen_main_levels_and_no_hints_unlock_bonus(self):
        game = GameState()
        stars = []
        for level in range(15):
            self.assertEqual(game.current_difficulty, level)
            result = game.submit_answer(True)
            if result.achievement:
                stars.append(result.achievement)
        self.assertEqual(stars, ["bronze", "silver", "gold"])
        self.assertEqual(game.correct_main_answers, 15)
        self.assertEqual(game.phase, "bonus")
        self.assertEqual(game.current_difficulty, 15)

    def test_hint_blocks_bonus_but_not_gold(self):
        game = GameState()
        game.record_hint_use()
        for _ in range(14):
            game.submit_answer(True)
        last = game.submit_answer(True)
        self.assertEqual(last.achievement, "gold")
        self.assertEqual(game.phase, "completed")
        with self.assertRaises(RuntimeError):
            _ = game.current_difficulty

    def test_wrong_main_answer_ends_game(self):
        game = GameState()
        game.submit_answer(True)
        result = game.submit_answer(False)
        self.assertFalse(result.correct)
        self.assertEqual(game.correct_main_answers, 1)
        self.assertEqual(game.phase, "lost")
        with self.assertRaises(RuntimeError):
            game.submit_answer(True)

    def test_bonus_failure_preserves_gold(self):
        game = GameState()
        stars = [game.submit_answer(True).achievement for _ in range(15)]
        result = game.submit_answer(False)
        self.assertIn("gold", stars)
        self.assertFalse(result.correct)
        self.assertIsNone(result.achievement)
        self.assertEqual(game.phase, "completed")
        self.assertIs(game.bonus_correct, False)

    def test_bonus_success_awards_platinum(self):
        game = GameState()
        for _ in range(15):
            game.submit_answer(True)
        result = game.submit_answer(True)
        self.assertEqual(result.achievement, "platinum")
        self.assertIs(game.bonus_correct, True)
        self.assertEqual(game.phase, "completed")

    def test_no_hints_during_bonus(self):
        game = GameState()
        for _ in range(15):
            game.submit_answer(True)
        with self.assertRaises(RuntimeError):
            game.record_hint_use()


if __name__ == "__main__":
    unittest.main()
