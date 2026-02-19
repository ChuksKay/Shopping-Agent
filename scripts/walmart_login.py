"""
One-time Walmart.ca login helper.

This is an ALTERNATIVE to the /link Telegram command — useful for running
directly from terminal before starting the bot.

Usage:
    python scripts/walmart_login.py

A headed browser window opens. Log in manually, then press ENTER in this
terminal to save the session. No passwords are stored.
"""

import asyncio
import os
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from agent.walmart import WalmartLinker

SESSION_PATH = os.getenv("SESSION_PATH", "sessions/walmart_session.json")


async def main() -> None:
    session_file = Path(SESSION_PATH)
    if session_file.exists():
        answer = input(
            f"Session already exists at {SESSION_PATH}. Overwrite? [y/N] "
        ).strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    print("Opening Walmart.ca sign-in page in a visible browser...")
    linker = WalmartLinker()
    await linker.start()

    print("\nLog into your Walmart.ca account in the browser window.")
    input("Press ENTER here when you are fully logged in... ")

    if await linker.is_logged_in():
        await linker.save_session(SESSION_PATH)
        print(f"Session saved to {SESSION_PATH} ✅")
    else:
        print(
            "Could not confirm login — the page still looks like a sign-in page.\n"
            "Make sure you completed the login, then try again."
        )

    await linker.close()


if __name__ == "__main__":
    asyncio.run(main())
