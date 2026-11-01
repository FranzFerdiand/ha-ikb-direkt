"""Constants for the IKB Direkt Smart Meter integration."""

DOMAIN = "ikb_direkt"
MANUFACTURER = "Innsbrucker Kommunalbetriebe (IKB)"

# Configuration keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_METER_ID = "meter_id"
CONF_HISTORICAL_DAYS = "historical_days"

# Defaults
DEFAULT_SCAN_INTERVAL = 3600  # Fetch new data every hour (grid data typically arrives with some delay)
DEFAULT_HISTORICAL_DAYS = 30  # Initial import of last 30 days of 15-min data

# Portal URLs
PORTAL_BASE_URL = "https://direkt.ikb.at"
PORTAL_LOGIN_URL = f"{PORTAL_BASE_URL}/index.php?page=login&action=login&ajax=y"
PORTAL_LOADPROFILE_PAGE_URL = f"{PORTAL_BASE_URL}/index.php?page=loadprofile"
PORTAL_LOADPROFILE_AJAX_URL = f"{PORTAL_BASE_URL}/index.php?page=loadprofile&ajax=y"
PORTAL_DATA_SERVICE_URL = f"{PORTAL_BASE_URL}/index.php"

# Resolutions supported by IKB Direkt portal
RESOLUTION_15MIN = "ResOpt1"
RESOLUTION_HOUR = "ResOpt2"
RESOLUTION_DAY = "ResOpt3"
RESOLUTION_WEEK = "ResOpt4"
RESOLUTION_MONTH = "ResOpt5"
