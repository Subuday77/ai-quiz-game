# AI Quiz Game

A command-line trivia game inspired by "Who Wants to Be a Millionaire?".

The app generates multiple-choice questions with an LLM, increases the difficulty after each correct answer, and ends when the player answers incorrectly or completes all 16 difficulty levels.

## Features

- Interactive terminal quiz flow.
- Language selection for generated questions.
- Difficulty levels from 0 to 15.
- Randomized question categories without repetition within a single game.
- Four-answer multiple-choice format.
- Configurable LLM provider:
  - OpenAI
  - NVIDIA OpenAI-compatible API
  - AWS Bedrock

## Project Structure

```text
.
├── main.py                 # Game loop and question flow
├── define_ai_provider.py   # LLM provider adapter
├── consts.py               # Prompts, constants, categories, provider keys
├── requirements.txt        # Python dependencies
├── .env.example            # Example environment configuration
└── .gitignore
```

## Requirements

- Python 3.10 or newer
- An API key or credentials for one supported LLM provider

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Create a local `.env` file from the example.

### Windows PowerShell

```powershell
Copy-Item .env.example .env
```

### macOS / Linux / Git Bash

```bash
cp .env.example .env
```

Then edit `.env` and set the provider you want to use:

```env
LLM_PROVIDER=openai
```

Supported values:

- `openai`
- `nvidia`
- `bedrock`

Set the required credentials for the selected provider.

### OpenAI

```env
OPENAI_API_KEY=your-key-here
OPENAI_MODEL_ID=your-model-id
```

### NVIDIA

```env
NVIDIA_API_KEY=your-key-here
NVIDIA_MODEL_ID=your-model-id
```

### AWS Bedrock

```env
AWS_ACCESS_KEY_ID=your-key-here
AWS_SECRET_ACCESS_KEY=your-secret-here
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=your-bedrock-model-id
```

## Run

Start the game:

```bash
python main.py
```

The game asks for your name and then lets you choose the language for the questions. Each correct answer advances the difficulty level. One incorrect answer ends the game.

## Example

```text
Hello, what is your name?
Alex
Hi, Alex
Let's play a game!

Choose language.
1 English
2 Russian
3 Hebrew
...

Selected language: English

QUESTION 1:
Which planet is known as the Red Planet?
====================
A. Venus
B. Mars
C. Jupiter
D. Mercury

B
Correct!
```

## Environment Notes

Do not commit `.env`; it contains secrets and should remain ignored by `.gitignore`.

Use `.env.example` as the shared template for required configuration.
