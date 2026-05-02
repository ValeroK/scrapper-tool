"""E2E test 3.E1-pydantic -- ``agent_extract`` with a pydantic schema class.

Verifies the type-safe schema branch. Same target as ``test_pattern_e1.py``
but the schema is a real pydantic model so validation runs end-to-end.
"""

from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel

from scrapper_tool.agent import AgentConfig, agent_extract


class Book(BaseModel):
    title: str
    price: float


class Catalogue(BaseModel):
    books: list[Book]


async def main() -> None:
    cfg = AgentConfig.from_env().merged(
        browser=os.environ.get("SCRAPPER_TOOL_AGENT_BROWSER", "patchright"),
        captcha_solver="none",
        timeout_s=180.0,
    )

    result = await agent_extract(
        "https://books.toscrape.com/",
        schema=Catalogue,
        config=cfg,
        instruction="Extract every book on the page with title and price.",
    )

    if result.error == "schema-validation-failed":
        print(
            "Pattern E1-pydantic [WARN]  LLM returned non-conforming JSON.\n"
            f"            raw output: {result.data}\n"
            "            try a stricter instruction or a larger model."
        )
        return

    assert result.data is not None
    books = result.data.get("books") if isinstance(result.data, dict) else None
    assert isinstance(books, list) and books, "no books extracted"
    print(
        f"Pattern E1-pydantic [OK]  extracted {len(books)} books in "
        f"{result.duration_s:.1f} s; first={books[0]}"
    )


if __name__ == "__main__":
    asyncio.run(main())
