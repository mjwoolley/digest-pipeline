"""Canonical publication-date helpers for the pipeline.

The pipeline is anchored to US Eastern time: cron fires at 3 AM ET and the
podcast feed publishes ET timestamps. A digest, its filename (``<date>.md``),
and the podcast that pairs with it must all agree on which day it is, so every
stage derives "today" from the same timezone here.

This used to be split: ``digest.py`` computed the date in UTC while
``podcast.py`` used America/New_York. On a pre-midnight-ET run (UTC is 4-5 h
ahead and rolls the date over early) the two disagreed by a day, so the digest
published under one date while the podcast was generated for the previous day.
Both stages now call into here, so they can never drift apart.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

# Single source of truth for the pipeline's notion of "today".
PIPELINE_TZ = ZoneInfo("America/New_York")


def now() -> datetime:
    """Timezone-aware 'now' in the pipeline timezone."""
    return datetime.now(PIPELINE_TZ)


def today_str() -> str:
    """Today's publication date as ``YYYY-MM-DD`` (e.g. ``2026-06-15``)."""
    return now().strftime("%Y-%m-%d")
