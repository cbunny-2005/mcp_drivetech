"""
One-time: create the "Oscar AI" bot account and add it to a team so it shows up
in that team's DIRECT MESSAGES list. DMs sent to this account are intercepted in
main.py (create_direct_message) and answered by the agent.

Usage:
    python scripts/create_oscar_bot.py            # team from RFQ_ORG_TEAM_ID (default 1)
    python scripts/create_oscar_bot.py 1          # explicit team id

Prints the bot's user id — put it in .env as OSCAR_AI_BOT_ID so the DM route
knows which peer to route through the agent.
"""

import os
import sys
import secrets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from models.database import SessionLocal
from models.orm_models import User, UserTeam
from services import user_service, team_service

BOT_NAME = "Oscar AI"
BOT_USERNAME = "oscar_ai"


def main():
    team_id = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("RFQ_ORG_TEAM_ID", "1"))
    db = SessionLocal()
    try:
        bot = db.query(User).filter(User.username == BOT_USERNAME).first()
        if bot:
            print(f"[=] Oscar AI already exists: user_id={bot.id}")
        else:
            bot = User(
                name=BOT_NAME, username=BOT_USERNAME, email=None,
                password_hash=user_service.hash_password(secrets.token_urlsafe(24)),
                is_active=1, account_type="team_member",
            )
            db.add(bot); db.commit(); db.refresh(bot)
            print(f"[+] Created Oscar AI: user_id={bot.id}")

        membership = db.query(UserTeam).filter_by(user_id=bot.id, team_id=team_id).first()
        if membership:
            print(f"[=] Already a member of team {team_id}")
        else:
            team_service.add_member(db, team_id, bot.id, role="team_member")
            print(f"[+] Added Oscar AI to team {team_id}")

        print()
        print("Next: add this line to .env —")
        print(f"    OSCAR_AI_BOT_ID={bot.id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
