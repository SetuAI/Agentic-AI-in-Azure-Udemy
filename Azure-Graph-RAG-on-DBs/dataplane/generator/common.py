"""
dataplane/generator/common.py
=============================

WHAT THIS FILE IS FOR
---------------------
Small helpers that every generator file uses:

    - Settings: reads config/generator.yaml (seed, scale, date window).
    - Dates:    business days, month ends, "move to the next business day".
    - Names:    fixed lists of first names, surnames, cities and words,
                used to build fictitious people and companies.

Keeping these in one place means every generator file uses the same seed,
the same calendar and the same city list, so the tables agree with each
other.

WHY "BUSINESS DAYS"
-------------------
NAVs and index values are only published on working days. We treat
Monday to Friday as working days and ignore public holidays, which keeps
the code simple. Every transaction is dated on a business day, so a NAV
always exists for its date.
"""

# "dataclass" lets us write a small class that just holds values (see Settings below).
from dataclasses import dataclass

# "Path" builds file paths that work on Windows, Mac and Linux alike.
from pathlib import Path

# numpy: fast arrays of numbers and dates; "np" is the usual short name.
import numpy as np

# yaml: reads the config/generator.yaml settings file.
import yaml

# The project's top folder. This file is at dataplane/generator/common.py,
# so going up three levels (generator -> dataplane -> project) reaches it.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# The full path of the settings file. "/" joins folder and file names.
CONFIG_FILE = PROJECT_ROOT / "config" / "generator.yaml"


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
# @dataclass automatically writes the __init__ method for us, so we can call
# Settings(rng=..., scale=..., start=..., end=...).
@dataclass
class Settings:
    """
    Everything a generator file needs to know about this run.

    A dataclass is a class that only holds values. Passing one Settings
    object around is simpler than passing seed, scale, start and end
    separately into every function.
    """

    rng: np.random.Generator    # the random number source, created from the seed
    scale: float                # how big the investor tables are (see generator.yaml)
    start: np.datetime64        # first day of the window, e.g. 2021-09-01
    end: np.datetime64          # last day of the window, e.g. 2026-08-31

    # A method (a function that belongs to Settings). "self" is the Settings object.
    def scaled(self, full_size: int) -> int:
        """
        Turn a full-size row count into the count for this run's scale.
        Example: scaled(200_000) is 40_000 when scale is 0.2.
        max(1, ...) makes sure we never ask for zero rows.
        """
        # Multiply by the scale, round to a whole number, never less than 1.
        return max(1, round(full_size * self.scale))


# Called once at the start of every run (see run.py).
def load_settings() -> Settings:
    """Read config/generator.yaml and build a Settings object from it."""
    # Open the YAML file; "with" closes it automatically afterwards.
    with open(CONFIG_FILE) as f:
        # Turn the YAML text into a Python dict, e.g. {"seed": 42, "scale": 0.2, ...}.
        config = yaml.safe_load(f)

    # Build and return the Settings object from the values just read.
    return Settings(
        # default_rng(seed) creates numpy's recommended random generator.
        # Every random number in the whole run comes from this one object,
        # in a fixed order, so the same seed gives the same database.
        rng=np.random.default_rng(config["seed"]),
        # float() makes sure the scale is a decimal number, even if written as "1".
        scale=float(config["scale"]),
        # "datetime64[D]" is numpy's date type, precise to one day.
        start=np.datetime64(config["start_date"], "D"),
        # The last day of the window, as a numpy date.
        end=np.datetime64(config["end_date"], "D"),
    )


# ---------------------------------------------------------------------------
# DATES
# ---------------------------------------------------------------------------
# Returns an array of dates (np.ndarray = a numpy array).
def business_days(start: np.datetime64, end: np.datetime64) -> np.ndarray:
    """
    Every Monday-to-Friday date from start to end, both included.

    np.arange(start, end + 1) gives every calendar day. np.is_busday()
    returns True for Monday to Friday, and we keep only those.
    """
    # Every calendar day. "+ 1 day" because arange stops BEFORE its end value.
    all_days = np.arange(start, end + np.timedelta64(1, "D"), dtype="datetime64[D]")
    # Keep only the days where is_busday is True (Monday to Friday).
    return all_days[np.is_busday(all_days)]


# Helper: weekend dates -> the next Monday.
def roll_forward(dates: np.ndarray) -> np.ndarray:
    """
    Move any Saturday or Sunday forward to the following Monday.
    Business days are left unchanged. Used so every transaction date has a NAV.
    """
    # busday_offset(..., 0, roll="forward"): move 0 business days, but first roll
    # a weekend date forward to Monday.
    return np.busday_offset(dates.astype("datetime64[D]"), 0, roll="forward")


# Helper: the list of month-end dates in a window.
def month_ends(start: np.datetime64, end: np.datetime64) -> np.ndarray:
    """
    The last calendar day of every month in the window,
    e.g. 2021-09-30, 2021-10-31, ... 2026-08-31.

    How it works: datetime64[M] counts in whole months. Adding 1 month to a
    month and going back 1 day lands on the last day of the original month.
    """
    # Every month in the window, e.g. 2021-09, 2021-10, ..., 2026-08.
    months = np.arange(
        # From the start month ...
        start.astype("datetime64[M]"),
        # ... up to and including the end month (arange stops before its end, hence + 1 month).
        end.astype("datetime64[M]") + np.timedelta64(1, "M"),
    )
    # Next month's first day minus one day = this month's last day.
    return (months + np.timedelta64(1, "M")).astype("datetime64[D]") - np.timedelta64(1, "D")


# Helper: numpy dates -> Python dates (None stays None).
def to_dates(values) -> list:
    """
    Turn a list of numpy dates (some may be None or "NaT", meaning no date)
    into plain Python dates, which polars stores as a proper Date column.

    Why this is needed: polars cannot build a Date column directly from a
    list that mixes numpy date objects with None. It would store them as
    generic "objects", which cannot be written out as CSV for loading.
    """
    # The result list we fill in below.
    out = []
    # Look at each value one by one.
    for v in values:
        # np.isnat = "is Not-a-Time" (numpy's empty date). Empty -> store None.
        if v is None or np.isnat(np.datetime64(v)):
            # None becomes an empty (NULL) cell in the table.
            out.append(None)
        # Otherwise it is a real date.
        else:
            # .item() turns a numpy date into a Python datetime.date.
            out.append(np.datetime64(v, "D").item())
    # Give back the list of Python dates (and Nones).
    return out


# Helper: random business days within given ranges.
def random_dates(settings: Settings, low: np.ndarray, high: np.ndarray) -> np.ndarray:
    """
    One random business day between low and high for each position.

    low and high are arrays of the same length. For each pair, a random
    whole number of days is added to low, then the result is moved forward
    to a business day. Where the move would pass high, we move backward
    instead, so the result always stays inside the range.
    """
    # Make sure both arrays hold whole-day dates.
    low = low.astype("datetime64[D]")
    # (same for the high dates)
    high = high.astype("datetime64[D]")
    # How many days lie between low and high, for every position.
    span = (high - low).astype(int)
    # rng.random() gives a number from 0 up to (not including) 1.
    # Multiplying by (span + 1) and taking the floor gives 0 .. span.
    offsets = np.floor(settings.rng.random(len(low)) * (span + 1)).astype(int)
    # Add the random number of days to each low date.
    picked = low + offsets.astype("timedelta64[D]")
    # The picked date moved forward to a business day (weekend -> Monday).
    forward = np.busday_offset(picked, 0, roll="forward")
    # The picked date moved backward to a business day (weekend -> Friday).
    backward = np.busday_offset(picked, 0, roll="backward")
    # np.where(condition, a, b): take "forward" where it stays inside the range,
    # otherwise take "backward".
    return np.where(forward <= high, forward, backward)


# ---------------------------------------------------------------------------
# NAME LISTS
# ---------------------------------------------------------------------------
# Fixed lists, so the same seed always picks the same names.
# People's names are common Indian names; the combinations are random and
# belong to nobody in particular. Company, group and index names are
# invented, so nothing can be mistaken for a real company.

# First names used to build fictitious investor names.
FIRST_NAMES = [
    # First names A (Aarav ... Arjun)
    "Aarav", "Aditi", "Aditya", "Akash", "Amit", "Ananya", "Anil", "Anjali", "Arjun",
    # First names A-K (Asha ... Kavita)
    "Asha", "Deepak", "Divya", "Gaurav", "Geeta", "Harish", "Isha", "Karan", "Kavita",
    # First names K-P (Kiran ... Pradeep)
    "Kiran", "Lakshmi", "Manish", "Meera", "Mohan", "Neha", "Nikhil", "Pooja", "Pradeep",
    # First names P-S (Priya ... Sarita)
    "Priya", "Rahul", "Rajesh", "Ramesh", "Ravi", "Rohan", "Sachin", "Sanjay", "Sarita",
    # First names S-V (Shreya ... Vikram)
    "Shreya", "Sneha", "Sunil", "Suresh", "Swati", "Tanvi", "Uday", "Varun", "Vikram",
    # First names V-Z, plus names from other communities (Farhan ... Gurpreet)
    "Vinod", "Yash", "Zoya", "Farhan", "Imran", "Joseph", "Mary", "Harpreet", "Gurpreet",
]

# Surnames used to build fictitious investor and manager names.
SURNAMES = [
    # Surnames from north and west India, and the south (Iyer, Nair, Menon)
    "Sharma", "Verma", "Patil", "Deshmukh", "Kulkarni", "Joshi", "Iyer", "Nair", "Menon",
    # Surnames from the south and west
    "Reddy", "Rao", "Naidu", "Gupta", "Agarwal", "Mehta", "Shah", "Desai", "Patel",
    # Surnames from the east and north
    "Chatterjee", "Banerjee", "Das", "Mukherjee", "Singh", "Kaur", "Gill", "Khan",
    # Surnames from various communities and the coast
    "Qureshi", "Fernandes", "D'Souza", "Pillai", "Kamath", "Shetty", "Bhat", "Pandey",
    # Surnames from the north and Maharashtra
    "Mishra", "Tiwari", "Yadav", "Chauhan", "Rathod", "Jadhav", "More", "Pawar",
]

# (city, state, two-letter state code, city tier)
# Tier 1 = large metro, 2 = mid-size city, 3 = smaller town.
# Each entry is one tuple: (city, state, state code, tier).
CITIES = [
    # Tier 1 (large metros): Mumbai, Delhi
    ("Mumbai", "Maharashtra", "MH", 1), ("Delhi", "Delhi", "DL", 1),
    # Tier 1: Bengaluru, Chennai
    ("Bengaluru", "Karnataka", "KA", 1), ("Chennai", "Tamil Nadu", "TN", 1),
    # Tier 1: Kolkata, Hyderabad
    ("Kolkata", "West Bengal", "WB", 1), ("Hyderabad", "Telangana", "TG", 1),
    # Tier 1: Pune, Ahmedabad
    ("Pune", "Maharashtra", "MH", 1), ("Ahmedabad", "Gujarat", "GJ", 1),
    # Tier 2 (mid-size cities): Jaipur, Lucknow
    ("Jaipur", "Rajasthan", "RJ", 2), ("Lucknow", "Uttar Pradesh", "UP", 2),
    # Tier 2: Nagpur, Indore
    ("Nagpur", "Maharashtra", "MH", 2), ("Indore", "Madhya Pradesh", "MP", 2),
    # Tier 2: Surat, Kochi
    ("Surat", "Gujarat", "GJ", 2), ("Kochi", "Kerala", "KL", 2),
    # Tier 2: Coimbatore, Bhopal
    ("Coimbatore", "Tamil Nadu", "TN", 2), ("Bhopal", "Madhya Pradesh", "MP", 2),
    # Tier 2: Vadodara, Chandigarh
    ("Vadodara", "Gujarat", "GJ", 2), ("Chandigarh", "Chandigarh", "CH", 2),
    # Tier 2: Nashik, Visakhapatnam
    ("Nashik", "Maharashtra", "MH", 2), ("Visakhapatnam", "Andhra Pradesh", "AP", 2),
    # Tier 3 (smaller towns): Kolhapur, Satara
    ("Kolhapur", "Maharashtra", "MH", 3), ("Satara", "Maharashtra", "MH", 3),
    # Tier 3: Belagavi, Hubballi
    ("Belagavi", "Karnataka", "KA", 3), ("Hubballi", "Karnataka", "KA", 3),
    # Tier 3: Jalgaon, Latur
    ("Jalgaon", "Maharashtra", "MH", 3), ("Latur", "Maharashtra", "MH", 3),
    # Tier 3: Mysuru, Udaipur
    ("Mysuru", "Karnataka", "KA", 3), ("Udaipur", "Rajasthan", "RJ", 3),
    # Tier 3: Guntur, Raipur
    ("Guntur", "Andhra Pradesh", "AP", 3), ("Raipur", "Chhattisgarh", "CG", 3),
    # Tier 3: Dehradun, Siliguri
    ("Dehradun", "Uttarakhand", "UK", 3), ("Siliguri", "West Bengal", "WB", 3),
]

# How investors are spread across tiers: 50% tier 1, 30% tier 2, 20% tier 3.
TIER_SHARE = {1: 0.50, 2: 0.30, 3: 0.20}

# Words used to build invented company and group names.
# First words of invented names, e.g. "Kaveri" in "Kaveri Power".
NAME_WORDS = [
    # Words from Indian history and rivers
    "Ashoka", "Trident", "Vardhaman", "Kaveri", "Narmada", "Godavari", "Himgiri", "Vindhya",
    # Mountain ranges and regions
    "Satpura", "Aravalli", "Konark", "Pratap", "Shivalik", "Nilgiri", "Malabar", "Coromandel",
    # Regions and rivers
    "Konkan", "Sindhu", "Yamuna", "Saraswati", "Chambal", "Tapti", "Mahanadi", "Tungabhadra",
    # Rivers and business-style words
    "Sabarmati", "Teesta", "Orion", "Pinnacle", "Sterling", "Apex", "Zenith", "Horizon",
    # Business-style and Sanskrit words
    "Summit", "Crest", "Meridian", "Pioneer", "Suryoday", "Chandra", "Arunodaya", "Vasundhara",
    # Sanskrit words and symbols
    "Prakriti", "Nakshatra", "Samudra", "Parvat", "Ganesha", "Lotus", "Banyan", "Peacock",
    # Animals and objects
    "Tiger", "Falcon", "Eagle", "Anchor", "Beacon", "Compass", "Harbour", "Keystone",
    # Objects and Sanskrit words
    "Lighthouse", "Monarch", "Navratna", "Omkar", "Rudra", "Shakti", "Tejas", "Utkarsh",
    # More Sanskrit words
    "Vikas", "Yashoda", "Amrit", "Bharati", "Dhruva", "Gagan", "Indra", "Jyoti",
]

# Second words of invented company names, e.g. "Power" in "Kaveri Power".
SECOND_WORDS = [
    # Generic company-type words
    "Industries", "Enterprises", "Holdings", "Technologies", "Infra", "Power", "Chemicals",
    # Industry words
    "Steel", "Textiles", "Motors", "Foods", "Pharma", "Energy", "Cement", "Logistics",
    # More industry words
    "Realty", "Telecom", "Systems", "Polymers", "Agro", "Electricals", "Engineering",
]