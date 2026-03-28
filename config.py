import os
from dotenv import load_dotenv

load_dotenv()

# FastDirect
FASTDIRECT_BASE_URL = "https://www.fastdir.com"
FASTDIRECT_SCHOOL_CODE = os.environ["FASTDIRECT_SCHOOL_CODE"]
FASTDIRECT_USERNAME = os.environ["FASTDIRECT_USERNAME"]
FASTDIRECT_PASSWORD = os.environ["FASTDIRECT_PASSWORD"]

# Microsoft Graph
MS_CLIENT_ID = os.environ["MS_CLIENT_ID"]
MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "common")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
MS_CALENDAR_NAME = os.environ.get("MS_CALENDAR_NAME", "")
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MS_SCOPES = ["Calendars.ReadWrite", "Mail.Read"]

EMAIL_LOOKBACK_DAYS = int(os.environ.get("EMAIL_LOOKBACK_DAYS", "7"))
