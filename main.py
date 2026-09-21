import json
import random

import colorama
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())
colorama.init()

from consts import (client, QUESTION_PROMPT, LANGUAGE_DICT, QUESTION, ANSWERS, DIFFICULTY, CORRECT_ANSWER,
                    QUESTION_CATEGORIES)


def main(players_name: str):
    print(f'Hi, {players_name}')
    print('Let\'s play a game!')
    language = language_select()
    complexity = 0
    categories = QUESTION_CATEGORIES.copy()
    random.shuffle(categories)
    random.shuffle(categories)
    question = create_question(complexity, language, categories[0])
    # print(question)
    result = True
    while result:
        result, complexity = ask_question(question)
        if complexity > 15 or not result:
            break
        categories.pop(0)
        question = create_question(complexity, language, categories[0])
    if complexity > 15:
        print(colorama.Fore.LIGHTGREEN_EX)
        print('Congratulations! You have completed the game.' + colorama.Fore.RESET)
        exit(0)
    else:
        print(colorama.Fore.YELLOW)
        print(f'Unfortunately, you lose this time.')
        print('Next time try harder.')
        exit(1)


def language_select():
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


def create_question(complexity: int, language: str, category: str):
    if not 0 <= complexity <= 15:
        raise ValueError("Complexity must be between 0 and 15")

    response = client().responses.create(

        instructions=QUESTION_PROMPT,

        input=f"Generate a question with difficulty level {complexity}. "
              f"Topic: {category}. "
              f"Use {language} to choose a language of question.",
        max_output_tokens=500,

        reasoning={
            "effort": "none"
        },
        temperature=1.0
    )
    # print(repr(response.output_text))
    return json.loads(response.output_text)


def ask_question(question_object):
    complexity = question_object[DIFFICULTY]
    question = question_object[QUESTION]
    answers = question_object[ANSWERS]
    correct_answer = question_object[CORRECT_ANSWER]

    print(colorama.Fore.BLUE)
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
