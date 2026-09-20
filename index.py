import os
import imaplib
import email
import ssl
import requests
import time
import re
from datetime import datetime, timedelta
from requests.auth import HTTPBasicAuth

TARGET_MAILBOX_NAME = "iCloud"

def parse_russian_date(date_str):
    months = {
        'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4,
        'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
        'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
    }
    match = re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', date_str, re.IGNORECASE)
    if match:
        day = int(match.group(1))
        month_name = match.group(2).lower()
        year = int(match.group(3))
        if month_name in months:
            return datetime(year, months[month_name], day)
    return None

def parse_assignment_email(body_text, html_content=''):
    assignments, debug_log = [], []
    
    # Шаг 1: ищем дату
    date_match = re.search(r'необходимо сдать до\s+(.+?)(?:\.|$)', body_text, re.IGNORECASE)
    if not date_match:
        return [], ["❌ Дата не найдена"]
    
    deadline_date = parse_russian_date(date_match.group(1))
    if not deadline_date:
        return [], [f"❌ Не удалось распарсить дату: '{date_match.group(1)}'"]
    
    debug_log.append(f"✅ Дата: {deadline_date.strftime('%Y-%m-%d')}")
    
    # Шаг 2: извлекаем ссылки из HTML
    html_links = []
    if html_content:
        link_pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>.*?Перейти к элементу.*?</a>'
        html_links = re.findall(link_pattern, html_content, re.IGNORECASE | re.DOTALL)
        debug_log.append(f"✅ Ссылок в HTML: {len(html_links)}")
    
    # Шаг 3: УНИВЕРСАЛЬНЫЙ парсер
    # Логика: [ЛЮБОЙ ТЕКСТ] в курсе [ЛЮБОЙ ТЕКСТ ДО СКОБКИ]
    # Это покрывает ВСЕ форматы: "Домашнее задание 1", "ДЗ_1_Модуль_1", и любые другие
    assignment_pattern = r'([^\n]+?)\s+в курсе\s+([^\n(]+?)(?:\s*\([^)]+\))?(?=\s*(?:Срок:|$))'
    matches = list(re.finditer(assignment_pattern, body_text, re.IGNORECASE | re.MULTILINE))
    
    debug_log.append(f"✅ Найдено блоков заданий: {len(matches)}")
    
    link_index = 0
    for i, match in enumerate(matches):
        task_name = match.group(1).strip()
        course_name = match.group(2).strip()
        
        # Очищаем название задания от маркеров списка
        task_name = re.sub(r'^\*\s*', '', task_name)  # Убираем "* " в начале
        
        # Форматируем название задания
        if '_' in task_name:
            # Если есть подчеркивания (ДЗ_1_Модуль_1), заменяем на пробелы
            task_name = task_name.replace('_', ' ')
        elif task_name.isupper():
            # Если полностью капсом (ДОМАШНЕЕ ЗАДАНИЕ 1), делаем первую букву заглавной
            task_name = task_name.capitalize()
        
        # Очищаем курс от переносов строк и лишних пробелов
        course_name = re.sub(r'\s+', ' ', course_name.replace('\n', ' ').replace('\r', ''))
        
        link = html_links[link_index] if link_index < len(html_links) else None
        link_index += 1
        
        assignments.append({
            'task_name': task_name,
            'course_name': course_name,
            'deadline': deadline_date,
            'link': link
        })
        debug_log.append(f"✅ Задание {i+1}: '{task_name}' | Курс: '{course_name}' | Ссылка: {'Есть' if link else 'Нет'}")
    
    return assignments, debug_log

def generate_ics_for_assignment(assignment):
    deadline = assignment['deadline']
    next_day = deadline + timedelta(days=1)
    
    dtstart = deadline.strftime('%Y%m%d')
    dtend = next_day.strftime('%Y%m%d')
    dtstamp = datetime.now().strftime('%Y%m%dT%H%M%SZ')
    
    uid = f"deadline-{assignment['task_name']}-{dtstart}@yandex-cloud"
    uid = re.sub(r'[^a-zA-Z0-9@.-]', '-', uid)
    
    summary = f"Дедлайн. {assignment['task_name']} в курсе {assignment['course_name']}"
    description = f"Ссылка: {assignment['link'].replace('&amp;', '&')}" if assignment['link'] else ""
    
    ics_lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Yandex Cloud//Assignment Parser//RU",
        "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{dtstamp}", f"DTSTART;VALUE=DATE:{dtstart}",
        f"DTEND;VALUE=DATE:{dtend}", f"SUMMARY:{summary}", f"DESCRIPTION:{description}",
        "END:VEVENT", "END:VCALENDAR"
    ]
    return "\r\n".join(ics_lines), uid

def handler(event, context):
    print("="*50)
    print("[START] Функция запущена")
    
    YANDEX_EMAIL = os.environ.get('YANDEX_EMAIL', '').strip()
    YANDEX_PASSWORD = os.environ.get('YANDEX_PASSWORD', '').strip()
    ICLOUD_EMAIL = os.environ.get('ICLOUD_EMAIL', '').strip()
    ICLOUD_PASSWORD = os.environ.get('ICLOUD_PASSWORD', '').strip()
    
    TARGET_CALENDAR_NAME = "Учеба"
    ASSIGNMENT_SUBJECT_PATTERN = "У вас есть задания, которые нужно сдать через"
    
    if not all([YANDEX_EMAIL, YANDEX_PASSWORD, ICLOUD_EMAIL, ICLOUD_PASSWORD]):
        print("[ERROR] Отсутствуют переменные окружения")
        return {'statusCode': 500, 'body': '❌ Переменные окружения пусты.'}
    
    mail = None
    print("[IMAP] Попытка подключения к imap.yandex.ru...")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            context_ssl = ssl.create_default_context()
            mail = imaplib.IMAP4_SSL("imap.yandex.ru", 993, ssl_context=context_ssl, timeout=15)
            print(f"[IMAP] Попытка авторизации ({attempt + 1}/{max_retries})...")
            mail.login(YANDEX_EMAIL, YANDEX_PASSWORD)
            print("[IMAP] ✅ Успешная авторизация в Яндекс.Почте")
            break
        except imaplib.IMAP4.error as e:
            print(f"[IMAP] ❌ Ошибка авторизации: {str(e)}")
            return {'statusCode': 500, 'body': f'❌ Ошибка авторизации: {str(e)}'}
        except Exception as e:
            print(f"[IMAP] ⚠️ Сетевая ошибка, повтор через 2 сек: {str(e)}")
            if attempt == max_retries - 1:
                return {'statusCode': 500, 'body': f'❌ Yandex IMAP Error: {str(e)}'}
            time.sleep(2)

    try:
        print(f"[IMAP] Выбор папки: '{TARGET_MAILBOX_NAME}'...")
        status, _ = mail.select(f'"{TARGET_MAILBOX_NAME}"')
        if status != 'OK':
            mail.logout()
            return {'statusCode': 500, 'body': f'❌ Не удалось выбрать папку "{TARGET_MAILBOX_NAME}".'}
        
        print("[IMAP] Поиск непрочитанных писем (UNSEEN)...")
        status, messages = mail.search(None, 'UNSEEN')
        if status != 'OK' or not messages or messages[0] == b'':
            mail.logout()
            print("[IMAP] ✅ Новых писем нет. Завершение.")
            return {'statusCode': 200, 'body': '✅ Новых непрочитанных писем нет.'}
        
        msg_ids = messages[0].split()
        print(f"[IMAP] ✅ Найдено непрочитанных писем: {len(msg_ids)}")
            
    except Exception as e:
        if mail: mail.logout()
        print(f"[IMAP] ❌ Ошибка обработки: {str(e)}")
        return {'statusCode': 500, 'body': f'❌ Yandex IMAP Processing Error: {str(e)}'}
    
    # --- iCloud CalDAV с механизмом Retry ---
    print("[ICLOUD] Начало подключения к CalDAV...")
    try:
        auth = HTTPBasicAuth(ICLOUD_EMAIL, ICLOUD_PASSWORD)
        headers_base = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15', 'Content-Type': 'application/xml; charset=utf-8'}
        
        # НОВОЕ: Механизм повторных попыток для iCloud
        max_icloud_retries = 3
        response1 = None
        
        for attempt in range(max_icloud_retries):
            try:
                print(f"[ICLOUD] Шаг 1: Попытка {attempt + 1}. Отправка PROPFIND к .well-known...")
                xml_discovery = '<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:current-user-principal/></D:prop></D:propfind>'
                
                response1 = requests.request(
                    'PROPFIND', 'https://caldav.icloud.com/.well-known/caldav',
                    headers={**headers_base, 'Depth': '0'},
                    auth=auth, data=xml_discovery, timeout=15, allow_redirects=False
                )
                print(f"[ICLOUD] Шаг 1: ✅ Получен ответ HTTP {response1.status_code}")
                break # Успех, выходим из цикла попыток
                
            except requests.exceptions.Timeout:
                print(f"[ICLOUD] ⚠️ ТАЙМАУТ соединения с Apple (попытка {attempt + 1}). Ждем 3 сек и пробуем снова...")
                time.sleep(3)
            except requests.exceptions.ConnectionError as e:
                print(f"[ICLOUD] ⚠️ Ошибка соединения с Apple (попытка {attempt + 1}): {str(e)}. Ждем 3 сек...")
                time.sleep(3)
            except Exception as e:
                print(f"[ICLOUD] ❌ Неожиданная ошибка при запросе: {str(e)}")
                break
        
        # Если после всех попыток ответа нет
        if response1 is None:
            mail.logout()
            return {'statusCode': 500, 'body': '❌ Не удалось подключиться к iCloud после 3 попыток. Сервер Apple не отвечает или блокирует IP Yandex Cloud.'}

        if response1.status_code in [301, 302, 307, 308]:
            principal_url = response1.headers.get('Location')
            if not principal_url.startswith('http'): principal_url = f'https://caldav.icloud.com{principal_url}'
        elif response1.status_code in [200, 207]:
            principal_url = 'https://caldav.icloud.com/.well-known/caldav'
        else:
            mail.logout()
            return {'statusCode': 500, 'body': f'❌ iCloud Discovery Error: HTTP {response1.status_code}. Ответ: {response1.text[:200]}'}
        
        print("[ICLOUD] Шаг 2: Запрос calendar-home-set...")
        xml_home_set = '<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><C:calendar-home-set/></D:prop></D:propfind>'
        response2 = requests.request('PROPFIND', principal_url, headers={**headers_base, 'Depth': '0'}, auth=auth, data=xml_home_set, timeout=15)
        print(f"[ICLOUD] Шаг 2: Получен HTTP {response2.status_code}")
        
        if response2.status_code not in [200, 207]:
            mail.logout()
            return {'statusCode': 500, 'body': f'❌ iCloud Home Set Error: HTTP {response2.status_code}'}
        
        import xml.etree.ElementTree as ET
        root = ET.fromstring(response2.content)
        namespaces = {'D': 'DAV:', 'C': 'urn:ietf:params:xml:ns:caldav'}
        
        home_set_elem = root.find('.//C:calendar-home-set/D:href', namespaces)
        if home_set_elem is None or not home_set_elem.text:
            mail.logout()
            return {'statusCode': 500, 'body': '❌ Не удалось получить calendar-home-set'}
        
        calendar_home_url = home_set_elem.text
        if not calendar_home_url.startswith('http'):
            calendar_home_url = f'https://caldav.icloud.com{calendar_home_url}'
        
        print("[ICLOUD] Шаг 3: Запрос списка календарей...")
        xml_calendars = '<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><D:displayname/><D:resourcetype/></D:prop></D:propfind>'
        response3 = requests.request('PROPFIND', calendar_home_url, headers={**headers_base, 'Depth': '1'}, auth=auth, data=xml_calendars, timeout=15)
        print(f"[ICLOUD] Шаг 3: Получен HTTP {response3.status_code}")
        
        if response3.status_code not in [200, 207]:
            mail.logout()
            return {'statusCode': 500, 'body': f'❌ iCloud Calendar List Error: HTTP {response3.status_code}'}
        
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
            return {'statusCode': 500, 'body': f'❌ Календарь "{TARGET_CALENDAR_NAME}" не найден.'}
        
        if not target_calendar_url.startswith('http'):
            target_calendar_url = f'https://caldav.icloud.com{target_calendar_url}'
            
        print(f"[ICLOUD] ✅ Целевой календарь найден и готов к работе")
            
    except Exception as e:
        if mail: mail.logout()
        print(f"[ICLOUD] ❌ Критическая ошибка: {str(e)}")
        return {'statusCode': 500, 'body': f'❌ iCloud CalDAV Critical Error: {str(e)}'}

    # --- Обработка писем ---
        # --- Обработка писем ---
    processed_count = 0
    ics_count = 0
    
    for num in msg_ids:
        print(f"[PROCESS] Начало обработки письма #{num.decode()}...")
        try:
            status, data = mail.fetch(num, '(RFC822)')
            if status != 'OK' or not data or not data[0]:
                continue
                
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            subject = msg.get('Subject', '')
            subject_decoded = email.header.decode_header(subject)
            subject_text = ''
            for part, encoding in subject_decoded:
                if isinstance(part, bytes):
                    subject_text += part.decode(encoding or 'utf-8', errors='ignore')
                else:
                    subject_text += part
            
            print(f"[PROCESS] Тема: '{subject_text[:50]}...'")
            
            # Проверка .ics
            ics_content = None
            for part in msg.walk():
                content_type = part.get_content_type()
                filename = part.get_filename()
                if content_type == 'text/calendar' or (filename and filename.lower().endswith('.ics')):
                    ics_content = part.get_payload(decode=True)
                    break
            
            if ics_content:
                print("[PROCESS] → Найден .ics файл")
                ics_string = ics_content.decode('utf-8', errors='ignore')
                uid = next((line.split(':', 1)[1].strip() for line in ics_string.split('\n') if line.startswith('UID:')), f'event-{num.decode()}')
                
                event_url = f'{target_calendar_url}{uid}.ics'
                print(f"[ICLOUD PUT] Отправка .ics события...")
                put_response = requests.put(event_url, headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'text/calendar; charset=utf-8', 'If-None-Match': '*'}, auth=auth, data=ics_string.encode('utf-8'), timeout=30)
                
                if put_response.status_code in [201, 204]:
                    mail.store(num, '+FLAGS', '\\Seen')
                    processed_count += 1
                    print(f"[ICLOUD PUT] ✅ Успешно (HTTP {put_response.status_code})")
                else:
                    print(f"[ICLOUD PUT] ❌ Ошибка сохранения .ics: HTTP {put_response.status_code}")
            
            # Проверка текстовых писем
            elif ASSIGNMENT_SUBJECT_PATTERN.lower() in subject_text.lower():
                print("[PROCESS] → Подходит под паттерн заданий. Парсинг...")
                body_text = ''
                html_content = ''
                
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == 'text/plain':
                            payload = part.get_payload(decode=True)
                            if payload: 
                                body_text += payload.decode('utf-8', errors='ignore')
                                print(f"[PARSE] Извлечено text/plain: {len(body_text)} символов")
                        elif part.get_content_type() == 'text/html':
                            payload = part.get_payload(decode=True)
                            if payload: 
                                html_content += payload.decode('utf-8', errors='ignore')
                                print(f"[PARSE] Извлечено text/html: {len(html_content)} символов")
                else:
                    payload = msg.get_payload(decode=True)
                    if payload: 
                        body_text = payload.decode('utf-8', errors='ignore')
                        print(f"[PARSE] Извлечено тело письма: {len(body_text)} символов")
                
                print(f"[PARSE] Запуск parse_assignment_email...")
                assignments, parse_log = parse_assignment_email(body_text, html_content)
                
                # НОВОЕ: Логируем результаты парсинга
                print(f"[PARSE] Результат: найдено {len(assignments)} заданий")
                for log in parse_log:
                    print(f"[PARSE] {log}")
                
                if not assignments:
                    print(f"[PARSE] ⚠️ ВНИМАНИЕ: Парсер не нашел заданий в тексте!")
                    print(f"[PARSE] Первые 200 символов body_text: '{body_text[:200]}'")
                
                for assignment in assignments:
                    ics_content, uid = generate_ics_for_assignment(assignment)
                    event_url = f'{target_calendar_url}{uid}.ics'
                    print(f"[ICLOUD PUT] Отправка события: '{assignment['task_name']}'...")
                    put_response = requests.put(event_url, headers={'User-Agent': 'Mozilla/5.0', 'Content-Type': 'text/calendar; charset=utf-8', 'If-None-Match': '*'}, auth=auth, data=ics_content.encode('utf-8'), timeout=30)
                    
                    if put_response.status_code in [201, 204]:
                        ics_count += 1
                        print(f"[ICLOUD PUT] ✅ Успешно (HTTP {put_response.status_code})")
                    else:
                        print(f"[ICLOUD PUT] ❌ Ошибка создания: HTTP {put_response.status_code}")
                
                mail.store(num, '+FLAGS', '\\Seen')
                processed_count += 1
            else:
                print("[PROCESS] → Не подходит под паттерн, пропускаем")
                
        except Exception as e:
            print(f"[PROCESS] ❌ Ошибка обработки письма #{num.decode()}: {str(e)}")
            import traceback
            print(f"[PROCESS] Stack trace: {traceback.format_exc()}")
            continue

    print("[END] Выход из IMAP и успешное завершение функции.")
    mail.logout()
    
    report = f"✅ Обработано писем: {processed_count}. Создано событий из текста: {ics_count}"
    return {'statusCode': 200, 'body': report}