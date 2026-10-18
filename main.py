"""Terminal UI for the quiz. All progression rules live in game_logic.py."""

from typing import Any
import random

import colorama
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())
colorama.init()

from consts import (  # noqa: E402 - load environment before constructing a provider
    LANGUAGE_DICT, QUESTION_CATEGORIES, QUESTION, ANSWERS, CORRECT_ANSWER,
)
from game_logic import GameState  # noqa: E402
from question_generator import create_question  # noqa: E402


def main(players_name: str) -> None:
    """Run a terminal game using the standalone game engine."""
    print(f"Hi, {players_name}")
    print("Let's play a game!")
    language = language_select()

    categories = QUESTION_CATEGORIES.copy()
    random.shuffle(categories)
    game = GameState()

    while game.phase in ("main", "bonus"):
        if not categories:
            raise RuntimeError("Not enough unique categories for this game")

        category = categories.pop()
        question = create_question(game.current_difficulty, language, category)
        is_correct = ask_question(question, game.correct_main_answers + 1)
        outcome = game.submit_answer(is_correct)

        if outcome.achievement:
            print(f"Achievement unlocked: {outcome.achievement} star!")
        if outcome.phase == "bonus":
            print("Perfect run without hints! Secret question 16 unlocked.")
        if game.bonus_correct is False:
            print("The bonus answer was incorrect, but your gold star is safe.")

    if game.phase == "lost":
        print(colorama.Fore.YELLOW + "Unfortunately, you lose this time.")
        print("Next time try harder." + colorama.Fore.RESET)
    else:
        print(colorama.Fore.LIGHTGREEN_EX)
        print("Congratulations! You have completed the main game.")
        print(colorama.Fore.RESET)


def language_select() -> str:
    """Read a supported language from the terminal."""
    while True:
        print("Choose language.")
        for key, value in LANGUAGE_DICT.items():
            print(key, value)

        choice = input().strip()
        if choice.isdigit():
            language = LANGUAGE_DICT.get(int(choice))
            if language is not None:
                print(f"Selected language: {language}")
                return language
        print("Invalid choice. Try again.")


def ask_question(question_object: dict[str, Any], number: int) -> bool:
    """Display a question and return whether the player's choice is correct."""
    question = question_object[QUESTION]
    answers = question_object[ANSWERS]
    correct_answer = question_object[CORRECT_ANSWER]

    print(colorama.Fore.LIGHTMAGENTA_EX)
    print(f"QUESTION {number}:")
    print(question)
    print("=" * 20)

    print(colorama.Fore.CYAN)
    for key, value in answers.items():
        print(f"{key}. {value}")

    choice = input().strip().upper()
    while choice not in answers:
        print(colorama.Fore.RED + "Invalid input. Try again." + colorama.Fore.RESET)
        choice = input().strip().upper()

    if choice == correct_answer:
        print(colorama.Fore.GREEN + "Correct!" + colorama.Fore.RESET)
        return True

    print(colorama.Fore.RED + "Incorrect!" + colorama.Fore.RESET)
    print(f"The correct answer was {correct_answer}. {answers[correct_answer]}")
    return False


if __name__ == "__main__":
    player_name = input("Hello, what is your name?\n")
    main(player_name)
