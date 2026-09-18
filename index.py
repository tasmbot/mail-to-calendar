import os
import imaplib
import email
import ssl
import requests
import time
from requests.auth import HTTPBasicAuth

def handler(event, context):
    YANDEX_EMAIL = os.environ.get('YANDEX_EMAIL')
    YANDEX_PASSWORD = os.environ.get('YANDEX_PASSWORD')
    ICLOUD_EMAIL = os.environ.get('ICLOUD_EMAIL')
    ICLOUD_PASSWORD = os.environ.get('ICLOUD_PASSWORD')
    
    TARGET_CALENDAR_NAME = "Учеба" # указать календарь, в который нужно сохранять события
    TARGET_MAILBOX_NAME = "iCloud" # указать папку в Яндекс.Почте, откуда нужно брать письма
    
    mail = None
    
    # 1. ПОДКЛЮЧЕНИЕ К ЯНДЕКСУ
    max_retries = 3
    for attempt in range(max_retries):
        try:
            context_ssl = ssl.create_default_context()
            mail = imaplib.IMAP4_SSL("imap.yandex.ru", 993, ssl_context=context_ssl, timeout=10)
            mail.login(YANDEX_EMAIL, YANDEX_PASSWORD)
            break
        except Exception as e:
            if attempt == max_retries - 1:
                return {'statusCode': 500, 'body': f'Yandex IMAP Error: {str(e)}'}
            time.sleep(2)

    try:
        status, _ = mail.select(f'"{TARGET_MAILBOX_NAME}"')
        
        if status != 'OK':
            mail.logout()
            return {'statusCode': 500, 'body': f'Не удалось выбрать папку "{TARGET_MAILBOX_NAME}".'}
        
        status, messages = mail.search(None, 'UNSEEN')
        
        if status != 'OK' or not messages or messages[0] == b'':
            mail.logout()
            return {'statusCode': 200, 'body': 'Новых непрочитанных писем нет.'}
            
    except Exception as e:
        if mail:
            mail.logout()
        return {'statusCode': 500, 'body': f'Yandex IMAP Processing Error: {str(e)}'}
    
    # 2. ПОДКЛЮЧЕНИЕ К ICLOUD (Правильный двухэтапный процесс)
    try:
        auth = HTTPBasicAuth(ICLOUD_EMAIL, ICLOUD_PASSWORD)
        headers_base = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
            'Content-Type': 'application/xml; charset=utf-8',
        }
        
        # ШАГ 2.1: Получаем principal URL
        xml_discovery = '''<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
    <D:prop><D:current-user-principal/></D:prop>
</D:propfind>'''
        
        response1 = requests.request(
            'PROPFIND', 'https://caldav.icloud.com/.well-known/caldav',
            headers={**headers_base, 'Depth': '0'},
            auth=auth, data=xml_discovery, timeout=15, allow_redirects=False
        )
        
        if response1.status_code in [301, 302, 307, 308]:
            principal_url = response1.headers.get('Location')
            if not principal_url.startswith('http'):
                principal_url = f'https://caldav.icloud.com{principal_url}'
        elif response1.status_code in [200, 207]:
            principal_url = 'https://caldav.icloud.com/.well-known/caldav'
        else:
            mail.logout()
            return {'statusCode': 500, 'body': f'iCloud Discovery Error: HTTP {response1.status_code}'}
        
        # ШАГ 2.2: Получаем calendar-home-set из principal URL
        xml_home_set = '''<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
    <D:prop>
        <C:calendar-home-set/>
    </D:prop>
</D:propfind>'''
        
        response2 = requests.request(
            'PROPFIND', principal_url,
            headers={**headers_base, 'Depth': '0'},
            auth=auth, data=xml_home_set, timeout=15
        )
        
        if response2.status_code not in [200, 207]:
            mail.logout()
            return {'statusCode': 500, 'body': f'iCloud Home Set Error: HTTP {response2.status_code}. Ответ: {response2.text[:300]}'}
        
        # Парсим calendar-home-set URL
        import xml.etree.ElementTree as ET
        root = ET.fromstring(response2.content)
        namespaces = {'D': 'DAV:', 'C': 'urn:ietf:params:xml:ns:caldav'}
        
        home_set_elem = root.find('.//C:calendar-home-set/D:href', namespaces)
        if home_set_elem is None:
            home_set_elem = root.find('.//C:calendar-home-set', namespaces)
        
        if home_set_elem is None or not home_set_elem.text:
            mail.logout()
            return {'statusCode': 500, 'body': f'Не удалось получить calendar-home-set из ответа Apple'}
        
        calendar_home_url = home_set_elem.text
        if not calendar_home_url.startswith('http'):
            calendar_home_url = f'https://caldav.icloud.com{calendar_home_url}'
        
        # ШАГ 2.3: Получаем список календарей из calendar-home-set
        xml_calendars = '''<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
    <D:prop>
        <D:displayname/>
        <D:resourcetype/>
    </D:prop>
</D:propfind>'''
        
        response3 = requests.request(
            'PROPFIND', calendar_home_url,
            headers={**headers_base, 'Depth': '1'},
            auth=auth, data=xml_calendars, timeout=15
        )
        
        if response3.status_code not in [200, 207]:
            mail.logout()
            return {'statusCode': 500, 'body': f'iCloud Calendar List Error: HTTP {response3.status_code}. Ответ: {response3.text[:300]}'}
        
        # Парсим список календарей
        root = ET.fromstring(response3.content)
        target_calendar_url = None
        
        for resp_elem in root.findall('.//D:response', namespaces):
            href = resp_elem.find('D:href', namespaces)
            propstat = resp_elem.find('.//D:propstat[D:status="HTTP/1.1 200 OK"]', namespaces)
            
            if propstat is not None and href is not None:
                displayname = propstat.find('.//D:displayname', namespaces)
                resourcetype = propstat.find('.//D:resourcetype', namespaces)
                
                if resourcetype is not None and resourcetype.find('C:calendar', namespaces) is not None:
                    cal_name = displayname.text.strip() if displayname is not None and displayname.text else ''
                    if TARGET_CALENDAR_NAME.lower() in cal_name.lower():
                        target_calendar_url = href.text
                        break
        
        if not target_calendar_url:
            mail.logout()
            all_cals = []
            for resp_elem in root.findall('.//D:response', namespaces):
                propstat = resp_elem.find('.//D:propstat[D:status="HTTP/1.1 200 OK"]', namespaces)
                if propstat is not None:
                    displayname = propstat.find('.//D:displayname', namespaces)
                    resourcetype = propstat.find('.//D:resourcetype', namespaces)
                    if resourcetype is not None and resourcetype.find('C:calendar', namespaces) is not None:
                        if displayname is not None and displayname.text:
                            all_cals.append(displayname.text.strip())
            return {'statusCode': 500, 'body': f'Календарь "{TARGET_CALENDAR_NAME}" не найден. Доступные: {", ".join(all_cals)}'}
        
        if not target_calendar_url.startswith('http'):
            target_calendar_url = f'https://caldav.icloud.com{target_calendar_url}'
            
    except Exception as e:
        if mail:
            mail.logout()
        return {'statusCode': 500, 'body': f'iCloud CalDAV Critical Error: {str(e)}'}

    # 3. ОБРАБОТКА ПИСЕМ
    processed_count = 0
    for num in messages[0].split():
        try:
            status, data = mail.fetch(num, '(RFC822)')
            if status != 'OK' or not data or not data[0]:
                continue
                
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            ics_content = None
            for part in msg.walk():
                content_type = part.get_content_type()
                filename = part.get_filename()
                if content_type == 'text/calendar' or (filename and filename.lower().endswith('.ics')):
                    ics_content = part.get_payload(decode=True)
                    break
            
            if ics_content:
                ics_string = ics_content.decode('utf-8', errors='ignore')
                
                uid = next((line.split(':', 1)[1].strip() for line in ics_string.split('\n') if line.startswith('UID:')), f'event-{num.decode()}')
                
                event_url = f'{target_calendar_url}{uid}.ics'
                put_response = requests.put(
                    event_url,
                    headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'text/calendar; charset=utf-8', 'If-None-Match': '*'},
                    auth=auth,
                    data=ics_string.encode('utf-8'),
                    timeout=15
                )
                
                if put_response.status_code in [201, 204]:
                    mail.store(num, '+FLAGS', '\\Seen')
                    processed_count += 1
                else:
                    print(f"Failed to save event {uid}: HTTP {put_response.status_code}")
                
        except Exception as e:
            print(f"Error processing message {num}: {str(e)}")
            continue

    mail.logout()
    return {'statusCode': 200, 'body': f'Successfully processed {processed_count} email(s)'}