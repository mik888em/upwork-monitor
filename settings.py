"""
Configuration for the Windows US-only Upwork monitor.

The goal is to surface small/simple jobs rather than one specific
profession.
"""

# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------

SEARCH_URLS = [
    "https://www.upwork.com/nx/search/jobs/?location=United%2520States&sort=recency",
]

# Individual job pages must confirm this exact client country.
REQUIRED_CLIENT_COUNTRY = "United States"

SEARCH_READY_TIMEOUT_SECONDS = 30

# ----------------------------------------------------------------------
# Simple-job filter
# ----------------------------------------------------------------------

MIN_SIMPLE_SCORE = 5

# These are sufficiently specific that a match can qualify by itself.
STRONG_SIMPLE_KEYWORDS = (
    # Data / research
    "data entry",
    "copy paste",
    "copy/paste",
    "web research",
    "internet research",
    "online research",
    "data collection",
    "list building",
    "data cleanup",
    "data cleaning",
    "spreadsheet cleanup",
    "excel cleanup",
    "csv cleanup",
    "pdf to excel",
    "pdf to word",
    "file conversion",
    "product listing",
    "content upload",

    # Google ecosystem
    "google maps",
    "google map",
    "google sheets",
    "google sheet",
    "google spreadsheet",
    "google forms",
    "google form",
    "google apps script",
    "apps script",

    # Testing
    "manual testing",
    "website testing",
    "app testing",
    "application testing",
    "user testing",
    "usability testing",
    "qa testing",
    "website feedback",
    "test website",
    "test our website",
    "test our app",

    # Image / photo
    "photo retouch",
    "photo retouching",
    "image retouch",
    "image retouching",
    "background removal",
    "remove background",
    "image resize",
    "resize images",
    "crop images",

    # Greece / local tasks
    "greece",
    "greek",
    "athens",
    "thessaloniki",
    "crete",
    "local task",
    "local research",
    "store visit",
    "take photos",
    "photo collection",
    "location verification",
    "address verification",
    "map verification",
    "mystery shopper",
)

# These are broad. A broad match requires a SIMPLE_SIGNALS match too.
BROAD_TOPIC_KEYWORDS = (
    "python",
    "python script",
    "automation",
    "script",
    "testing",
    "qa",
    "excel",
    "spreadsheet",
    "csv",
    "web scraping",
    "scraping",
    "data scraping",
    "photoshop",
    "retouch",
)

# Signals that the client describes the work as small / quick.
SIMPLE_SIGNALS = (
    "simple",
    "quick",
    "easy",
    "small task",
    "small job",
    "small project",
    "minor",
    "quick fix",
    "small fix",
    "minor fix",
    "tiny task",
    "micro task",
    "one-time",
    "one time",
    "one-off",
    "one off",
    "short task",
    "few minutes",
    "few mins",
    "5 minutes",
    "10 minutes",
    "15 minutes",
    "basic",
    "straightforward",
)

# Any of these rejects the job even if a positive keyword is present.
# The target is short, easy work — not a long software engagement.
COMPLEXITY_SIGNALS = (
    "senior",
    "lead developer",
    "software architect",
    "solution architect",
    "full stack",
    "full-stack",
    "enterprise",
    "large project",
    "complex project",
    "long-term",
    "long term",
    "ongoing",
    "full-time",
    "full time",
    "30+ hrs/week",
    "more than 30 hrs/week",
    "more than 6 months",
    "3 to 6 months",
    "3-6 months",
    "6+ months",
    "machine learning",
    "deep learning",
    "devops",
    "kubernetes",
    "microservices",
    "saas platform",
    "production system",
    "team of developers",
)

# Reserved for exact phrases we may decide to ban later.
EXCLUDE_KEYWORDS = (
)

COUNTRY_FLAGS = {
    "United States": "🇺🇸",
}
