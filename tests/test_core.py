import io
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="thirtystash-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_ROOT / 'test.db'}"
os.environ["BACKUP_ROOT"] = str(TEST_ROOT / "backups")
os.environ["BACKUP_HOST_PATH"] = str(TEST_ROOT / "backups")
os.environ["SECRET_KEY"] = "test-only-secret"
os.environ["OFF_USER_AGENT"] = "ThirtyStash-tests"

import app as ts  # noqa: E402


def reset_database():
    with ts.app.app_context():
        ts.db.session.remove()
        ts.db.drop_all()
        ts.db.create_all()
        ts.ensure_prototype_schema()


def test_unit_conversions():
    assert round(ts.weight_to_kg(220, "lb"), 3) == 99.79
    assert round(ts.height_to_cm(76, "in"), 2) == 193.04
    assert round(ts.mass_to_grams(1, "lb"), 5) == round(ts.GRAMS_PER_POUND, 5)
    assert round(ts.volume_to_liters(5, "gal"), 5) == round(5 * ts.LITERS_PER_GALLON, 5)


def test_tdee_manual_and_calculated():
    manual = ts.HouseholdMember(name="A", calc_profile="manual", tdee_override=2400)
    assert manual.tdee == 2400

    calculated = ts.HouseholdMember(
        name="B",
        age=30,
        weight_kg=80,
        height_cm=180,
        calc_profile="male",
        activity_level="moderate",
    )
    expected_bmr = (10 * 80) + (6.25 * 180) - (5 * 30) + 5
    assert calculated.tdee == round(expected_bmr * ts.ACTIVITY_MULTIPLIERS["moderate"])


def test_food_calorie_math():
    item = ts.FoodItem(
        name="Test food",
        total_grams=680,
        serving_size_g=85,
        calories_per_serving=180,
    )
    assert item.total_calories == 1440


def test_backup_subdir_rejects_traversal():
    for value in ("../outside", "daily/../../outside", "../../outside"):
        try:
            ts.normalize_backup_subdir(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Traversal path was accepted: {value}")


def test_daily_backup_schedule_math():
    now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    result = ts.compute_next_backup_utc("02:00", "America/Denver", now)
    # 02:00 MDT is 08:00 UTC; at 12:00 UTC the next occurrence is the following day.
    assert result == datetime(2026, 8, 27, 8, 0)


def test_backup_archive_is_valid_sqlite():
    reset_database()
    with ts.app.app_context():
        ts.db.session.add(
            ts.HouseholdMember(
                name="Backup Test",
                calc_profile="manual",
                tdee_override=2200,
                is_primary=True,
            )
        )
        ts.db.session.add(
            ts.FoodItem(
                name="Rice",
                total_grams=1000,
                serving_size_g=100,
                calories_per_serving=360,
            )
        )
        ts.db.session.commit()
        archive = ts.create_sqlite_backup_archive().getvalue()

    with zipfile.ZipFile(io.BytesIO(archive), "r") as zf:
        assert {"thirtystash.db", "manifest.json", "RESTORE.txt"}.issubset(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["application"] == "ThirtyStash"
        assert manifest["integrity_check"] == "ok"
        assert manifest["counts"]["food_items"] == 1
        db_bytes = zf.read("thirtystash.db")

    restored = TEST_ROOT / "archive-check.db"
    restored.write_bytes(db_bytes)
    conn = sqlite3.connect(restored)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM food_item").fetchone()[0] == 1
    finally:
        conn.close()


def test_csrf_blocks_untrusted_post_and_allows_form_post():
    reset_database()
    ts.app.config.update(TESTING=True)
    client = ts.app.test_client()

    blocked = client.post(
        "/onboarding",
        data={"name": "No Token", "calc_profile": "manual", "tdee_override": "2000"},
    )
    assert blocked.status_code == 400

    page = client.get("/onboarding")
    assert page.status_code == 200
    with client.session_transaction() as session:
        token = session["_csrf_token"]

    allowed = client.post(
        "/onboarding",
        data={
            "csrf_token": token,
            "name": "Test User",
            "calc_profile": "manual",
            "tdee_override": "2000",
            "activity_level": "sedentary",
            "weight_unit": "lb",
            "height_unit": "in",
        },
        follow_redirects=False,
    )
    assert allowed.status_code == 302

    with ts.app.app_context():
        assert ts.HouseholdMember.query.count() == 1
