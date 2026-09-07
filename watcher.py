#!/usr/bin/env python3
"""
Crystal Sports tennis-court cancellation watcher.

Continuously sweeps every bookable day (today .. today+DAYS_AHEAD) at both venues
(Crystal Sports, Crystal Sports G), every court, every hourly slot, and fires an alert the
moment a slot flips from booked -> free (i.e. somebody cancelled) or a new day opens.

Only the plain court-booking flow (booking.php) is watched - that is the "non-coaching"
product; coach bookings live in a different flow on the site.

Run:  python watcher.py            (loops forever)
      python watcher.py --once     (one sweep, then exit - good for testing)
      python watcher.py --dry-run  (never sends to LINE/Discord, prints only)

All tuning is via environment variables - see .env.example.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from crystal_api import BASE_URL, CrystalAPI, Slot
from notifiers import Notifier, StdoutNotifier, broadcast, build_notifiers_from_env

TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Bangkok"))
BOOKING_URL = f"{BASE_URL}/booking.php"

log = logging.getLogger("crystal.watch")


# --------------------------------------------------------------------------- config
def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def env_set(name: str) -> set[str]:
    raw = os.getenv(name, "").strip()
    return {x.strip().lower() for x in raw.split(",") if x.strip()} if raw else set()


class Config:
    days_ahead = env_int("DAYS_AHEAD", 14)            # site UI exposes today + 14 days
    request_gap = env_float("REQUEST_GAP_SEC", 2.0)   # pause between per-day requests
    sweep_gap = env_float("SWEEP_GAP_SEC", 5.0)       # pause between full sweeps
    state_file = Path(os.getenv("STATE_FILE", "state.json"))
    alert_on_startup = os.getenv("ALERT_ON_STARTUP", "1") not in ("0", "false", "no")
    heartbeat_hours = env_float("HEARTBEAT_HOURS", 0)  # 0 = off; else "still alive" ping
    fail_alert_after_min = env_float("FAIL_ALERT_AFTER_MIN", 15)
    # optional filters - empty means "everything"
    venues = env_set("FILTER_VENUES")       # e.g. "LOC001" or "crystal sports g"
    courts = env_set("FILTER_COURTS")       # e.g. "north-1,g south-2"
    weekdays = env_set("FILTER_WEEKDAYS")   # e.g. "sat,sun"
    time_from = os.getenv("FILTER_TIME_FROM", "").strip()  # e.g. "17:00"
    time_to = os.getenv("FILTER_TIME_TO", "").strip()      # e.g. "22:00" (slot start < this)


# --------------------------------------------------------------------------- helpers
def now_tz() -> datetime:
    return datetime.now(TZ)


def fmt_date(d: str) -> str:
    dt = datetime.strptime(d, "%Y-%m-%d")
    return dt.strftime("%a %d %b %Y")


def slot_is_bookable_now(s: Slot, now: datetime) -> bool:
    """Replicates the site's rule: on today's date, slots that started before the
    current hour are greyed out; the slot for the current hour is still bookable."""
    if s.date != now.strftime("%Y-%m-%d"):
        return True
    return s.time_start >= now.strftime("%H:00")


def passes_filters(s: Slot, cfg: Config) -> bool:
    if cfg.venues and not ({s.loc_id.lower(), s.loc_name.lower()} & cfg.venues):
        return False
    if cfg.courts and s.stadium_name.lower() not in cfg.courts:
        return False
    if cfg.weekdays:
        wd = datetime.strptime(s.date, "%Y-%m-%d").strftime("%a").lower()
        if wd not in cfg.weekdays and wd[:3] not in cfg.weekdays:
            return False
    if cfg.time_from and s.time_start < cfg.time_from:
        return False
    if cfg.time_to and s.time_start >= cfg.time_to:
        return False
    return True


def compress_hours(times: list[str]) -> str:
    """['06:00','07:00','08:00','14:00'] -> '06:00-09:00, 14:00-15:00'"""
    hours = sorted({int(t[:2]) for t in times})
    out, start, prev = [], None, None
    for h in hours:
        if start is None:
            start = prev = h
        elif h == prev + 1:
            prev = h
        else:
            out.append((start, prev + 1))
            start = prev = h
    if start is not None:
        out.append((start, prev + 1))
    return ", ".join(f"{a:02d}:00-{b % 24:02d}:00" for a, b in out)


def format_alert(slots: list[Slot], title: str) -> str:
    """Group by date -> venue -> court, compress hours into ranges."""
    by_date: dict[str, dict[tuple[str, str], list[Slot]]] = {}
    for s in slots:
        by_date.setdefault(s.date, {}).setdefault((s.loc_name, s.stadium_name), []).append(s)

    lines = [title]
    for date in sorted(by_date):
        lines.append("")
        lines.append(f"📅 {fmt_date(date)}")
        courts = by_date[date]
        for (venue, court) in sorted(courts, key=lambda k: (k[0], sort_key(k[1]))):
            ss = courts[(venue, court)]
            hours = compress_hours([s.time_start for s in ss])
            price = ss[0].price
            price_txt = f" (฿{float(price):.0f}/hr)" if price.replace(".", "", 1).isdigit() else ""
            lines.append(f"• {venue} — {court}: {hours}{price_txt}")
    lines.append("")
    lines.append(f"Book now: {BOOKING_URL}")
    return "\n".join(lines)


def sort_key(court: str) -> tuple:
    order = {"north": 0, "center": 1, "south": 2}
    name = court.lower().replace("g ", "")
    for k, v in order.items():
        if name.startswith(k):
            return (v, name)
    return (9, name)


# --------------------------------------------------------------------------- state
class State:
    """key -> {"free": bool, "seen": iso, "since": iso}. Persisted so a restart doesn't re-alert."""

    def __init__(self, path: Path):
        self.path = path
        self.slots: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.slots = json.loads(self.path.read_text()).get("slots", {})
                log.info("loaded state: %d slots from %s", len(self.slots), self.path)
            except Exception as e:  # noqa: BLE001
                log.warning("could not read state file (%s) - starting fresh", e)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved": now_tz().isoformat(), "slots": self.slots}))
        tmp.replace(self.path)

    def prune_before(self, date: str) -> None:
        self.slots = {k: v for k, v in self.slots.items() if k.split("|", 1)[0] >= date}


# --------------------------------------------------------------------------- watcher
class Watcher:
    def __init__(self, cfg: Config, api: CrystalAPI, notifiers: list[Notifier]):
        self.cfg = cfg
        self.api = api
        self.notifiers = notifiers
        self.state = State(cfg.state_file)
        self.first_sweep = not self.state.slots  # brand-new state => treat first sweep as baseline
        self.stop = False
        self.fail_since: datetime | None = None
        self.fail_alerted = False
        self.last_heartbeat = now_tz()
        self.sweeps = 0

    # -- one day ----------------------------------------------------------------
    def check_day(self, date: str, now: datetime) -> list[Slot]:
        slots = self.api.get_day(date)  # both venues, all courts
        newly_free: list[Slot] = []
        seen_iso = now.isoformat(timespec="seconds")

        for s in slots:
            prev = self.state.slots.get(s.key)
            free = s.is_free and slot_is_bookable_now(s, now)
            if free and passes_filters(s, self.cfg):
                was_free = prev["free"] if prev else None
                if was_free is False:
                    newly_free.append(s)                       # cancellation!
                elif was_free is None:
                    # first time we see this slot: either startup baseline or a new day
                    if (self.first_sweep and self.cfg.alert_on_startup) or not self.first_sweep:
                        newly_free.append(s)
            self.state.slots[s.key] = {
                "free": free,
                "seen": seen_iso,
                "since": (prev["since"] if prev and prev.get("free") == free else seen_iso),
            }

        n_free = sum(1 for s in slots if s.is_free)
        log.info("%s  rows=%d free=%d new=%d", date, len(slots), n_free, len(newly_free))
        if not slots:
            log.warning("%s returned 0 rows - site may have changed or date out of range", date)
        return newly_free

    # -- one full sweep -----------------------------------------------------------
    def sweep(self) -> None:
        now = now_tz()
        today = now.strftime("%Y-%m-%d")
        self.state.prune_before(today)
        total_new: list[Slot] = []

        for offset in range(self.cfg.days_ahead + 1):
            if self.stop:
                break
            date = (now + timedelta(days=offset)).strftime("%Y-%m-%d")
            try:
                newly = self.check_day(date, now_tz())
                self.mark_ok()
            except Exception as e:  # noqa: BLE001
                self.mark_fail(e)
                newly = []
            if newly:
                if self.first_sweep:
                    total_new.extend(newly)         # batch the startup summary into one message
                else:
                    # alert immediately per day - don't wait for the whole sweep to finish
                    broadcast(self.notifiers, format_alert(newly, "🎾 Tennis court slot AVAILABLE!"))
            self.state.save()
            time.sleep(self.cfg.request_gap)

        if self.first_sweep:
            if total_new:
                broadcast(self.notifiers, format_alert(total_new, "🎾 Watcher started — currently free slots:"))
            elif self.cfg.alert_on_startup:
                broadcast(self.notifiers, "🎾 Watcher started. No free slots right now — I'll ping you the moment one opens.")
            self.first_sweep = False

        self.sweeps += 1
        self.maybe_heartbeat()

    # -- health -------------------------------------------------------------------
    def mark_ok(self) -> None:
        if self.fail_since is not None:
            if self.fail_alerted:
                broadcast(self.notifiers, "✅ Watcher: booking site reachable again, monitoring resumed.")
            self.fail_since, self.fail_alerted = None, False

    def mark_fail(self, err: Exception) -> None:
        if self.fail_since is None:
            self.fail_since = now_tz()
        mins = (now_tz() - self.fail_since).total_seconds() / 60
        log.error("day check failed (%.0f min of failures): %s", mins, err)
        if not self.fail_alerted and mins >= self.cfg.fail_alert_after_min:
            broadcast(self.notifiers, f"⚠️ Watcher: booking site unreachable for {mins:.0f} min ({err}). Still retrying.")
            self.fail_alerted = True
        time.sleep(min(60, 5 * max(1, mins)))  # back off while the site is down

    def maybe_heartbeat(self) -> None:
        if self.cfg.heartbeat_hours <= 0:
            return
        if (now_tz() - self.last_heartbeat).total_seconds() >= self.cfg.heartbeat_hours * 3600:
            free = sum(1 for v in self.state.slots.values() if v.get("free"))
            broadcast(self.notifiers, f"💓 Watcher alive — {self.sweeps} sweeps done, {free} free slot(s) tracked.")
            self.last_heartbeat = now_tz()

    # -- main loop ----------------------------------------------------------------
    def run_forever(self, max_runtime: float = 0) -> None:
        """Loop until stopped. max_runtime > 0 exits after that many seconds (for cron hosts
        such as GitHub Actions, where each job must finish and the next one takes over)."""
        log.info("watching %d day(s) ahead, request gap %.1fs, sweep gap %.1fs, notifiers=%s",
                 self.cfg.days_ahead, self.cfg.request_gap, self.cfg.sweep_gap,
                 [n.name for n in self.notifiers])
        started = time.time()
        while not self.stop:
            t0 = time.time()
            self.sweep()
            took = time.time() - t0
            log.info("sweep #%d done in %.0fs", self.sweeps, took)
            if max_runtime and time.time() - started + took + self.cfg.sweep_gap > max_runtime:
                log.info("max runtime %.0fs reached, exiting for the next scheduled run", max_runtime)
                break
            time.sleep(self.cfg.sweep_gap)
        self.state.save()
        log.info("stopped")


# --------------------------------------------------------------------------- entry
def load_dotenv(path: Path = Path(".env")) -> None:
    """Tiny .env loader so no extra dependency is needed."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="single sweep then exit")
    ap.add_argument("--dry-run", action="store_true", help="print alerts only, never push")
    ap.add_argument("--test-alert", action="store_true", help="send a test message to every configured channel and exit")
    ap.add_argument("--max-runtime", type=float, default=env_float("MAX_RUNTIME_SEC", 0),
                    help="exit after N seconds (for cron hosts like GitHub Actions); 0 = run forever")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = Config()
    notifiers = [StdoutNotifier()] if args.dry_run else build_notifiers_from_env()

    if args.test_alert:
        broadcast(notifiers, "🎾 Test alert from Crystal Sports watcher — notifications are working.")
        return 0

    watcher = Watcher(cfg, CrystalAPI(), notifiers)

    def _stop(*_):
        log.info("shutdown requested")
        watcher.stop = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if args.once:
        watcher.sweep()
        watcher.state.save()
        return 0
    watcher.run_forever(max_runtime=args.max_runtime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
