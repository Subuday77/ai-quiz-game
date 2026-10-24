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

DIFFICULTY_LEVELS = (
    "0 — Trivial: universally recognizable facts; an effortless warm-up.",
    "1 — Very easy: everyday facts familiar to most adults.",
    "2 — Easy: common general knowledge; no specialist education.",
    "3 — Easy-medium: familiar subjects requiring a moment of recall.",
    "4 — Moderate: common school knowledge and cultural references.",
    "5 — Medium: knowledge a reasonably well-informed general player may have.",
    "6 — Medium-high: good general erudition; many ordinary players may not know.",
    "7 — Challenging: non-obvious but approachable to curious generalists.",
    "8 — Difficult: substantial general erudition, not professional certification.",
    "9 — Very difficult: uncommon yet recognizable cultural, historical or scientific knowledge.",
    "10 — Advanced trivia: rare facts, still suitable for a broad quiz audience.",
    "11 — Expert general knowledge: uncommon facts that dedicated trivia players may know.",
    "12 — Exceptional general knowledge: obscure but verifiable cross-disciplinary knowledge.",
    "13 — Elite trivia: very obscure cultural, historical, geographical or scientific facts.",
    "14 — Near-final: remarkably difficult yet still fair to a highly erudite non-specialist.",
    "15 — Final challenge: exceptionally rare, interesting, answerable trivia, not a technical exam.",
)

DIFFICULTY_SCALE = "\n".join(DIFFICULTY_LEVELS)

DIFFICULTY_GUIDANCE = (
    "Evaluate difficulty from the perspective of an ordinary educated adult playing a "
    "general-interest trivia game, not from the perspective of a language model, "
    "university professor or domain specialist. Knowing a fact because it is taught "
    "at school does not mean most adult players remember it. "
    "A question at level 0 or 1 is SUPPOSED to be easy: never flag it as too easy "
    "merely for being obvious. Judge difficulty using both the knowledge needed "
    "and the four available answer choices. Do not raise difficulty by inventing "
    "narrow terminology, professional examination questions, or obscure numeric "
    "constants. On levels 10–15 prefer unusually deep general erudition over "
    "specialist-only formulas, molecular structures and exact giant numbers. "
    "Allow a broad variation in subjective difficulty; flag only substantial mismatch."
)

SHARED_DIFFICULTY_INSTRUCTIONS = (
    "Shared 0–15 quiz difficulty scale (authoritative for this game):\n"
    + DIFFICULTY_SCALE + "\n\n" + DIFFICULTY_GUIDANCE
)

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

MAX_DIFFICULTY = 15


def client() -> "LLMClient":
    """
    Lazily create the configured LLM client.
    :return: Configured LLMClient instance.
    """
    from define_ai_provider import get_client
    return get_client()
