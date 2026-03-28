"""Scrape full messages from the FastDirect parent portal."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)


@dataclass
class SchoolMessage:
    subject: str
    body: str
    sender: str
    date: datetime
    raw_html: str = ""
    links: list[str] = field(default_factory=list)


class FastDirectScraper:
    """Logs in to FastDirect and retrieves full message content."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })
        self.base_url = config.FASTDIRECT_BASE_URL
        self.logged_in = False

    def login(self) -> bool:
        """Authenticate with FastDirect using school code + family credentials."""
        login_url = f"{self.base_url}/{config.FASTDIRECT_SCHOOL_CODE}"

        # Load the login page to get any CSRF tokens / hidden fields
        resp = self.session.get(login_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Find the login form and extract hidden inputs
        form = soup.find("form")
        if not form:
            logger.error("Could not find login form on %s", login_url)
            return False

        payload = {}
        for hidden in form.find_all("input", {"type": "hidden"}):
            name = hidden.get("name")
            if name:
                payload[name] = hidden.get("value", "")

        # Add credentials — FastDirect typically uses these field names
        # but they may vary; adjust if login fails
        payload.update({
            "username": config.FASTDIRECT_USERNAME,
            "password": config.FASTDIRECT_PASSWORD,
        })

        action = form.get("action", login_url)
        if action.startswith("/"):
            action = f"{self.base_url}{action}"

        resp = self.session.post(action, data=payload)
        resp.raise_for_status()

        # Check for successful login by looking for common post-login indicators
        if "logout" in resp.text.lower() or "sign out" in resp.text.lower():
            self.logged_in = True
            logger.info("Successfully logged in to FastDirect")
            return True

        # Some sites redirect on success
        if resp.url and "login" not in resp.url.lower():
            self.logged_in = True
            logger.info("Successfully logged in to FastDirect (redirect)")
            return True

        logger.error("Login may have failed — check credentials and school code")
        return False

    def get_messages(self) -> list[SchoolMessage]:
        """Fetch the list of messages from the messaging/inbox area."""
        if not self.logged_in:
            raise RuntimeError("Not logged in — call login() first")

        messages: list[SchoolMessage] = []

        # Navigate to the messages section
        # FastDirect URL patterns vary; try common paths
        message_paths = [
            f"/{config.FASTDIRECT_SCHOOL_CODE}/messages",
            f"/{config.FASTDIRECT_SCHOOL_CODE}/communications",
            f"/{config.FASTDIRECT_SCHOOL_CODE}/inbox",
            f"/{config.FASTDIRECT_SCHOOL_CODE}/parent/messages",
        ]

        inbox_soup = None
        for path in message_paths:
            url = f"{self.base_url}{path}"
            resp = self.session.get(url)
            if resp.status_code == 200 and "message" in resp.text.lower():
                inbox_soup = BeautifulSoup(resp.text, "lxml")
                logger.info("Found messages page at %s", url)
                break

        if inbox_soup is None:
            # Fallback: look for a messages link on the main page
            main_resp = self.session.get(
                f"{self.base_url}/{config.FASTDIRECT_SCHOOL_CODE}"
            )
            main_soup = BeautifulSoup(main_resp.text, "lxml")
            msg_link = main_soup.find("a", href=re.compile(r"message|comm|inbox", re.I))
            if msg_link:
                href = msg_link["href"]
                if not href.startswith("http"):
                    href = f"{self.base_url}{href}"
                resp = self.session.get(href)
                inbox_soup = BeautifulSoup(resp.text, "lxml")
                logger.info("Found messages via link: %s", href)

        if inbox_soup is None:
            logger.warning("Could not locate the messages inbox")
            return messages

        # Find individual message links and fetch each one
        message_links = self._find_message_links(inbox_soup)
        logger.info("Found %d message links", len(message_links))

        for link in message_links:
            msg = self._fetch_message(link)
            if msg:
                messages.append(msg)

        return messages

    def _find_message_links(self, soup: BeautifulSoup) -> list[str]:
        """Extract links to individual messages from the inbox page."""
        links = []
        # Look for rows/links that point to individual messages
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if re.search(r"message|view|detail|read", href, re.I):
                if not href.startswith("http"):
                    href = f"{self.base_url}{href}"
                if href not in links:
                    links.append(href)
        return links

    def _fetch_message(self, url: str) -> SchoolMessage | None:
        """Fetch and parse a single message page."""
        try:
            resp = self.session.get(url)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Failed to fetch message at %s: %s", url, e)
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        # Extract subject
        subject = ""
        subject_el = soup.find(
            ["h1", "h2", "h3"],
            class_=re.compile(r"subject|title|heading", re.I),
        )
        if subject_el:
            subject = subject_el.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            if title_tag:
                subject = title_tag.get_text(strip=True)

        # Extract message body
        body_el = soup.find(
            ["div", "td", "article"],
            class_=re.compile(r"body|content|message", re.I),
        )
        if body_el:
            body = body_el.get_text(separator="\n", strip=True)
            raw_html = str(body_el)
        else:
            # Fallback: grab the main content area
            main = soup.find("main") or soup.find("div", {"id": "content"})
            body = main.get_text(separator="\n", strip=True) if main else ""
            raw_html = str(main) if main else ""

        # Extract sender
        sender = ""
        sender_el = soup.find(
            string=re.compile(r"from|sender|teacher", re.I)
        )
        if sender_el:
            parent = sender_el.find_parent()
            if parent:
                sender = parent.get_text(strip=True)

        # Extract date
        date = datetime.now()
        date_el = soup.find(string=re.compile(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"))
        if date_el:
            match = re.search(r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", str(date_el))
            if match:
                for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y"):
                    try:
                        date = datetime.strptime(match.group(1), fmt)
                        break
                    except ValueError:
                        continue

        # Extract any links in the body (e.g., to event sign-ups)
        body_links = []
        if body_el:
            for a in body_el.find_all("a", href=True):
                body_links.append(a["href"])

        return SchoolMessage(
            subject=subject,
            body=body,
            sender=sender,
            date=date,
            raw_html=raw_html,
            links=body_links,
        )

    def get_message_from_link(self, url: str) -> SchoolMessage | None:
        """Fetch a single message given a direct link (e.g., from an email notification)."""
        if not self.logged_in:
            raise RuntimeError("Not logged in — call login() first")
        return self._fetch_message(url)
