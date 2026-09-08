"""
Configuration for the Windows US-only Upwork monitor.

Goal:
surface small/simple jobs across multiple unrelated categories rather
than jobs from one profession.
"""

# ======================================================================
# Search
# ======================================================================

SEARCH_URLS = [
    "https://www.upwork.com/nx/search/jobs/?location=United%2520States&sort=recency",
]

# Individual job page must confirm this exact client country.
REQUIRED_CLIENT_COUNTRY = "United States"

SEARCH_READY_TIMEOUT_SECONDS = 30

# ======================================================================
# Simple-job scoring
# ======================================================================

MIN_SIMPLE_SCORE = 5


# ======================================================================
# Strong simple-job subjects
#
# Any match here contributes +5 and can qualify the job by itself,
# unless a hard complexity signal rejects it.
# ======================================================================

STRONG_SIMPLE_KEYWORDS = (

    # ------------------------------------------------------------------
    # Data entry / research / lists
    # ------------------------------------------------------------------

    "data entry",
    "copy paste",
    "copy/paste",
    "typing job",
    "typing task",
    "web research",
    "internet research",
    "online research",
    "google search",
    "data collection",
    "data gathering",
    "information gathering",
    "list building",
    "lead list",
    "business list",
    "contact list",
    "company list",
    "email list",
    "business research",
    "product research",
    "market research",
    "data cleanup",
    "data cleaning",
    "spreadsheet cleanup",
    "excel cleanup",
    "csv cleanup",
    "pdf to excel",
    "pdf to word",
    "pdf conversion",
    "file conversion",
    "document formatting",
    "product listing",
    "product data entry",
    "content upload",

    # ------------------------------------------------------------------
    # Google ecosystem
    # ------------------------------------------------------------------

    "google maps",
    "google map",
    "google maps research",
    "google sheets",
    "google sheet",
    "google spreadsheet",
    "google forms",
    "google form",
    "google apps script",
    "apps script",

    # ------------------------------------------------------------------
    # Testing
    # ------------------------------------------------------------------

    "manual testing",
    "website testing",
    "web testing",
    "app testing",
    "application testing",
    "user testing",
    "usability testing",
    "qa testing",
    "website feedback",
    "app feedback",
    "test website",
    "test our website",
    "test our app",
    "test a website",
    "bug testing",

    # ------------------------------------------------------------------
    # Annotation / labeling
    # ------------------------------------------------------------------

    "cvat",
    "data annotation",
    "image annotation",
    "image annotator",
    "data labeling",
    "image labeling",
    "image labelling",
    "image tagging",
    "photo annotation",
    "bounding box",
    "bounding boxes",
    "polygon annotation",
    "object annotation",
    "object labeling",
    "object labelling",
    "label images",
    "labeling images",
    "labelling images",
    "annotation task",
    "annotation job",

    # ------------------------------------------------------------------
    # Transcription / captions
    # ------------------------------------------------------------------

    "transcription",
    "audio transcription",
    "video transcription",
    "transcribe audio",
    "transcribe video",
    "captioning",
    "caption task",
    "subtitle creation",
    "create subtitles",

    # ------------------------------------------------------------------
    # Image / photo
    # ------------------------------------------------------------------

    "photo retouch",
    "photo retouching",
    "image retouch",
    "image retouching",
    "background removal",
    "remove background",
    "remove backgrounds",
    "image resize",
    "resize images",
    "resize photos",
    "crop images",
    "crop photos",
    "basic photoshop",

    # ------------------------------------------------------------------
    # CMS / content
    # ------------------------------------------------------------------

    "wordpress content upload",
    "wordpress data entry",
    "shopify product upload",
    "shopify product entry",
    "woocommerce product upload",

    # ------------------------------------------------------------------
    # Greece / local tasks
    # ------------------------------------------------------------------

    "greece",
    "greek",
    "athens",
    "thessaloniki",
    "crete",
    "local task",
    "local research",
    "store visit",
    "shop visit",
    "take photos",
    "take pictures",
    "photo collection",
    "location verification",
    "address verification",
    "map verification",
    "business verification",
    "mystery shopper",
)


# ======================================================================
# Broad technical subjects
#
# These contribute only +2.
# They need a SIMPLE_SIGNALS match (+3) to reach score 5.
#
# Example:
#
#   "Python developer"             -> reject
#   "Quick Python script"          -> accept
# ======================================================================

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
    "javascript",
    "api",
)


# ======================================================================
# Explicit small/quick-work language
# ======================================================================

SIMPLE_SIGNALS = (
    "simple",
    "quick",
    "easy",
    "easy task",
    "simple task",
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
    "short job",
    "few minutes",
    "few mins",
    "5 minutes",
    "10 minutes",
    "15 minutes",
    "30 minutes",
    "less than an hour",
    "under an hour",
    "basic",
    "straightforward",
    "very straightforward",
    "should be quick",
    "quick job",
    "quick task",
)


# ======================================================================
# Hard complexity / long engagement signals
#
# Any of these rejects the job.
#
# IMPORTANT:
# "machine learning" and "deep learning" are intentionally NOT here.
# A simple image-labeling/CVAT task may support an ML project without
# being technically difficult itself.
# ======================================================================

COMPLEXITY_SIGNALS = (
    "senior",
    "lead developer",
    "principal engineer",
    "staff engineer",
    "software architect",
    "solution architect",
    "system architect",
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
    "devops",
    "kubernetes",
    "microservices",
    "saas platform",
    "production system",
    "team of developers",
    "enterprise application",
    "enterprise system",
)


# ======================================================================
# Explicit exclusions
#
# Keep empty for now.
# We can populate this later from real false-positive alerts.
# ======================================================================

EXCLUDE_KEYWORDS = (
)


COUNTRY_FLAGS = {
    "United States": "🇺🇸",
}
