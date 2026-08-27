import logging
import os
import time
from datetime import datetime, timezone

from app import (
    app,
    db,
    get_backup_schedule,
    write_backup_archive,
    compute_next_backup_utc,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("thirtystash.backup")

POLL_SECONDS = 30
FAILURE_RETRY_SECONDS = 300


def run_once():
    with app.app_context():
        schedule = get_backup_schedule()
        if not schedule.enabled or not schedule.next_backup_at:
            return POLL_SECONDS

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        if now_utc < schedule.next_backup_at:
            return POLL_SECONDS

        try:
            path = write_backup_archive(schedule.destination_subdir)
            completed = datetime.now(timezone.utc).replace(tzinfo=None)
            schedule.last_backup_at = completed
            schedule.last_backup_path = path
            schedule.last_error = None
            schedule.next_backup_at = compute_next_backup_utc(
                schedule.backup_time,
                schedule.timezone_name,
                datetime.now(timezone.utc),
            )
            db.session.commit()
            log.info("Scheduled backup completed: %s", path)
            return POLL_SECONDS
        except Exception as exc:
            db.session.rollback()
            schedule = get_backup_schedule()
            schedule.last_error = str(exc)
            db.session.commit()
            log.exception("Scheduled backup failed")
            return FAILURE_RETRY_SECONDS


def main():
    log.info("ThirtyStash daily backup scheduler started")
    while True:
        try:
            sleep_for = run_once()
        except Exception:
            log.exception("Backup scheduler loop error")
            sleep_for = FAILURE_RETRY_SECONDS
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
