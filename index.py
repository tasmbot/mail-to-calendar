import base64
import hashlib
import imaplib
import json
import os
import re
import ssl
import time
from datetime import datetime, timedelta, timezone
from email import policy
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote, urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import email
import requests
from requests.auth import HTTPBasicAuth


IMAP_HOST = "imap.yandex.ru"
IMAP_PORT = 993
CALDAV_BASE = "https://caldav.icloud.com"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


class Diagnostics:
    def __init__(self):
        self.lines = []

    def add(self, message):
        message = str(message)
        self.lines.append(message)
        print(message)

    def response(self, processed=0, created=0, existing=0, failed=0, status_code=None):
        status_code = (
            status_code if status_code is not None else (200 if failed == 0 else 207)
        )
        return {
            "statusCode": status_code,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(
                {
                    "processed_emails": processed,
                    "created_events": created,
                    "existing_events": existing,
                    "failed_emails": failed,
                    "diagnostics": self.lines,
                },
                ensure_ascii=False,
                indent=2,
            ),
        }


class HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "li",
        "p",
        "section",
        "tr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.current_link = None

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            self.current_link = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag == "a" and self.current_link:
            self.parts.append(" " + self.current_link)
            self.current_link = None
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self):
        return "".join(self.parts)


def html_to_text(html_content):
    parser = HTMLTextExtractor()
    parser.feed(html_content or "")
    parser.close()
    return unescape(parser.get_text())


def decode_part(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_email_content(message):
    plain_parts, html_parts, calendar_parts = [], [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        filename = part.get_filename() or ""
        disposition = part.get_content_disposition()
        if content_type == "text/calendar" or filename.lower().endswith(".ics"):
            content = decode_part(part)
            if content:
                calendar_parts.append({"filename": filename, "content": content})
            continue
        if disposition == "attachment":
            continue
        if content_type == "text/plain":
            plain_parts.append(decode_part(part))
        elif content_type == "text/html":
            html_parts.append(decode_part(part))
    plain_text = "\n".join(plain_parts)
    html_content = "\n".join(html_parts)
    if not plain_text.strip() and html_content.strip():
        plain_text = html_to_text(html_content)
    return plain_text, html_content, calendar_parts


def parse_russian_date(date_str):
    match = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", date_str, re.IGNORECASE)
    if not match or not MONTHS.get(match.group(2).lower()):
        return None
    try:
        return datetime(
            int(match.group(3)), MONTHS[match.group(2).lower()], int(match.group(1))
        )
    except ValueError:
        return None


def normalize_text(text):
    text = unescape(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("**", "")
    text = re.sub(r"\\[ \t]*\n", "\n", text)
    return re.sub(r"[ \t]+\n", "\n", text)


def extract_assignment_link(block):
    match = re.search(r"https?://[^\s)\]>]+", block, re.IGNORECASE)
    return match.group(0).replace("\\&", "&").rstrip(".,;") if match else None


def extract_deadline_time(block):
    match = re.search(
        r"Срок\s*:\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?", block, re.IGNORECASE
    )
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    ampm = (match.group(3) or "").upper()
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "PM" else 0)
    elif hour > 23:
        return None
    return hour, minute


def parse_assignment_email(body_text, html_content=""):
    if not body_text.strip() and html_content:
        body_text = html_to_text(html_content)
    text = normalize_text(body_text)
    debug = [f"Длина текста: {len(text)}"]
    date_match = re.search(
        r"необходимо\s+сдать\s+до\s+(?:[а-яё]+\s+)?(\d{1,2}\s+[а-яё]+\s+\d{4})",
        text,
        re.IGNORECASE,
    )
    if not date_match:
        return [], debug + ["ERROR: дата дедлайна не найдена"]
    deadline = parse_russian_date(date_match.group(1))
    if not deadline:
        return [], debug + ["ERROR: дата дедлайна не распознана"]
    debug.append(f"Дата: {deadline:%Y-%m-%d}")

    assignments = []
    blocks = re.split(r"(?=^[ \t]*[-*•][ \t]+\S)", text, flags=re.MULTILINE)
    for block in blocks:
        if not re.match(r"^[ \t]*[-*•][ \t]+\S", block):
            continue
        match = re.search(
            r"^[ \t]*[-*•][ \t]+(?P<task>.+?)\s+в\s+курсе\s+(?P<course>.+?)\s+Срок\s*:",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            continue
        task_name = re.sub(r"\s+", " ", match.group("task")).strip().replace("_", " ")
        course_name = re.sub(r"\s+", " ", match.group("course")).strip()
        deadline_time = extract_deadline_time(block)
        link = extract_assignment_link(block)
        assignments.append(
            {
                "task_name": task_name,
                "course_name": course_name,
                "deadline": deadline,
                "deadline_time": deadline_time,
                "link": link,
            }
        )
        debug.append(
            f"Задание: {task_name} | Курс: {course_name} | Время: {deadline_time} | "
            f"Ссылка: {'есть' if link else 'нет'}"
        )
    debug.append(f"Всего найдено заданий: {len(assignments)}")
    return assignments, debug


def escape_ics_text(value):
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def fold_ics_line(line):
    result, current, size = [], "", 0
    for character in line:
        character_size = len(character.encode("utf-8"))
        if current and size + character_size > 75:
            result.append(current)
            current, size = " ", 1
        current += character
        size += character_size
    if current:
        result.append(current)
    return "\r\n".join(result)


def generate_ics_for_assignment(
    assignment, all_day=True, timezone_name="Europe/Moscow"
):
    link = assignment.get("link") or ""
    id_match = re.search(r"[?&]id=(\d+)", link)
    identity = (
        ("moodle-assignment:" + id_match.group(1))
        if id_match
        else "|".join(
            [
                assignment["task_name"],
                assignment["course_name"],
                assignment["deadline"].strftime("%Y-%m-%d"),
            ]
        )
    )
    uid = hashlib.sha256(identity.encode("utf-8")).hexdigest() + "@yandex-cloud"
    summary = f"Дедлайн. {assignment['task_name']} в курсе {assignment['course_name']}"
    description = f"Ссылка: {link}" if link else ""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "PRODID:-//mail-to-calendar//Assignment Parser//RU",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
    ]
    if all_day:
        start = assignment["deadline"]
        lines += [
            f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
            f"DTEND;VALUE=DATE:{start + timedelta(days=1):%Y%m%d}",
        ]
    else:
        hour, minute = assignment.get("deadline_time") or (23, 59)
        local_datetime = assignment["deadline"].replace(hour=hour, minute=minute)
        utc_datetime = local_datetime.replace(
            tzinfo=ZoneInfo(timezone_name)
        ).astimezone(timezone.utc)
        lines += [
            f"DTSTART:{utc_datetime:%Y%m%dT%H%M%SZ}",
            f"DTEND:{utc_datetime + timedelta(minutes=30):%Y%m%dT%H%M%SZ}",
        ]
    lines += [
        f"SUMMARY:{escape_ics_text(summary)}",
        f"DESCRIPTION:{escape_ics_text(description)}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n", uid


def request_with_retries(method, url, diagnostics, **kwargs):
    retry_statuses = {429, 500, 502, 503, 504}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if response.status_code not in retry_statuses or attempt == MAX_RETRIES:
                return response
            diagnostics.add(
                f"[HTTP] {method} получил {response.status_code}; повтор {attempt}/{MAX_RETRIES}"
            )
        except (requests.ConnectionError, requests.Timeout):
            diagnostics.add(
                f"[HTTP] {method} ошибка соединения; повтор {attempt}/{MAX_RETRIES}"
            )
            if attempt == MAX_RETRIES:
                raise
        time.sleep(attempt * 2)
    raise RuntimeError("HTTP-запрос не был выполнен")


def absolute_dav_url(base_url, href):
    return urljoin(base_url.rstrip("/") + "/", href)


def find_href(root, path, namespaces):
    element = root.find(path, namespaces)
    return element.text.strip() if element is not None and element.text else None


def propfind_with_redirects(url, diagnostics, **kwargs):
    """Follow DAV redirects without allowing requests to change PROPFIND to GET."""
    for _ in range(5):
        response = request_with_retries(
            "PROPFIND", url, diagnostics, allow_redirects=False, **kwargs
        )
        if response.status_code not in (301, 302, 307, 308):
            return response
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("iCloud вернул перенаправление без Location")
        url = absolute_dav_url(response.url, location)
    raise RuntimeError("Слишком много перенаправлений iCloud CalDAV")


def discover_calendar(auth, target_name, diagnostics):
    headers = {
        "User-Agent": "mail-to-calendar/1.0",
        "Content-Type": "application/xml; charset=utf-8",
    }
    namespaces = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav"}
    principal_xml = (
        '<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:">'
        "<D:prop><D:current-user-principal/></D:prop></D:propfind>"
    )
    response = propfind_with_redirects(
        CALDAV_BASE + "/.well-known/caldav",
        diagnostics,
        headers={**headers, "Depth": "0"},
        auth=auth,
        data=principal_xml,
    )
    if response.status_code not in (200, 207):
        raise RuntimeError(f"iCloud discovery: HTTP {response.status_code}")
    root = ET.fromstring(response.content)
    principal_href = find_href(root, ".//D:current-user-principal/D:href", namespaces)
    principal_url = (
        absolute_dav_url(response.url, principal_href)
        if principal_href
        else response.url
    )

    home_xml = (
        '<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:" '
        'xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><C:calendar-home-set/>'
        "</D:prop></D:propfind>"
    )
    response = propfind_with_redirects(
        principal_url,
        diagnostics,
        headers={**headers, "Depth": "0"},
        auth=auth,
        data=home_xml,
    )
    if response.status_code not in (200, 207):
        raise RuntimeError(f"iCloud calendar-home-set: HTTP {response.status_code}")
    root = ET.fromstring(response.content)
    home_href = find_href(root, ".//C:calendar-home-set/D:href", namespaces)
    if not home_href:
        raise RuntimeError("iCloud не вернул calendar-home-set")
    home_url = absolute_dav_url(response.url, home_href)

    calendars_xml = (
        '<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:" '
        'xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><D:displayname/>'
        "<D:resourcetype/></D:prop></D:propfind>"
    )
    response = propfind_with_redirects(
        home_url,
        diagnostics,
        headers={**headers, "Depth": "1"},
        auth=auth,
        data=calendars_xml,
    )
    if response.status_code not in (200, 207):
        raise RuntimeError(f"iCloud calendar list: HTTP {response.status_code}")
    root = ET.fromstring(response.content)
    exact_match = partial_match = None
    for item in root.findall(".//D:response", namespaces):
        href = find_href(item, "D:href", namespaces)
        display_name = find_href(item, ".//D:displayname", namespaces) or ""
        is_calendar = item.find(".//D:resourcetype/C:calendar", namespaces) is not None
        if not href or not is_calendar:
            continue
        candidate = absolute_dav_url(response.url, href)
        if display_name.casefold() == target_name.casefold():
            exact_match = candidate
            break
        if target_name.casefold() in display_name.casefold():
            partial_match = candidate
    calendar_url = exact_match or partial_match
    if not calendar_url:
        raise RuntimeError(f'Календарь "{target_name}" не найден')
    diagnostics.add(f'[ICLOUD] Календарь "{target_name}" найден')
    return calendar_url.rstrip("/") + "/"


def unfold_ics(ics_content):
    return re.sub(r"\r?\n[ \t]", "", ics_content)


def extract_ics_uid(ics_content):
    match = re.search(
        r"^UID(?:;[^:]*)?:(.+)$", unfold_ics(ics_content), re.MULTILINE | re.IGNORECASE
    )
    return match.group(1).strip().rstrip("\r") if match else None


def put_calendar_event(calendar_url, ics_content, uid, auth, diagnostics):
    resource_id = hashlib.sha256(uid.encode("utf-8")).hexdigest()
    event_url = urljoin(calendar_url, quote(resource_id, safe="") + ".ics")
    response = request_with_retries(
        "PUT",
        event_url,
        diagnostics,
        headers={
            "User-Agent": "mail-to-calendar/1.0",
            "Content-Type": "text/calendar; charset=utf-8",
            "If-None-Match": "*",
        },
        auth=auth,
        data=ics_content.encode("utf-8"),
    )
    if response.status_code in (201, 204):
        return "created"
    if response.status_code == 412:
        existing = request_with_retries(
            "GET",
            event_url,
            diagnostics,
            headers={"User-Agent": "mail-to-calendar/1.0"},
            auth=auth,
        )
        if existing.status_code == 200 and extract_ics_uid(existing.text) == uid:
            return "existing"
        raise RuntimeError("Ресурс события существует, но его UID не совпадает")
    raise RuntimeError(f"iCloud PUT: HTTP {response.status_code}")


def connect_imap(email_address, password, diagnostics):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            connection = imaplib.IMAP4_SSL(
                IMAP_HOST,
                IMAP_PORT,
                ssl_context=ssl.create_default_context(),
                timeout=15,
            )
            connection.login(email_address, password)
            diagnostics.add("[IMAP] Авторизация выполнена")
            return connection
        except imaplib.IMAP4.error as error:
            raise RuntimeError("Ошибка авторизации в Яндекс.Почте") from error
        except (OSError, TimeoutError) as error:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Yandex IMAP недоступен: {error}") from error
            diagnostics.add(f"[IMAP] Ошибка соединения; повтор {attempt}/{MAX_RETRIES}")
            time.sleep(attempt * 2)
    raise RuntimeError("Не удалось подключиться к Яндекс.Почте")


def encode_imap_mailbox(name):
    """Encode a Unicode mailbox name with IMAP modified UTF-7."""
    result = []
    unicode_buffer = []

    def flush_buffer():
        if not unicode_buffer:
            return
        encoded = base64.b64encode("".join(unicode_buffer).encode("utf-16be"))
        result.append("&" + encoded.decode("ascii").rstrip("=").replace("/", ",") + "-")
        unicode_buffer.clear()

    for character in name:
        if "\x20" <= character <= "\x7e":
            flush_buffer()
            result.append("&-" if character == "&" else character)
        else:
            unicode_buffer.append(character)
    flush_buffer()
    return "".join(result)


def bool_from_env(name, default):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} должен иметь значение true или false")
    return value == "true"


def handler(event, context):
    del event, context
    diagnostics = Diagnostics()
    diagnostics.add("[START] Функция запущена")
    yandex_email = os.getenv("YANDEX_EMAIL", "").strip()
    yandex_password = os.getenv("YANDEX_PASSWORD", "").strip()
    icloud_email = os.getenv("ICLOUD_EMAIL", "").strip()
    icloud_password = os.getenv("ICLOUD_PASSWORD", "").strip()
    mailbox_name = os.getenv("TARGET_MAILBOX_NAME", "iCloud").strip()
    calendar_name = os.getenv("TARGET_CALENDAR_NAME", "Учеба").strip()
    subject_pattern = os.getenv(
        "ASSIGNMENT_SUBJECT_PATTERN", "У вас есть задания, которые нужно сдать через"
    ).strip()
    timezone_name = os.getenv("DEADLINE_TIMEZONE", "Europe/Moscow").strip()
    missing = [
        name
        for name, value in (
            ("YANDEX_EMAIL", yandex_email),
            ("YANDEX_PASSWORD", yandex_password),
            ("ICLOUD_EMAIL", icloud_email),
            ("ICLOUD_PASSWORD", icloud_password),
        )
        if not value
    ]
    if missing:
        diagnostics.add("[CONFIG] Не заданы переменные: " + ", ".join(missing))
        return diagnostics.response(failed=1, status_code=500)
    try:
        all_day = bool_from_env("ASSIGNMENT_ALL_DAY", True)
        ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        diagnostics.add(f"[CONFIG] {error}")
        return diagnostics.response(failed=1, status_code=500)

    mail = None
    processed = created = existing = failed = 0
    try:
        mail = connect_imap(yandex_email, yandex_password, diagnostics)
        status, _ = mail.select(f'"{encode_imap_mailbox(mailbox_name)}"')
        if status != "OK":
            raise RuntimeError(f'Не удалось выбрать папку "{mailbox_name}"')
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("Не удалось получить список непрочитанных писем")
        message_ids = messages[0].split() if messages and messages[0] else []
        diagnostics.add(f"[IMAP] Непрочитанных писем: {len(message_ids)}")
        if not message_ids:
            return diagnostics.response()
        auth = HTTPBasicAuth(icloud_email, icloud_password)
        calendar_url = discover_calendar(auth, calendar_name, diagnostics)

        for message_id in message_ids:
            message_label = message_id.decode("ascii", errors="replace")
            diagnostics.add(f"[MAIL {message_label}] Начало обработки")
            try:
                status, data = mail.fetch(message_id, "(BODY.PEEK[])")
                if status != "OK" or not data or not isinstance(data[0], tuple):
                    raise RuntimeError("IMAP не вернул содержимое письма")
                message = email.message_from_bytes(data[0][1], policy=policy.default)
                subject = str(message.get("Subject", ""))
                diagnostics.add(f"[MAIL {message_label}] Тема: {subject[:120]}")
                body_text, html_content, calendar_parts = extract_email_content(message)
                outcomes = []
                if calendar_parts:
                    diagnostics.add(
                        f"[MAIL {message_label}] Вложений calendar/ICS: {len(calendar_parts)}"
                    )
                    for calendar_part in calendar_parts:
                        uid = extract_ics_uid(calendar_part["content"])
                        if not uid:
                            raise RuntimeError("Во вложении .ics отсутствует UID")
                        outcomes.append(
                            put_calendar_event(
                                calendar_url,
                                calendar_part["content"],
                                uid,
                                auth,
                                diagnostics,
                            )
                        )
                elif subject_pattern.casefold() in subject.casefold():
                    assignments, parse_log = parse_assignment_email(
                        body_text, html_content
                    )
                    for line in parse_log:
                        diagnostics.add(f"[MAIL {message_label}] {line}")
                    if not assignments:
                        raise RuntimeError("Парсер не нашёл заданий")
                    for assignment in assignments:
                        ics_content, uid = generate_ics_for_assignment(
                            assignment, all_day, timezone_name
                        )
                        outcomes.append(
                            put_calendar_event(
                                calendar_url, ics_content, uid, auth, diagnostics
                            )
                        )
                else:
                    diagnostics.add(
                        f"[MAIL {message_label}] Тип письма не поддерживается; пропуск"
                    )
                    continue
                created += outcomes.count("created")
                existing += outcomes.count("existing")
                store_status, _ = mail.store(message_id, "+FLAGS", "\\Seen")
                if store_status != "OK":
                    raise RuntimeError("Не удалось пометить письмо прочитанным")
                processed += 1
                diagnostics.add(
                    f"[MAIL {message_label}] Обработано: создано {outcomes.count('created')}, уже было {outcomes.count('existing')}"
                )
            except Exception as error:
                failed += 1
                diagnostics.add(
                    f"[MAIL {message_label}] ERROR: {type(error).__name__}: {error}; письмо оставлено непрочитанным"
                )
    except Exception as error:
        diagnostics.add(f"[FATAL] {type(error).__name__}: {error}")
        failed += 1
        return diagnostics.response(
            processed, created, existing, failed, status_code=500
        )
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
    diagnostics.add(
        f"[END] Обработано писем: {processed}; создано: {created}; уже существовало: {existing}; ошибок: {failed}"
    )
    return diagnostics.response(processed, created, existing, failed)
