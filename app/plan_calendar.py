"""Business-calendar conversion for meal plans, independent of the host zone."""
import datetime
from zoneinfo import ZoneInfo


UTC = datetime.timezone.utc
BRATISLAVA = ZoneInfo("Europe/Bratislava")


def utc_instant(value: datetime.datetime | None = None) -> datetime.datetime:
    """Return an aware UTC instant; legacy naive timestamps are stored UTC."""
    if value is None:
        return datetime.datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def bratislava_day(value: datetime.datetime | None = None) -> datetime.date:
    """Return the calendar day used for plan weeks and regeneration limits."""
    return utc_instant(value).astimezone(BRATISLAVA).date()


def bratislava_monday(value: datetime.datetime | None = None) -> str:
    """Return the Bratislava calendar week's Monday in the server's ISO form."""
    today = bratislava_day(value)
    return (today - datetime.timedelta(days=today.weekday())).isoformat()
