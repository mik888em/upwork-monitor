"""
Windows configuration for Upwork Monitor.
"""

# One search only:
# client location = United States
# newest jobs first.
SEARCH_URLS = [
    "https://www.upwork.com/nx/search/jobs/?location=United%2520States&sort=recency",
]

# Do not apply the original author's country exclusions.
SKIP_COUNTRIES = set()

# Do not apply the original author's $1000 fixed-budget floor.
MIN_FIXED_BUDGET = 0

COUNTRY_FLAGS = {
    "United States": "🇺🇸",
}
