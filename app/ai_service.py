import os
from groq import Groq

try:
    from app.config import GROQ_API_KEY, GROQ_MODEL
except ImportError:
    try:
        from config import GROQ_API_KEY, GROQ_MODEL
    except ImportError:
        GROQ_API_KEY = os.getenv("GROQ_API_KEY")
        GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

def get_client() -> Groq | None:
    api_key = GROQ_API_KEY or os.getenv("GROQ_API_KEY")
    return Groq(api_key=api_key) if api_key else None

def get_ai_response(message: str) -> str:
    client = get_client()
    if client is None:
        raise RuntimeError("GROQ_API_KEY is not configured.")
    model = GROQ_MODEL or os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "user", "content": message}
        ]
    )
    return response.choices[0].message.content or ""
