from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from define_ai_provider import LLMClient

LANGUAGE_DICT = {
    1: "English",
    2: "Russian",
    3: "Hebrew",
    4: "German",
    5: "French",
    6: "Spanish",
    7: "Italian"
}

QUESTION_PROMPT = """
You are the host of a quiz show similar to "Who Wants to Be a Millionaire?".

Generate exactly one multiple-choice trivia question.

Use the following difficulty scale:

0 — Trivial. Extremely obvious facts known by almost everyone.
1 — Very easy. Basic everyday knowledge.
2 — Easy. Common general knowledge most adults are expected to know.
3 — Easy-medium. Familiar facts, but not completely obvious.
4 — Medium-low. Basic school knowledge or common cultural knowledge.
5 — Medium. General knowledge expected from a reasonably well-informed person.
6 — Medium. Requires some education or exposure to the subject.
7 — Medium-high. Requires solid general erudition.
8 — Challenging. A knowledgeable person may know the answer, but many people will not.
9 — Difficult. Requires strong general knowledge or familiarity with the topic.
10 — Very difficult. Requires advanced knowledge or recall of less commonly known facts.
11 — Expert-level general knowledge. Likely known mainly by enthusiasts or highly educated people.
12 — Very advanced. Requires deep knowledge of a specific field or unusually strong erudition.
13 — Specialist. Typically requires significant knowledge of the relevant subject.
14 — Expert specialist. Requires detailed and uncommon knowledge.
15 — Extremely difficult. Even experts in the broader field may find the question challenging.

Requirements:
- Ask exactly one clear and unambiguous question.
- Provide exactly four answer choices: A, B, C, and D.
- Exactly one answer must be correct.
- Incorrect answers must be plausible.
- The difficulty must come from the required knowledge, not from confusing wording or trick phrasing.
- Do not ask subjective or ambiguous questions.
- Avoid questions where more than one answer could reasonably be considered correct.
- Avoid facts that may change over time.
- Randomize the position of the correct answer.
- Match the requested difficulty level as closely as possible.

Return only valid JSON in this format:

{
    "difficulty": 7,
    "question": "Question text",
    "answers": {
        "A": "Answer A",
        "B": "Answer B",
        "C": "Answer C",
        "D": "Answer D"
    },
    "correct_answer": "B"
}
"""

QUESTION_CATEGORIES = [
    "Ancient history",
    "Medieval history",
    "Modern history",
    "World geography",
    "Countries and capitals",
    "Cities and landmarks",
    "Oceans, rivers, and lakes",
    "Mountains and natural landscapes",
    "Astronomy and space",
    "Physics",
    "Chemistry",
    "Biology",
    "Human anatomy",
    "Medicine and health",
    "Animals",
    "Plants and botany",
    "Earth science and geology",
    "Weather and climate",
    "Mathematics",
    "Computer science",
    "Technology",
    "Inventions and discoveries",
    "Engineering",
    "Literature",
    "Poetry",
    "Authors and books",
    "Languages and linguistics",
    "Mythology",
    "Religion and world beliefs",
    "Philosophy",
    "Psychology",
    "Visual arts",
    "Architecture",
    "Classical music",
    "Popular music",
    "Musical instruments",
    "Cinema",
    "Television",
    "Theater",
    "Comics and graphic novels",
    "Video games",
    "Board games and puzzles",
    "Sports",
    "Olympic Games",
    "Food and cooking",
    "Drinks and culinary traditions",
    "Fashion and clothing",
    "Transportation",
    "Everyday objects and household knowledge",
    "Traditions, customs, and world culture"
]

QUESTION = "question"
ANSWERS = "answers"
DIFFICULTY = "difficulty"
CORRECT_ANSWER = "correct_answer"

LLM_PROVIDER = "LLM_PROVIDER"
OPENAI = "openai"
NVIDIA = "nvidia"
BEDROCK = "bedrock"
BEDROCK_RUNTIME = "bedrock-runtime"
OPENAI_API_KEY = "OPENAI_API_KEY"
OPENAI_MODEL_ID = "OPENAI_MODEL_ID"
NVIDIA_API_KEY = "NVIDIA_API_KEY"
NVIDIA_MODEL_ID = "NVIDIA_MODEL_ID"
AWS_REGION = "AWS_REGION"
BEDROCK_MODEL_ID = "BEDROCK_MODEL_ID"

TEXT = "text"
ROLE = "role"
USER = "user"
SYSTEM = "system"
CONTENT = "content"
UNKNOWN_PROVIDER = "Unknown provider"
MAX_TOKENS = "maxTokens"
TEMPERATURE = "temperature"
OUTPUT = "output"
MESSAGE = "message"

MAX_RETRIES = 5
MAX_DIFFICULTY = 15


def client() -> "LLMClient":
    """
    Lazily create the configured LLM client.
    :return: Configured LLMClient instance.
    """
    from define_ai_provider import get_client
    return get_client()
