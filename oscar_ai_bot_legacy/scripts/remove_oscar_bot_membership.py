"""
Remove the "Oscar AI" bot from all team memberships so it is NO LONGER a team
member / DM contact. Oscar AI stays a server-side entity — RFQ is triggered via
the main Oscar assistant tab (/chat), which routes tasks to the asker's team lead.

Keeps the user row (id from OSCAR_AI_BOT_ID) so historical task ownership/comments
stay intact; only the team memberships are removed.

Usage:  python scripts/remove_oscar_bot_membership.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from models.database import SessionLocal
from models.orm_models import UserTeam, User

BOT_USERNAME = "oscar_ai"


def main():
    db = SessionLocal()
    try:
        bot = db.query(User).filter(User.username == BOT_USERNAME).first()
        if not bot:
            print("Oscar AI user not found — nothing to remove.")
            return
        rows = db.query(UserTeam).filter(UserTeam.user_id == bot.id).all()
        if not rows:
            print(f"Oscar AI (id {bot.id}) has no team memberships.")
            return
        for r in rows:
            print(f"  removing membership: user={bot.id} team={r.team_id} role={r.role}")
            db.delete(r)
        db.commit()
        print(f"Removed {len(rows)} membership(s). Oscar AI is now server-side only "
              f"(not a DM contact).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
