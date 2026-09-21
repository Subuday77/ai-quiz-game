import json
import random
import sys
from typing import Any

import colorama
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())
colorama.init()

from consts import (client, QUESTION_PROMPT, LANGUAGE_DICT, QUESTION, ANSWERS, DIFFICULTY, CORRECT_ANSWER,
                    QUESTION_CATEGORIES)
from config import MAX_DIFFICULTY, MAX_OUTPUT_TOKENS, TEMPERATURE


def main(players_name: str) -> None:
    """
    Run the quiz game for the selected player.
    :param players_name: Name of the player shown in the greeting.
    :return: None.
    """
    print(f'Hi, {players_name}')
    print('Let\'s play a game!')
    language = language_select()
    complexity = 0
    category_index = 0
    categories = QUESTION_CATEGORIES.copy()
    random.shuffle(categories)
    question = create_question(complexity, language, categories[category_index])
    # print(question)
    result = True
    while result:
        result, complexity = ask_question(question)
        if complexity > MAX_DIFFICULTY or not result:
            break
        categories.pop(category_index)
        category_index = random.randrange(len(categories))
        question = create_question(complexity, language, categories[category_index])
    if complexity > MAX_DIFFICULTY:
        print(colorama.Fore.LIGHTGREEN_EX)
        print('Congratulations! You have completed the game.' + colorama.Fore.RESET)
        sys.exit(0)
    else:
        print(colorama.Fore.YELLOW)
        print(f'Unfortunately, you lose this time.')
        print('Next time try harder.')
        sys.exit(1)


def language_select() -> str:
    """
    Ask the player to choose a supported question language.
    :return: Selected language name.
    """
    while True:
        print("Choose language.")

        for key, value in LANGUAGE_DICT.items():
            print(key, value)

        choice = input()

        if choice.isdigit():
            language = LANGUAGE_DICT.get(int(choice))

            if language is not None:
                print(f"Selected language: {language}")
                return language

        print("Invalid choice. Try again.")


def create_question(complexity: int, language: str, category: str) -> dict[str, Any]:
    """
    Generate one quiz question using the configured LLM provider.
    :param complexity: Difficulty level from 0 to 15.
    :param language: Language to use for the generated question.
    :param category: Topic category for the generated question.
    :return: Question data with difficulty, question text, answers, and correct answer.
    Raise:
        ValueError: If complexity is outside the supported range.
    """
    if not 0 <= complexity <= MAX_DIFFICULTY:
        raise ValueError(f"Complexity must be between 0 and {MAX_DIFFICULTY}")

    response = client().responses.create(

        instructions=QUESTION_PROMPT,

        input=f"Generate a question with difficulty level {complexity}. "
              f"Topic: {category}. "
              f"Use {language} to choose a language of question.",
        max_output_tokens=MAX_OUTPUT_TOKENS,

        reasoning={
            "effort": "none"
        },
        temperature=TEMPERATURE
    )
    # print(repr(response.output_text))
    return json.loads(response.output_text)


def ask_question(question_object: dict[str, Any]) -> tuple[bool, int]:
    """
    Display a question, read the player's answer, and calculate the next difficulty.
    :param question_object: Question data returned by create_question.
    :return: Tuple with answer correctness and resulting difficulty level.
    """
    complexity = question_object[DIFFICULTY]
    question = question_object[QUESTION]
    answers = question_object[ANSWERS]
    correct_answer = question_object[CORRECT_ANSWER]

    print(colorama.Fore.LIGHTMAGENTA_EX)
    print(f"QUESTION {complexity + 1}:")
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
        return True, complexity + 1

    print(colorama.Fore.RED + "Incorrect!" + colorama.Fore.RESET)
    print(f"The correct answer was {correct_answer}. {answers[correct_answer]}")
    return False, complexity


if __name__ == '__main__':
    player_name = input('Hello, what is your name?\n')
    main(player_name)
