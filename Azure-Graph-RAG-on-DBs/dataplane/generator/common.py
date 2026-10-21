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

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

# The project's top folder. This file is at dataplane/generator/common.py,
# so going up three levels (generator -> dataplane -> project) reaches it.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "config" / "generator.yaml"


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
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

    def scaled(self, full_size: int) -> int:
        """
        Turn a full-size row count into the count for this run's scale.
        Example: scaled(200_000) is 40_000 when scale is 0.2.
        max(1, ...) makes sure we never ask for zero rows.
        """
        return max(1, round(full_size * self.scale))


def load_settings() -> Settings:
    """Read config/generator.yaml and build a Settings object from it."""
    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    return Settings(
        # default_rng(seed) creates numpy's recommended random generator.
        # Every random number in the whole run comes from this one object,
        # in a fixed order, so the same seed gives the same database.
        rng=np.random.default_rng(config["seed"]),
        scale=float(config["scale"]),
        # "datetime64[D]" is numpy's date type, precise to one day.
        start=np.datetime64(config["start_date"], "D"),
        end=np.datetime64(config["end_date"], "D"),
    )


# ---------------------------------------------------------------------------
# DATES
# ---------------------------------------------------------------------------
def business_days(start: np.datetime64, end: np.datetime64) -> np.ndarray:
    """
    Every Monday-to-Friday date from start to end, both included.

    np.arange(start, end + 1) gives every calendar day. np.is_busday()
    returns True for Monday to Friday, and we keep only those.
    """
    all_days = np.arange(start, end + np.timedelta64(1, "D"), dtype="datetime64[D]")
    return all_days[np.is_busday(all_days)]


def roll_forward(dates: np.ndarray) -> np.ndarray:
    """
    Move any Saturday or Sunday forward to the following Monday.
    Business days are left unchanged. Used so every transaction date has a NAV.
    """
    return np.busday_offset(dates.astype("datetime64[D]"), 0, roll="forward")


def month_ends(start: np.datetime64, end: np.datetime64) -> np.ndarray:
    """
    The last calendar day of every month in the window,
    e.g. 2021-09-30, 2021-10-31, ... 2026-08-31.

    How it works: datetime64[M] counts in whole months. Adding 1 month to a
    month and going back 1 day lands on the last day of the original month.
    """
    months = np.arange(
        start.astype("datetime64[M]"),
        end.astype("datetime64[M]") + np.timedelta64(1, "M"),
    )
    return (months + np.timedelta64(1, "M")).astype("datetime64[D]") - np.timedelta64(1, "D")


def to_dates(values) -> list:
    """
    Turn a list of numpy dates (some may be None or "NaT", meaning no date)
    into plain Python dates, which polars stores as a proper Date column.

    Why this is needed: polars cannot build a Date column directly from a
    list that mixes numpy date objects with None. It would store them as
    generic "objects", which cannot be written out as CSV for loading.
    """
    out = []
    for v in values:
        if v is None or np.isnat(np.datetime64(v)):
            out.append(None)
        else:
            # .item() turns a numpy date into a Python datetime.date.
            out.append(np.datetime64(v, "D").item())
    return out


def random_dates(settings: Settings, low: np.ndarray, high: np.ndarray) -> np.ndarray:
    """
    One random business day between low and high for each position.

    low and high are arrays of the same length. For each pair, a random
    whole number of days is added to low, then the result is moved forward
    to a business day. Where the move would pass high, we move backward
    instead, so the result always stays inside the range.
    """
    low = low.astype("datetime64[D]")
    high = high.astype("datetime64[D]")
    span = (high - low).astype(int)
    # rng.random() gives a number from 0 up to (not including) 1.
    # Multiplying by (span + 1) and taking the floor gives 0 .. span.
    offsets = np.floor(settings.rng.random(len(low)) * (span + 1)).astype(int)
    picked = low + offsets.astype("timedelta64[D]")
    forward = np.busday_offset(picked, 0, roll="forward")
    backward = np.busday_offset(picked, 0, roll="backward")
    return np.where(forward <= high, forward, backward)


# ---------------------------------------------------------------------------
# NAME LISTS
# ---------------------------------------------------------------------------
# Fixed lists, so the same seed always picks the same names.
# People's names are common Indian names; the combinations are random and
# belong to nobody in particular. Company, group and index names are
# invented, so nothing can be mistaken for a real company.

FIRST_NAMES = [
    "Aarav", "Aditi", "Aditya", "Akash", "Amit", "Ananya", "Anil", "Anjali", "Arjun",
    "Asha", "Deepak", "Divya", "Gaurav", "Geeta", "Harish", "Isha", "Karan", "Kavita",
    "Kiran", "Lakshmi", "Manish", "Meera", "Mohan", "Neha", "Nikhil", "Pooja", "Pradeep",
    "Priya", "Rahul", "Rajesh", "Ramesh", "Ravi", "Rohan", "Sachin", "Sanjay", "Sarita",
    "Shreya", "Sneha", "Sunil", "Suresh", "Swati", "Tanvi", "Uday", "Varun", "Vikram",
    "Vinod", "Yash", "Zoya", "Farhan", "Imran", "Joseph", "Mary", "Harpreet", "Gurpreet",
]

SURNAMES = [
    "Sharma", "Verma", "Patil", "Deshmukh", "Kulkarni", "Joshi", "Iyer", "Nair", "Menon",
    "Reddy", "Rao", "Naidu", "Gupta", "Agarwal", "Mehta", "Shah", "Desai", "Patel",
    "Chatterjee", "Banerjee", "Das", "Mukherjee", "Singh", "Kaur", "Gill", "Khan",
    "Qureshi", "Fernandes", "D'Souza", "Pillai", "Kamath", "Shetty", "Bhat", "Pandey",
    "Mishra", "Tiwari", "Yadav", "Chauhan", "Rathod", "Jadhav", "More", "Pawar",
]

# (city, state, two-letter state code, city tier)
# Tier 1 = large metro, 2 = mid-size city, 3 = smaller town.
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

# How investors are spread across tiers: 50% tier 1, 30% tier 2, 20% tier 3.
TIER_SHARE = {1: 0.50, 2: 0.30, 3: 0.20}

# Words used to build invented company and group names.
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

SECOND_WORDS = [
    "Industries", "Enterprises", "Holdings", "Technologies", "Infra", "Power", "Chemicals",
    "Steel", "Textiles", "Motors", "Foods", "Pharma", "Energy", "Cement", "Logistics",
    "Realty", "Telecom", "Systems", "Polymers", "Agro", "Electricals", "Engineering",
]