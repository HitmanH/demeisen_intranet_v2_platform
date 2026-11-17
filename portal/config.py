"""Edit this file to configure the platform (firm details, members, menu)."""
import os

FIRM_NAME = "Demeisen LLP"
FIRM_ADDRESS = "c/o HaysMac, 10 Queen Street Place, London EC4R 1AG, United Kingdom"
FIRM_OPERATING_ADDRESS = "53 Davies Street, London W1K 5JH"
FIRM_COMPANY_NO = "OC460451"
BRAND_NAME = "Demeisen"            # shown in the header
WORDMARK_TEXT = "DEMEISEN"          # used for PDF wordmark/watermark
SECRET_KEY = os.environ.get("INTRANET_SECRET", "dev-change-me")

# LLP members - every one of these must sign off a memo. Default password: changeme
MEMBERS = [
    {"name": "Russell Hart",  "email": "russell.hart@demeisen.co.uk",  "admin": True},
    {"name": "Richard Heath", "email": "richard.heath@demeisen.co.uk", "admin": False},
]

# Memos at or above this value are flagged "material" (all members must sign off)
MATERIAL_THRESHOLD = 5000

# Demeisen bar (top of every page): all apps, in the same order in every app. [] shows just this app.
APPS = [
    {"title": "Intranet", "url": "/", "current": True},
    {"title": "Investor CRM", "url": os.environ.get("CRM_URL", "/crm/")},
]

# Functions hosted by the portal, in menu order. Each is a folder with a register(registry) function.
FUNCTIONS = [f for f in os.environ.get("PLATFORM_FUNCTIONS", "mnda,memo,documents").split(",") if f]

# Home page tiles
LINKS = [
    {"title": "CRM", "url": os.environ.get("CRM_URL", "/crm/"), "desc": "Demeisen CRM"},
    {"title": "Email", "url": "https://outlook.office.com", "desc": "Webmail"},
    {"title": "OneNDA", "url": "https://www.onenda.org/products/onenda", "desc": "OneNDA standard terms"},
    {"title": "Companies House", "url": "https://find-and-update.company-information.service.gov.uk/", "desc": "Company search"},
]

NDA_PURPOSES = {
    "investor": "Evaluating a potential investment in, or financing of, {firm}.",
    "data_vendor": "Evaluating and negotiating the potential licensing or supply of data by {cp} to {firm}.",
    "other": "",
}
