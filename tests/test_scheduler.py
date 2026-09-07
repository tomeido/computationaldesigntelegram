from datetime import UTC, datetime, time

import pytest

from compdesign_bot.config import Settings
from compdesign_bot.scheduler import due_slot, next_run


def test_seoul_due_window_uses_local_time_and_five_minute_boundary():
    settings = Settings()
    assert due_slot(datetime(2026, 9, 7, 0, 0, tzinfo=UTC), settings) == "2026-09-07T09:00:00+09:00"
    assert due_slot(datetime(2026, 9, 7, 0, 4, 59, tzinfo=UTC), settings) is not None
    assert due_slot(datetime(2026, 9, 7, 0, 5, tzinfo=UTC), settings) is None
    assert due_slot(datetime(2026, 9, 6, 23, 59, 59, tzinfo=UTC), settings) is None


def test_next_run_rolls_over_local_day():
    settings = Settings()
    scheduled = next_run(datetime(2026, 9, 7, 9, tzinfo=UTC), settings)
    assert scheduled.isoformat() == "2026-09-08T09:00:00+09:00"
    alternate = Settings(timezone="America/New_York", post_times=(time(9),))
    scheduled = next_run(datetime(2026, 9, 7, 12, tzinfo=UTC), alternate)
    assert scheduled.isoformat() == "2026-09-07T09:00:00-04:00"


def test_environment_schedule_is_sorted_deduplicated_and_validated(monkeypatch):
    monkeypatch.setattr("compdesign_bot.config.load_dotenv", lambda: None)
    monkeypatch.setenv("POST_TIMES", "18:00,09:00,09:00")
    monkeypatch.setenv("TIMEZONE", "Asia/Seoul")
    assert Settings.from_env().post_times == (time(9), time(18))
    monkeypatch.setenv("POST_TIMES", "09:00:30")
    with pytest.raises(ValueError, match="POST_TIMES"):
        Settings.from_env()
    monkeypatch.setenv("POST_TIMES", "09:00")
    monkeypatch.setenv("TIMEZONE", "Not/A_Zone")
    with pytest.raises(ValueError, match="TIMEZONE"):
        Settings.from_env()
