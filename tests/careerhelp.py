"""Test helper: give a user enough recorded progress to clear a career gate.

Paper trading and ranked play are unlocked by career level (see
app/routes/progress.MODE_UNLOCKS), so tests that start those modes need a user
who has actually earned them. This writes the same aggregates the real game
writes when sessions and missions are scored.
"""
from app import db
from app.models.mission import Mission, MissionAttempt
from app.models.progress import UserProgress


def promote(app, user_id, level=2):
    """Record enough progress for `user_id` to reach `level` (2 or 3)."""
    with app.app_context():
        from app.routes.missions import SEED_MISSIONS
        from app.routes.progress import get_or_create_progress, CURRICULUM

        # Missions must exist for a passed-attempt row to reference.
        for spec in SEED_MISSIONS[:3]:
            if Mission.query.filter_by(slug=spec["slug"]).first() is None:
                db.session.add(Mission(
                    slug=spec["slug"], title=spec["title"], brief=spec["brief"],
                    difficulty_tier=spec["difficulty_tier"], xp_reward=spec["xp_reward"],
                    rules=spec["rules"], is_active=True))
        db.session.commit()

        p = get_or_create_progress(user_id)
        p.sessions_scored = 12
        p.total_trades_all = 20
        p.trades_with_stops_all = 20          # 100% stop usage
        p.discipline_sum = 12 * 90.0          # avg discipline 90
        if level >= 3:                        # level 3 also wants lessons done
            lessons = []
            for unit in CURRICULUM:
                lessons.extend(unit["lessons"])
            p.completed_lessons = lessons[:8]
        db.session.commit()

        # missions_passed counts DISTINCT missions, so only add attempts for
        # missions this user hasn't already passed.
        need = 1 if level < 3 else 3
        have = {a.mission_id for a in
                MissionAttempt.query.filter_by(user_id=user_id, passed=True).all()}
        for m in Mission.query.all():
            if len(have) >= need:
                break
            if m.id in have:
                continue
            db.session.add(MissionAttempt(mission_id=m.id, user_id=user_id, passed=True))
            have.add(m.id)
        db.session.commit()


def level_of(app, user_id):
    with app.app_context():
        from app.routes.progress import career_level_of
        return career_level_of(user_id)
