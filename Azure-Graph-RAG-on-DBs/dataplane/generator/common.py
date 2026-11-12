##############################################################################
# dataplane/generator/common.py
#
# PURPOSE:
#   The foundation every other generator file stands on. Three kinds of thing
#   live here and nothing else:
#
#     Settings  reads config/generator.yaml — the seed, the scale factor and
#               the five-year date window — into one object that gets passed
#               to every builder.
#     Dates     business days, month ends, "move this to the next working
#               day", "give me a random working day in this range".
#     Names     fixed lists of first names, surnames, cities and words, used
#               to build fictitious people and companies.
#
# WHAT PROBLEM DOES THIS SOLVE:
#   Seven generator files build 20 tables that have to agree with each other.
#   A transaction must fall on a date that has a NAV. A folio must open in a
#   city that exists in the investors table. An AUM month end must match the
#   month ends used by portfolio_holdings.
#
#   If each file read the config itself, created its own random generator and
#   kept its own city list, those agreements would hold by luck. One file
#   using a different calendar, or a second random generator, and the tables
#   quietly stop lining up — and the failure appears as a validation error
#   three stages later with no obvious cause.
#
#   So: ONE config read, ONE random generator, ONE calendar, ONE city list.
#   Everything else imports from here.
#
# THE SINGLE MOST IMPORTANT PROPERTY OF THIS FILE:
#   Every random number in the entire generator comes from one seeded
#   generator created here. Which means:
#
#       same seed  ->  byte-identical database, every time
#
#   That is not a nicety. It is what makes a bug reproducible, what makes the
#   planted patterns land in the same place on every machine, and what lets
#   tests/test_stage2_data.py assert exact things about the data.
#
#   It also means ANY file that calls np.random.* directly instead of
#   settings.rng silently breaks it — numpy's module-level functions use a
#   separate global generator we do not control. Nothing errors; the database
#   simply stops being reproducible.
#
# WHAT "BUSINESS DAY" MEANS HERE, AND WHAT IT DOES NOT:
#   Monday to Friday. PUBLIC HOLIDAYS ARE IGNORED.
#
#   That is a deliberate simplification and worth being honest about. The real
#   Indian market closes for about 15 trading holidays a year, so our
#   calendar has roughly 15 extra "trading days" per year that a real NAV feed
#   would not have.
#
#   The reason it is acceptable: nothing in this project depends on the exact
#   number of trading days. What matters is that the SAME calendar is used
#   everywhere, so a transaction date always finds a NAV. Adding a real
#   holiday calendar would mean maintaining a list of dates per year, and
#   would teach nothing the project needs.
#
#   The reason it has to be stated: a student who counts the NAV rows and
#   compares them with a real fund's would find a difference, and should know
#   it is a choice rather than a bug.
#
# HOW TO USE:
#   Every generator file starts the same way:
#
#       from dataplane.generator.common import Settings, business_days
#
#   and run.py creates the one Settings object:
#
#       from dataplane.generator.common import load_settings
#       settings = load_settings()
#
#   Nothing in here is run directly. It has no main().
##############################################################################


from dataclasses import dataclass
# A dataclass is a class that exists only to hold values.
#
# Writing it by hand would mean an __init__ that assigns four fields, plus a
# __repr__ so printing it is readable, plus an __eq__ so two of them can be
# compared. The decorator writes all of that from the field declarations.
#
# We use it for Settings below, because Settings has no behaviour beyond one
# small helper — it is four values travelling together.

from pathlib import Path
# Used to find config/generator.yaml relative to this file rather than
# relative to wherever the terminal happens to be. See PROJECT_ROOT below.

import numpy as np
# numpy does two jobs in this file, and they are unrelated to each other.
#
#   1. THE RANDOM GENERATOR. np.random.default_rng(seed) is the seeded
#      source every random draw in the project comes from.
#
#   2. DATE ARITHMETIC. numpy's datetime64 type can add months properly,
#      skip weekends with is_busday, and roll a date forward to the next
#      working day — all over whole arrays at once, with no Python loop.
#      Python's own datetime module can do none of those three.
#
# The second is the reason the generator can build 170,000 NAV rows in a
# fraction of a second: every date is computed as part of an array operation
# rather than one at a time.

import yaml
# Reads config/generator.yaml into a Python dictionary.
#
# Note we use yaml.safe_load below, never yaml.load. The unsafe version can
# construct arbitrary Python objects from a YAML file, which means a YAML file
# could execute code. safe_load reads only plain data — strings, numbers,
# lists, dictionaries — which is all a config file should ever contain.


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# The project's top folder, worked out from this file's own location.
#
# Reading it left to right:
#   __file__     this file's path — dataplane/generator/common.py
#   Path(...)    as a Path object
#   .resolve()   turned into a full absolute path, with any symbolic links
#                and any ".." segments collapsed
#   .parents[2]  two... no — THREE levels up, because parents is 0-indexed:
#                  parents[0] -> dataplane/generator
#                  parents[1] -> dataplane
#                  parents[2] -> the project root
#
# THE [2] IS THE FRAGILE PART OF THIS LINE. It encodes how deep this file
# sits in the tree. Move common.py one folder deeper and the number must
# change, and nothing will warn you — the next line will simply look for
# config/generator.yaml in the wrong place and fail with "no such file".
#
# Why anchor to __file__ at all rather than writing "config/generator.yaml":
# a relative path is resolved against the TERMINAL's folder, not this file's.
# So it would work when run from the project root and break the moment
# anyone runs it from anywhere else.

CONFIG_FILE = PROJECT_ROOT / "config" / "generator.yaml"
# The config file's full path.
#
# Note the / operator. Path overloads it to join path segments, so this
# reads like a path and works on every operating system — on Windows it
# produces backslashes without being asked.


# ── SETTINGS ─────────────────────────────────────────────────────────────────

@dataclass
class Settings:
    """
    Everything a generator file needs to know about this run.

    Passing one Settings object into every builder is simpler than passing
    seed, scale, start and end separately into each of seven files — and it
    makes it impossible for one file to be given a different seed from
    another, which is the failure this class exists to prevent.

    Attributes:
        rng:   the random number source, created once from the seed. Every
               random draw in the project must come from this object.
        scale: how big the investor tables are. 1.0 is full size, 0.2 is the
               default. Only the investor side scales; see generator.yaml.
        start: first day of the window, as a numpy date.
        end:   last day of the window, as a numpy date.
    """

    rng: np.random.Generator    # the random number source, created from the seed
    scale: float                # how big the investor tables are (see generator.yaml)
    start: np.datetime64        # first day of the window, e.g. 2021-09-01
    end: np.datetime64          # last day of the window, e.g. 2026-08-31
    # These four lines are the whole class definition. The dataclass
    # decorator turns them into an __init__ that takes four keyword
    # arguments, which is how load_settings constructs one below.
    #
    # The type annotations are not enforced by Python at runtime — passing a
    # string as `scale` would not raise here. They are for the reader and for
    # the editor, and ruff checks them.

    def scaled(self, full_size: int) -> int:
        """
        Turn a full-size row count into the count for this run's scale.

        Args:
            full_size: how many rows this table would have at scale 1.0.
                       Always written out in the calling code as a literal,
                       e.g. settings.scaled(200_000), so the full-scale
                       number is visible where it is used.

        Returns:
            The row count for this run. scaled(200_000) returns 40,000 when
            scale is 0.2.

        WHY THIS IS A METHOD AND NOT A MULTIPLICATION AT EACH SITE: because
        of the two things it does beyond multiplying — rounding, and the
        floor of 1. Spread across seven files, those would be forgotten in
        at least one.
        """
        return max(1, round(full_size * self.scale))
        # round() because a count must be a whole number, and
        # 200_000 * 0.15 is 30000.000000000004 in floating point.
        #
        # max(1, ...) IS THE INTERESTING HALF. At a very small scale — 0.001,
        # say, which somebody will try — this would otherwise return 0, and a
        # table with zero rows breaks things far downstream: a join produces
        # nothing, a mean is NaN, and tests/test_stage2_data.py's "every
        # table has rows" check fails with no clue as to why.
        #
        # One row is never useful data, but it keeps every join and every
        # aggregate well-defined, which turns a confusing failure into an
        # obviously-too-small database.


def load_settings() -> Settings:
    """
    Read config/generator.yaml and build the one Settings object for this run.

    Called exactly once, by run.py, at the start of the pipeline. The result
    is then passed down into every builder.

    Returns:
        A Settings object with a freshly seeded random generator.

    Raises:
        FileNotFoundError: if config/generator.yaml is missing — usually
                           means PROJECT_ROOT resolved wrongly.
        KeyError:          if any of seed, scale, start_date or end_date is
                           absent from the YAML.
    """
    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)
        # "with open(...)" closes the file when the block ends, even if
        # reading raises. safe_load turns the YAML text into a plain
        # dictionary: {"seed": 42, "scale": 0.2, ...}

    return Settings(
        rng=np.random.default_rng(config["seed"]),
        # THE LINE THE WHOLE PROJECT'S REPRODUCIBILITY RESTS ON.
        #
        # default_rng is numpy's current recommended generator. Given the
        # same seed it produces the same sequence of numbers, in the same
        # order, forever.
        #
        # WHICH MEANS THE ORDER OF CALLS MATTERS. The 400th random number
        # drawn in the run depends on everything drawn before it. So
        # inserting one extra rng.random() call anywhere in any generator
        # file shifts every subsequent draw, and the whole database changes —
        # different names, different amounts, patterns landing on different
        # rows.
        #
        # That is worth knowing before changing a generator file and
        # wondering why a test that asserted an exact value now fails.
        #
        # The older np.random.seed() approach sets a single global generator
        # shared by the whole process, which anything else in the program can
        # also draw from. default_rng gives us an object we own.
        #
        # config["seed"] with square brackets, not .get() — a missing seed
        # should stop the run here with a clear KeyError, not silently
        # produce a non-reproducible database.

        scale=float(config["scale"]),
        # float() explicitly, because YAML will read "1" as an integer and
        # "1.0" as a float. Without the cast, scaled() would do integer
        # arithmetic in one case and floating point in the other.

        start=np.datetime64(config["start_date"], "D"),
        end=np.datetime64(config["end_date"], "D"),
        # The window, as numpy dates.
        #
        # THE "D" IS NOT OPTIONAL. It pins the unit to DAYS. Without it,
        # numpy infers a unit from the string, and a mismatch between units —
        # days in one place, seconds or microseconds in another — makes date
        # arithmetic quietly wrong by a factor of 86,400 or a million.
        #
        # Every date in this project is datetime64[D], and every function
        # below re-casts to [D] defensively for the same reason.
        #
        # YAML gives these as strings like "2021-09-01", which is exactly
        # what np.datetime64 parses.
    )


# ── DATES ────────────────────────────────────────────────────────────────────

def business_days(start: np.datetime64, end: np.datetime64) -> np.ndarray:
    """
    Every Monday-to-Friday date from start to end, both ends included.

    Used to build the NAV and benchmark calendars: roughly 1,250 dates across
    the five-year window.

    Args:
        start: first date to consider.
        end:   last date to consider — INCLUDED in the result.

    Returns:
        A numpy array of datetime64[D], in ascending order, weekends removed.
    """
    all_days = np.arange(start, end + np.timedelta64(1, "D"), dtype="datetime64[D]")
    # Every calendar day in the range, including weekends.
    #
    # THE "+ 1 DAY" IS WHY end IS INCLUDED. np.arange stops BEFORE its upper
    # bound, exactly like Python's range(). Without it, the last day of the
    # window would be missing from every calendar in the project — and that
    # would be a silent off-by-one: the data would simply end one day early.

    return all_days[np.is_busday(all_days)]
    # Keep only the weekdays.
    #
    # np.is_busday returns a True/False array the same length as its input.
    # Indexing an array with a boolean array keeps the True positions — this
    # is called boolean masking, and it appears constantly in the generator.
    #
    # By default is_busday treats Monday to Friday as business days and takes
    # no holiday list. It accepts a `holidays=` argument we deliberately do
    # not use, per the note in the banner.


def roll_forward(dates: np.ndarray) -> np.ndarray:
    """
    Move any Saturday or Sunday forward to the following Monday. Weekdays are
    left exactly as they are.

    Args:
        dates: an array of dates, any of which may fall on a weekend.

    Returns:
        An array the same length, every date a weekday.

    WHY THIS EXISTS, AND WHY IT IS CALLED EVERYWHERE:
    NAVs exist only on business days. The generator attaches a NAV to every
    transaction with an INNER join on (plan_id, date) — so a transaction
    dated on a Sunday finds no NAV and the row is SILENTLY DROPPED.

    No error, no warning. Just fewer transactions than expected, and a
    student wondering why some months look thin. Every date that will later
    be joined to nav_history passes through here first.
    """
    return np.busday_offset(dates.astype("datetime64[D]"), 0, roll="forward")
    # busday_offset(dates, 0, roll="forward") reads as: move each date by
    # ZERO business days, and if it is not a business day to begin with,
    # roll forward until it is.
    #
    # The 0 is what makes this a "fix the weekend" operation rather than an
    # "add N days" one. With 1 it would move every date to the NEXT business
    # day, including the weekdays — which would shift the whole dataset by a
    # day.
    #
    # roll="forward" versus roll="backward" matters at the edges of the
    # window: forward can push a date past the end, which is why
    # random_dates below has to handle that case explicitly.
    #
    # .astype("datetime64[D]") defensively re-pins the unit, because this
    # function is called with arrays that came from polars, and polars may
    # hand back microseconds depending on version.


def month_ends(start: np.datetime64, end: np.datetime64) -> np.ndarray:
    """
    The last calendar day of every month in the window.

    For a window of 2021-09-01 to 2026-08-31 that is 60 dates:
    2021-09-30, 2021-10-31, 2021-11-30, ... 2026-08-31.

    Args:
        start: any date in the first month wanted.
        end:   any date in the last month wanted.

    Returns:
        One date per month, each the last calendar day of that month.

    NOTE THESE ARE CALENDAR month ends, not trading ones — 31 March is
    returned even when it is a Sunday. scheme_aum_monthly stores exactly
    these dates, and its CHECK constraint enforces that they are calendar
    month ends. The NAV used for that month end is the last TRADING day's,
    which summaries.py looks up separately.
    """
    months = np.arange(
        start.astype("datetime64[M]"),
        end.astype("datetime64[M]") + np.timedelta64(1, "M"),
    )
    # Every month in the range, as datetime64[M] values — 2021-09, 2021-10,
    # and so on. Casting a day to a month throws the day away.
    #
    # The "+ 1 month" is the same inclusive-upper-bound fix as in
    # business_days: arange stops short, so without it the final month would
    # be missing.

    return (months + np.timedelta64(1, "M")).astype("datetime64[D]") - np.timedelta64(1, "D")
    # THE TRICK, AND IT IS WORTH SAYING OUT LOUD ON CAMERA.
    #
    # There is no "last day of this month" operation. So instead:
    #   add one month      2021-09  ->  2021-10
    #   cast to a day      2021-10  ->  2021-10-01  (casting gives the FIRST)
    #   subtract one day   2021-10-01  ->  2021-09-30
    #
    # Which lands on the last day of the ORIGINAL month, with February,
    # 30-day months and leap years all handled by numpy's own calendar rather
    # than by us.
    #
    # The alternative — a dictionary of month lengths plus a leap-year rule —
    # is about fifteen lines, and one of those lines is wrong in a year
    # divisible by 100.


def to_dates(values) -> list:
    """
    Turn a list of numpy dates — some of which may be None or NaT, meaning
    "no date" — into plain Python date objects that polars will accept as a
    proper Date column.

    Args:
        values: a list that may contain numpy datetime64 values, None, and
                NaT in any mixture.

    Returns:
        A plain Python list of datetime.date and None.

    WHY THIS IS NEEDED, AND IT IS NOT OBVIOUS:
    polars can build a Date column from a clean numpy date array, and it can
    build one from a list of Python dates. What it CANNOT do is build one
    from a list that mixes numpy date objects with None.

    Faced with that, polars gives up and stores the column as generic
    "objects" — and an object column cannot be written out as CSV for the
    COPY loader. The failure appears at load time, as a type error about a
    column several stages away from here.

    This function appears wherever a date column is allowed to be empty:
    investors.date_of_birth, sip_registrations.end_date, cancelled_date.
    """
    out = []
    for v in values:
        # A plain Python loop, deliberately. These lists are tens of
        # thousands of entries at most and this runs a handful of times; the
        # clarity is worth more than vectorising it.

        if v is None or np.isnat(np.datetime64(v)):
            # TWO DIFFERENT WAYS A DATE CAN BE ABSENT, and both have to be
            # handled:
            #   None  — Python's missing value, which is what the generator's
            #           blank-one-percent helper inserts
            #   NaT   — "Not a Time", numpy's own missing date, which comes
            #           out of np.where() when one branch has no date
            #
            # np.isnat only works on a numpy date, so None has to be checked
            # FIRST. Python's `or` short-circuits, so np.datetime64(None) is
            # never evaluated for a None — which matters, because that call
            # would itself produce NaT and the check would still work, but by
            # accident rather than by design.
            out.append(None)
        else:
            out.append(np.datetime64(v, "D").item())
            # .item() converts a numpy scalar into its plain Python
            # equivalent — here a datetime.date.
            #
            # The ", D" re-pins the unit first, so a value that arrived as
            # microseconds becomes a date rather than a datetime. Without it,
            # .item() would return a datetime.datetime and polars would build
            # a Datetime column where the schema expects a Date.
    return out


def random_dates(settings: Settings, low: np.ndarray, high: np.ndarray) -> np.ndarray:
    """
    One random business day between low and high, for each position.

    Args:
        settings: used only for its rng, so the draw is reproducible.
        low:      array of earliest allowed dates, one per row wanted.
        high:     array of latest allowed dates, same length as low.

    Returns:
        An array of business days, each inside its own [low, high] range.

    Used wherever a transaction or a folio needs a date "somewhere after this
    and before that" — later purchases, redemptions, switch-outs, and the
    four-week window of pattern 1's redemption spike.

    Both bounds are ARRAYS, not single dates, because each row has its own
    range: a redemption must fall after ITS folio opened, not after the
    earliest folio opened.
    """
    low = low.astype("datetime64[D]")
    high = high.astype("datetime64[D]")
    # Re-pin both to days, for the same reason as everywhere else.

    span = (high - low).astype(int)
    # How many days wide each range is. Subtracting two datetime64[D] values
    # gives a timedelta64[D]; .astype(int) turns that into a plain number of
    # days, which is what the arithmetic below needs.

    offsets = np.floor(settings.rng.random(len(low)) * (span + 1)).astype(int)
    # A random whole number of days to add, from 0 up to and including span.
    #
    # Reading it outwards:
    #   rng.random(n)       n numbers in [0, 1) — zero included, one excluded
    #   * (span + 1)        scaled to [0, span+1)
    #   np.floor(...)       floored to a whole number: 0 .. span
    #
    # THE "+ 1" IS WHY high IS REACHABLE. Without it the range would be
    # [0, span) and the last day would never be drawn. That is the same
    # inclusive-bound issue as in business_days, in a different shape.
    #
    # Note this uses settings.rng, not np.random.*. That is the rule the
    # whole file exists to enforce.

    picked = low + offsets.astype("timedelta64[D]")
    # The raw dates, which may land on weekends.

    forward = np.busday_offset(picked, 0, roll="forward")
    backward = np.busday_offset(picked, 0, roll="backward")
    # BOTH DIRECTIONS ARE COMPUTED FOR EVERY ROW, and only one is used. That
    # is slightly wasteful and entirely vectorised — far cheaper than
    # deciding per row in Python.

    return np.where(forward <= high, forward, backward)
    # Prefer rolling FORWARD; fall back to BACKWARD when forward would
    # overshoot the upper bound.
    #
    # WHY BOTH ARE NEEDED: if `high` itself falls on a Friday and `picked`
    # lands on the Saturday after it, rolling forward gives the following
    # Monday — which is OUTSIDE the allowed range. For a redemption that
    # would mean a transaction dated after the window ends, and the partition
    # for it would not exist, so the load would fail with "no partition of
    # relation transactions found for row".
    #
    # Rolling backward in that case gives the Friday, which is inside the
    # range and is a business day.
    #
    # np.where(condition, a, b) picks elementwise — a where the condition is
    # True, b where it is False. The array equivalent of an if/else.


# ── NAME LISTS ───────────────────────────────────────────────────────────────
#
# Fixed lists, written out rather than generated, so the same seed always
# picks the same names. A library like Faker would produce names too, and the
# list would change when the library updated — and with it every name in the
# database.
#
# ON WHAT THESE NAMES ARE:
#   PEOPLE'S names are common Indian first names and surnames. The
#   COMBINATIONS are drawn at random and belong to nobody in particular —
#   there is no real person behind any row, and the personal columns are
#   masked anyway.
#
#   COMPANY, GROUP and INDEX names are INVENTED, assembled from the word
#   lists at the bottom. That is deliberate: nothing in a demo of a credit
#   downgrade, a concentration breach or a distributor with a bad rejection
#   rate should be mistakable for a real company. Those are reputational
#   claims, and they would be false ones.

FIRST_NAMES = [
    "Aarav", "Aditi", "Aditya", "Akash", "Amit", "Ananya", "Anil", "Anjali", "Arjun",
    "Asha", "Deepak", "Divya", "Gaurav", "Geeta", "Harish", "Isha", "Karan", "Kavita",
    "Kiran", "Lakshmi", "Manish", "Meera", "Mohan", "Neha", "Nikhil", "Pooja", "Pradeep",
    "Priya", "Rahul", "Rajesh", "Ramesh", "Ravi", "Rohan", "Sachin", "Sanjay", "Sarita",
    "Shreya", "Sneha", "Sunil", "Suresh", "Swati", "Tanvi", "Uday", "Varun", "Vikram",
    "Vinod", "Yash", "Zoya", "Farhan", "Imran", "Joseph", "Mary", "Harpreet", "Gurpreet",
]
# 54 first names, spread across regions and communities on purpose — a list
# of only north-Indian Hindu names would make an investor book that does not
# look like India's.
#
# 54 names across 40,000 investors means each appears about 740 times, which
# is why PAN rather than name is the identifier in the investors table.

SURNAMES = [
    "Sharma", "Verma", "Patil", "Deshmukh", "Kulkarni", "Joshi", "Iyer", "Nair", "Menon",
    "Reddy", "Rao", "Naidu", "Gupta", "Agarwal", "Mehta", "Shah", "Desai", "Patel",
    "Chatterjee", "Banerjee", "Das", "Mukherjee", "Singh", "Kaur", "Gill", "Khan",
    "Qureshi", "Fernandes", "D'Souza", "Pillai", "Kamath", "Shetty", "Bhat", "Pandey",
    "Mishra", "Tiwari", "Yadav", "Chauhan", "Rathod", "Jadhav", "More", "Pawar",
]
# 42 surnames. Note "D'Souza" contains an APOSTROPHE, and that is a useful
# accident: it is exactly the character that breaks naive string handling.
# Our loader uses PostgreSQL's COPY with proper CSV quoting, so it is handled
# — but a student who tries to build an INSERT statement by concatenating
# strings will meet it immediately, which is a lesson worth having.
#
# 54 first names x 42 surnames gives 2,268 possible full names for 40,000
# investors, so duplicates are certain. That is correct: real investor tables
# are full of repeated names.

CITIES = [
    ("Mumbai", "Maharashtra", "MH", 1), ("Delhi", "Delhi", "DL", 1),
    ("Bengaluru", "Karnataka", "KA", 1), ("Chennai", "Tamil Nadu", "TN", 1),
    ("Kolkata", "West Bengal", "WB", 1), ("Hyderabad", "Telangana", "TG", 1),
    ("Pune", "Maharashtra", "MH", 1), ("Ahmedabad", "Gujarat", "GJ", 1),
    ("Jaipur", "Rajasthan", "RJ", 2), ("Lucknow", "Uttar Pradesh", "UP", 2),
    ("Nagpur", "Maharashtra", "MH", 2), ("Indore", "Madhya Pradesh", "MP", 2),
    ("Surat", "Gujarat", "GJ", 2), ("Kochi", "Kerala", "KL", 2),
    ("Coimbatore", "Tamil Nadu", "TN", 2), ("Bhopal", "Madhya Pradesh", "MP", 2),
    ("Vadodara", "Gujarat", "GJ", 2), ("Chandigarh", "Chandigarh", "CH", 2),
    ("Nashik", "Maharashtra", "MH", 2), ("Visakhapatnam", "Andhra Pradesh", "AP", 2),
    ("Kolhapur", "Maharashtra", "MH", 3), ("Satara", "Maharashtra", "MH", 3),
    ("Belagavi", "Karnataka", "KA", 3), ("Hubballi", "Karnataka", "KA", 3),
    ("Jalgaon", "Maharashtra", "MH", 3), ("Latur", "Maharashtra", "MH", 3),
    ("Mysuru", "Karnataka", "KA", 3), ("Udaipur", "Rajasthan", "RJ", 3),
    ("Guntur", "Andhra Pradesh", "AP", 3), ("Raipur", "Chhattisgarh", "CG", 3),
    ("Dehradun", "Uttarakhand", "UK", 3), ("Siliguri", "West Bengal", "WB", 3),
]
# 32 cities, each a 4-tuple: (city, state, two-letter state code, tier).
#
# THE POSITIONS MATTER, because the generator indexes this list rather than
# naming fields:
#   CITIES[i][0]  city name     -> investors.city and distributors.city_nm
#   CITIES[i][1]  state name    -> investors.state
#   CITIES[i][2]  state code    -> distributors.st_cd
#   CITIES[i][3]  tier          -> investors.city_tier
#
# Note positions 1 and 2 feed DIFFERENT TABLES. investors stores the full
# state name; distributors stores the two-letter code. The same information
# in two shapes, which is the inconsistency built into 03_investors.sql on
# purpose.
#
# COUNT THE TIERS: 8 tier-1, 12 tier-2, 12 tier-3. Which is why TIER_SHARE
# below exists and why the generator picks a TIER FIRST and a city second.
# Drawing a city uniformly from this list would make 37.5% of investors
# tier-3, against the 20% we intend — and pattern 4, which lives in tier 3,
# would sit in a much larger population than planned.

TIER_SHARE = {1: 0.50, 2: 0.30, 3: 0.20}
# How investors are spread across tiers: half in the big metros, a fifth in
# smaller towns. Roughly the real shape of Indian mutual fund ownership,
# which is heavily metro-weighted.
#
# PATTERN 4 DEPENDS ON THE 0.20. Tier 3 has to be a meaningful minority: big
# enough that a rise in cancellations there is statistically visible, small
# enough that it does not dominate the book. At 0.02 the pattern would be
# noise; at 0.50 it would not be a finding about a segment.
#
# These three must sum to 1.0 — numpy's rng.choice raises if its p= argument
# does not, which is the behaviour we want rather than a silently normalised
# typo.

NAME_WORDS = [
    "Ashoka", "Trident", "Vardhaman", "Kaveri", "Narmada", "Godavari", "Himgiri", "Vindhya",
    "Satpura", "Aravalli", "Konark", "Pratap", "Shivalik", "Nilgiri", "Malabar", "Coromandel",
    "Konkan", "Sindhu", "Yamuna", "Saraswati", "Chambal", "Tapti", "Mahanadi", "Tungabhadra",
    "Sabarmati", "Teesta", "Orion", "Pinnacle", "Sterling", "Apex", "Zenith", "Horizon",
    "Summit", "Crest", "Meridian", "Pioneer", "Suryoday", "Chandra", "Arunodaya", "Vasundhara",
    "Prakriti", "Nakshatra", "Samudra", "Parvat", "Ganesha", "Lotus", "Banyan", "Peacock",
    "Tiger", "Falcon", "Eagle", "Anchor", "Beacon", "Compass", "Harbour", "Keystone",
    "Lighthouse", "Monarch", "Navratna", "Omkar", "Rudra", "Shakti", "Tejas", "Utkarsh",
    "Vikas", "Yashoda", "Amrit", "Bharati", "Dhruva", "Gagan", "Indra", "Jyoti",
]
# 72 first words for invented company names — rivers, mountain ranges,
# qualities, and some neutral English words. Drawn from Indian geography and
# from the vocabulary Indian companies actually use, so the results read as
# plausible Indian companies.
#
# The ones that matter: "Vardhaman" and "Konkan" appear in the planted
# patterns, and tests/test_stage5_metadata.py looks up "Vardhaman Housing
# Finance Ltd" by name. Removing a word from this list could therefore break
# a test — the lists are data, but they are data something depends on.

SECOND_WORDS = [
    "Industries", "Enterprises", "Holdings", "Technologies", "Infra", "Power", "Chemicals",
    "Steel", "Textiles", "Motors", "Foods", "Pharma", "Energy", "Cement", "Logistics",
    "Realty", "Telecom", "Systems", "Polymers", "Agro", "Electricals", "Engineering",
]
# 22 second words, each suggesting an industry. market.py pairs a NAME_WORD
# with a SECOND_WORD to build an issuer name — "Tungabhadra Polymers",
# "Keystone Logistics" — giving 72 x 22 = 1,584 possible names for about 800
# issuers.
#
# Note the second word only SUGGESTS the industry; the issuer's actual
# sector_id is drawn separately. So "Nilgiri Steel" can be classified under
# Chemicals, which looks like a mistake and is in fact realistic — company
# names rarely match their current business after a few decades of
# diversification. It is also a small reminder that a name is not data: the
# sector column is, and a query must use it rather than parsing the name.