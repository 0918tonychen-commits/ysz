"""Pin the test environment before any test module is imported.

``bridge`` and ``main`` both call ``load_dotenv()`` at import time, and
``load_dotenv`` does not override variables that are already set. Without this,
whichever module got imported first would let a real local ``.env`` decide the
API key and backend URL for the entire session — so simply adding a test file
that sorts earlier could turn every authenticated request into a 401.
"""

import os

os.environ["DATABASE_URL"] = "postgresql://fake/db"
os.environ["LORA_API_KEY"] = "test-key-123"
os.environ["DISCORD_WEBHOOK_URL"] = ""  # never hit a real webhook from tests
os.environ["BACKEND_URL"] = "https://backend.invalid/update"
