import os, imaplib, base64, socket, sys, time, re, html, logging, urllib.request
imaplib._MAXLINE = 10 * 1024 * 1024 # 기본값 2048을 10MB로 상향 (긴 줄이 포함된 메일 처리용)
from datetime import datetime
from logging.handlers import RotatingFileHandler
from email import message_from_bytes
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header, Header
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from config import ACCOUNTS_CONFIG
try:
    from config import AUTO_UPDATE
except ImportError:
    AUTO_UPDATE = 'no'

# [v1.4 업데이트 내역]
# (2026-07-08)
# 1. 에러 리포트 메일 HTML 주입(인젝션) 방지 처리 추가.
#   - 문제: 리포트 본문에 삽입되는 제목/발신/사유는 외부(발신자)가 정하는 값인데,
#           decode_mime_header()가 html.unescape()로 끝나 HTML 태그가 그대로 렌더링될 수 있었음.
#           (예: 제목에 <a>/<img> 를 심어 피싱 링크·추적 픽셀 삽입 가능)
#   - 조치: 본문 삽입용 safe_subject/safe_sender/safe_reason 변수를 만들어 html.escape() 적용.
#   - 유지: msg['Subject'] 헤더와 로그는 텍스트라 이스케이프하지 않고 pretty_* 원문 사용.
# (2026-07-11) — 일반 메일 안전성 개선(스팸 대응 코드의 전역 부작용 제거)
# 2. 제목 HTML 엔티티 복원을 '숫자형(&#40; 등)'만으로 한정.
#   - 문제: html.unescape()가 모든 제목에 적용돼, &amp;/&lt; 등이 든 정상 제목이 변형될 수 있었음.
#   - 조치: named 엔티티는 두고 숫자형 문자참조만 복원(_unescape_numeric_entities).
# 3. 제목 재작성+재직렬화(as_bytes)를 '비표준 charset/숫자엔티티 제목'에만 적용.
#   - 문제: 모든 메일을 파싱→재직렬화해 전송, 정상 메일의 DKIM/서명이 깨질 여지가 있었음.
#   - 조치: subject_needs_rewrite()가 True인 메일만 재작성, 표준 메일은 원본 raw 그대로 전송(무손실).

# [v1.3 업데이트 내역]
# 1. 자동 업데이트 기능 추가 (GitHub 원격 버전 체크 및 자가 파일 교체)
# 2. 매일 새벽 4시 정기 종료 로직 추가 (Cron에 의한 재기동 및 자동 업데이트 유도)

# [v1.2 업데이트 내역]
# 1. 메일 제목의 HTML 엔티티(&#40; 등) 디코딩 처리 추가

# [v1.1 업데이트 내역]
# 1. 지메일 import 에러 발생 시 재처리하도록 로직 순서 개선.
#   - (이전) imap 메일 읽기 => 휴지통 이동 => 지메일 import
#   - (개선) imap 메일 읽기 => 지메일 import => 휴지통 이동

TO_GMAIL_VERSION="v1.4"
COMMON_CREDENTIALS = "client_secret.json"
TIMEOUT = 60
socket.setdefaulttimeout(TIMEOUT)

# [원인분석용] 메일 내용 전체 로깅 스위치.
# 제목/본문 깨짐 등 디버깅이 필요할 때만 True로 켠다.
# True면 본문 전문이 toGmail.log에 남으므로(개인정보) 평소에는 반드시 False 유지.
DUMP_MAIL_CONTENT = False

# ==========================================
# [설정 구역] 로깅 및 실행 모드 설정
# ==========================================
run_once = len(sys.argv) > 1

logger = logging.getLogger('toGmail')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

if run_once:
    # 1회성 실행: 화면(콘솔)에만 로그 출력
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
else:
    # 무한 루프: 파일에만 로그 출력 (5MB 단위 5개 순환)
    file_handler = RotatingFileHandler('toGmail.log', maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
# ==========================================

def check_and_update():
    """GitHub에서 최신 버전을 확인하고, 버전이 다르면 다운로드 및 교체 후 종료합니다."""
    update_url = "https://raw.githubusercontent.com/gomclass/toGmail/refs/heads/main/toGmail.py"
    try:
        req = urllib.request.Request(update_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode('utf-8')
        
        # 정규식으로 TO_GMAIL_VERSION 추출
        match = re.search(r'TO_GMAIL_VERSION\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            remote_version = match.group(1)
            if remote_version != TO_GMAIL_VERSION:
                logger.info(f"새 버전({remote_version})을 발견했습니다. (현재: {TO_GMAIL_VERSION})업데이트를 진행합니다.")
                
                # 문법 검사 (오류 발생 시 예외 발생하여 덮어쓰기 방지)
                compile(content, '<string>', 'exec')
                
                current_file = os.path.abspath(__file__)
                temp_file = current_file + ".tmp"
                with open(temp_file, 'w', encoding='utf-8') as f:
                    f.write(content)
                os.replace(temp_file, current_file) # 원본 덮어쓰기 (원자적 교체)
                
                logger.info("업데이트 파일 교체가 완료되었습니다. 적용을 위해 프로그램을 종료(재시작)합니다.")
                sys.exit(0)
    except Exception as e:
        logger.error(f"업데이트 확인 중 오류 발생 (진행 무시): {e}")

def get_gmail_service(token_filename):
    creds = None
    # modify 권한은 import 뿐만 아니라 메일 발송(send) 권한도 포함합니다.
    scopes = ['https://www.googleapis.com/auth/gmail.modify']
    token_path = token_filename
    
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, scopes)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(COMMON_CREDENTIALS, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    
    return build('gmail', 'v1', credentials=creds, static_discovery=False)

# 스팸 메일 등에서 쓰는 비표준 인코딩 이름 → 표준 이름 보정 맵
ENCODING_MAP = {
    'unicode': 'utf-16-be',
    'ms1361': 'cp1361',
    'ks_c_5601-1987': 'cp949',
    'ksc5601': 'cp949',
}
# 위 비표준 charset은 Gmail이 원본 그대로는 못 읽으므로, 이런 제목만 재작성 대상으로 삼는다.
NONSTANDARD_CHARSETS = set(ENCODING_MAP)
# 숫자형 HTML 문자참조(&#40; / &#xAB;)만 매칭. named 엔티티(&amp; &lt; 등)는 제외.
_NUMERIC_ENTITY_RE = re.compile(r'&#(?:x[0-9a-fA-F]+|\d+);')
# RFC2047 인코딩드워드에서 charset 부분만 추출( =?charset?B?...?= )
_ENCODED_WORD_CHARSET_RE = re.compile(r'=\?([^?]+)\?[bBqQ]\?')

def _unescape_numeric_entities(s):
    """숫자형 HTML 문자참조(&#40; · &#xAB;)만 복원한다.
    named 엔티티(&amp; &lt; &gt; 등)는 정상 메일 제목과 충돌 위험이 있어 건드리지 않는다."""
    return _NUMERIC_ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), s)

def subject_needs_rewrite(raw_subject):
    """Gmail이 '원본 그대로'는 제대로 표시하지 못하는 제목인지 판단한다.
    - 비표준 charset(unicode/ms1361/ksc5601 등) 인코딩드워드를 포함하거나
    - 숫자형 HTML 엔티티(&#40; 등)를 포함하면 재작성이 필요.
    그 외 표준 메일은 원본 raw 바이트를 그대로 전송한다(무손실 · 서명 보존)."""
    s = str(raw_subject)
    for cs in _ENCODED_WORD_CHARSET_RE.findall(s):
        if cs.strip().lower() in NONSTANDARD_CHARSETS:
            return True
    return bool(_NUMERIC_ENTITY_RE.search(s))

def decode_mime_header(text):
    """MIME 인코딩된 헤더를 한글 텍스트로 변환하는 공통 함수"""
    if not text: return "Unknown"
    text_str = str(text)
    decoded = []

    try:
        for part, encoding in decode_header(text_str):
            if isinstance(part, bytes):
                if encoding:
                    encoding = encoding.lower()
                    encoding = ENCODING_MAP.get(encoding, encoding)
                try:
                    decoded.append(part.decode(encoding or 'utf-8', errors='replace'))
                except LookupError:
                    # 파이썬이 지원하지 않는 알 수 없는 인코딩일 경우 무시하고 utf-8로 강제 시도
                    decoded.append(part.decode('utf-8', errors='replace'))
            else:
                decoded.append(str(part))
    except:
        return text_str
    # named 엔티티는 그대로 두고 숫자형 문자참조만 복원(정상 제목 변형 방지)
    return _unescape_numeric_entities("".join(decoded))

def log_mail_content(acc_id, uid, email_msg, raw_subject, subject, raw_sender, sender):
    """[원인분석용] 메일의 원본 헤더/디코딩 결과/본문 전체를 로그에 상세 기록한다.
    제목·본문 깨짐(charset/인코딩) 원인 파악을 위해 raw 값과 charset 정보를 함께 남긴다."""
    try:
        lines = []
        lines.append(f"[{acc_id}] ===== 메일 내용 덤프 시작 (UID: {uid}) =====")
        lines.append(f"[{acc_id}] Raw Subject 헤더 : {raw_subject!r}")
        lines.append(f"[{acc_id}] 디코딩 Subject   : {subject}")
        lines.append(f"[{acc_id}] Raw From 헤더    : {raw_sender!r}")
        lines.append(f"[{acc_id}] 디코딩 From      : {sender}")
        lines.append(f"[{acc_id}] Date             : {email_msg.get('Date', '')}")
        lines.append(f"[{acc_id}] Content-Type     : {email_msg.get('Content-Type', '')}")
        lines.append(f"[{acc_id}] Content-Transfer-Encoding: {email_msg.get('Content-Transfer-Encoding', '')}")

        part_index = 0
        for part in email_msg.walk():
            ctype = part.get_content_type()
            charset = part.get_content_charset()
            cte = part.get('Content-Transfer-Encoding', '')
            disp = part.get_content_disposition()
            if part.is_multipart():
                lines.append(f"[{acc_id}] --- part#{part_index} (multipart) type={ctype}")
                part_index += 1
                continue
            lines.append(f"[{acc_id}] --- part#{part_index} type={ctype} charset={charset} cte={cte} disp={disp}")
            if ctype.startswith('text/') and disp != 'attachment':
                try:
                    payload = part.get_payload(decode=True)
                    if payload is not None:
                        text = payload.decode(charset or 'utf-8', errors='replace')
                    else:
                        text = str(part.get_payload())
                    lines.append(f"[{acc_id}] 본문(part#{part_index}) ↓↓↓\n{text}\n[{acc_id}] 본문(part#{part_index}) ↑↑↑")
                except Exception as be:
                    lines.append(f"[{acc_id}] 본문 디코딩 실패(part#{part_index}): {be}")
            else:
                lines.append(f"[{acc_id}] (첨부/비텍스트 파트: filename={part.get_filename()}, 본문 생략)")
            part_index += 1
        lines.append(f"[{acc_id}] ===== 메일 내용 덤프 끝 (UID: {uid}) =====")
        logger.info("\n".join(lines))
    except Exception as e:
        logger.error(f"[{acc_id}] 메일 내용 덤프 실패(UID: {uid}): {e}")

def get_trash_folder(server):
    """IMAP 서버의 휴지통 폴더명을 자동 탐색합니다."""
    try:
        status, response = server.list()
        if status == 'OK':
            # 1. \Trash 플래그가 있는 폴더 찾기
            for line in response:
                if not line: continue
                line_str = line.decode('ascii', errors='ignore')
                if '\\Trash' in line_str:
                    match = re.search(r'("[^"]+"|\S+)$', line_str.strip())
                    if match:
                        return match.group(1).strip('"')
            # 2. 플래그가 없다면 이름(Trash)으로 유추
            for line in response:
                if not line: continue
                line_str = line.decode('ascii', errors='ignore')
                match = re.search(r'("[^"]+"|\S+)$', line_str.strip())
                if match and match.group(1).strip('"').lower() == 'trash':
                    return match.group(1).strip('"')
    except Exception:
        pass
    return "Trash"

def send_error_report(service, my_email, uid, subject, sender, reason, acc_id):
    """에러 발생 시 클릭 가능한 링크가 포함된 HTML 리포트 발송"""
    bizmeka_url = "https://ezwebmail.bizmeka.com/mail/list.do"
    
    # 전달받은 subject, sender는 메일 추출 시 1차 디코딩되지만, 안전을 위해 한 번 더 처리
    pretty_subject = decode_mime_header(subject)
    pretty_sender = decode_mime_header(sender)

    # HTML 리포트 본문 삽입용: HTML 주입 방지를 위해 이스케이프 (헤더/로그용 pretty_* 는 원문 유지)
    safe_subject = html.escape(pretty_subject)
    safe_sender = html.escape(pretty_sender)
    safe_reason = html.escape(str(reason))

    # 메일 객체 생성 (HTML을 지원하기 위해 MIMEMultipart 사용)
    msg = MIMEMultipart('alternative')
    # 지메일 목록에서도 한글로 보이도록 제목도 인코딩하여 설정
    msg['Subject'] = Header(f"[Import Error] {pretty_subject}", 'utf-8').encode()
    msg['To'] = my_email
    msg['From'] = my_email

    # [수정] HTML 서식: 항목 열 너비 증가 및 스타일 보강
    html_content = f"""
    <html>
      <body style="font-family: sans-serif;">
        <h3 style="color: #d93025;">⚠️ Gmail Import 에러 리포트</h3>
        <table border="1" style="border-collapse: collapse; width: 100%; max-width: 600px; table-layout: fixed;">
          <colgroup><col style="width: 25%;"><col style="width: 75%;"></colgroup>
          <tr><td style="padding: 10px; background: #fafafa; font-weight: bold;">제목</td><td style="padding: 10px;">{safe_subject}</td></tr>
          <tr><td style="padding: 10px; background: #fafafa; font-weight: bold;">발신</td><td style="padding: 10px;">{safe_sender}</td></tr>
          <tr><td style="padding: 10px; background: #fafafa; font-weight: bold; color: #d93025;">사유</td><td style="padding: 10px; color: #d93025;">{safe_reason}</td></tr>
        </table>
        <br>
        <a href="{bizmeka_url}" style="background-color: #1a73e8; color: white; padding: 12px 24px; text-decoration: none; border-radius: 4px; font-weight: bold; display: inline-block;">웹메일 바로가기</a>
      </body>
    </html>
    """
    
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))
    
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    
    # [변경 포인트] import_() 대신 send() 사용
    try:
        service.users().messages().send(
            userId='me', 
            body={'raw': raw}
        ).execute()
        logger.info(f"[{acc_id}] - 에러 리포트 발송 완료(Subject: {pretty_subject[:20]}...)")
    except Exception as e:
        logger.error(f"[{acc_id}] !!! 에러 리포트 발송 실패: {e}")

def process_unread(server, service, acc_id, my_email, trash_folder, success_action, error_action):
    """INBOX에서 읽지 않은 메일을 검색하여 처리합니다."""
    try:
        server.select("INBOX")
        status, messages = server.uid('SEARCH', 'UNSEEN')
    except Exception as e:
        logger.error(f"[{acc_id}] SEARCH UNREAD 실패: {e}")
        raise
        
    if status != 'OK' or not messages[0]:
        return
        
    msg_uids = messages[0].split()[:3]
    
    for uid_bytes in msg_uids:
        uid = uid_bytes.decode()
        raw_content = None
        email_msg = None
        subject = ""
        sender = ""

        try:
            # 1. 메일 데이터 가져오기 (FETCH)
            # BODY.PEEK[]를 사용하여 메일을 읽을 때 서버에서 자동으로 읽음 처리되는 것을 방지합니다.
            status, msg_data = server.uid('FETCH', uid, '(BODY.PEEK[])')
            if status != 'OK':
                logger.warning(f"[{acc_id}] UID {uid} FETCH 실패, 건너뜁니다.")
                continue
                
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    raw_content = response_part[1]
                    
            if not raw_content:
                logger.warning(f"[{acc_id}] UID {uid} 내용이 비어있어 건너뜁니다.")
                continue
                
            email_msg = message_from_bytes(raw_content)
            raw_subject = email_msg.get('Subject', '')
            subject = decode_mime_header(raw_subject)
            raw_sender = email_msg.get('From', '')
            sender = decode_mime_header(raw_sender)

            # [원인분석용] 제목/본문 깨짐 원인 파악을 위해 메일 내용 전체를 로그에 기록
            # (평소엔 DUMP_MAIL_CONTENT=False 로 비활성화)
            if DUMP_MAIL_CONTENT:
                log_mail_content(acc_id, uid, email_msg, raw_subject, subject, raw_sender, sender)

            # 제목 재작성이 필요한 '비표준' 메일만 파싱·재직렬화하고,
            # 표준 메일은 원본 raw 바이트를 그대로 전송한다(바이트 무손실 · DKIM/서명 보존).
            if subject_needs_rewrite(raw_subject) and 'Subject' in email_msg:
                email_msg.replace_header('Subject', Header(subject, 'utf-8').encode())
                modified_raw_content = email_msg.as_bytes()
            else:
                modified_raw_content = raw_content

            # 2. Gmail로 가져오기 (Import)
            try:
                encoded_raw = base64.urlsafe_b64encode(modified_raw_content).decode()
                
                request = service.users().messages().import_(
                    userId='me',
                    internalDateSource='dateHeader',
                    neverMarkSpam=True,  # 스팸/의심(피싱) 분류 판정을 무시하여 '의심 메시지' 배너 방지
                    body={'raw': encoded_raw, 'labelIds': ['INBOX', 'UNREAD']}
                )
                request.uri += '&processForFilters=true'
                request.execute()
                
            except HttpError as e:
                # Gmail API가 반환한 특정 오류 처리
                if e.resp.status == 400: # 복구 불가능한 '불량 메일' 오류
                    reason = e._get_reason()
                    logger.info(f"[{acc_id}] - 스킵(불량메일): {subject[:40]}... 이유: {reason}")
                    
                    try:
                        send_error_report(service, my_email, uid, subject, sender, reason, acc_id)
                    except Exception as report_err:
                        logger.error(f"[{acc_id}]   [!] 에러 리포트 발송 실패: {report_err}")
                    
                    # 불량 메일은 재시도하지 않도록 error_action 수행
                    if error_action == "읽음처리":
                        server.uid('STORE', uid, '+FLAGS', '(\\Seen)')
                        logger.info(f"[{acc_id}] - 불량 메일 읽음 처리 완료.")
                    elif error_action == "휴지통이동":
                        copy_status, _ = server.uid('COPY', uid, f'"{trash_folder}"')
                        if copy_status == 'OK':
                            server.uid('STORE', uid, '+FLAGS', '(\\Deleted)')
                            server.expunge()
                            logger.info(f"[{acc_id}] - 불량 메일 휴지통 이동 완료.")
                    
                    continue # 다음 메일로 넘어감
                else:
                    # 400 외 다른 HTTP 오류(5xx 등)는 일시적일 수 있으므로,
                    # 예외를 상위로 보내 프로그램을 종료하고 재시도하도록 함
                    raise

            # 3. 가져오기 성공 시 원본 서버에서 후처리 (success_action)
            if success_action == "휴지통이동":
                copy_status, _ = server.uid('COPY', uid, f'"{trash_folder}"')
                if copy_status == 'OK':
                    server.uid('STORE', uid, '+FLAGS', '(\\Deleted)')
                    server.expunge()
                    logger.info(f"[{acc_id}] - 성공(휴지통 이동): {subject[:40]}")
                else:
                    # 복사 실패 시 읽음 처리로 대체
                    server.uid('STORE', uid, '+FLAGS', '(\\Seen)')
                    logger.warning(f"[{acc_id}] - 성공(휴지통 복사 실패로 읽음 처리만 됨): {subject[:40]}")
            elif success_action == "읽음처리":
                server.uid('STORE', uid, '+FLAGS', '(\\Seen)')
                logger.info(f"[{acc_id}] - 성공(읽음 처리): {subject[:40]}")
                
        except Exception as e:
            # FETCH, Import(HttpError 400 제외), success_action 등 모든 단계의 오류를 여기서 잡음
            # Broken pipe 같은 연결 오류도 여기에 해당됨
            # 오류 발생 시 상위로 예외를 전달하여 main 루프에서 프로그램을 종료하도록 함
            logger.error(f"[{acc_id}] 메일(UID: {uid}) 처리 중 오류 발생. 상위로 전달: {e}")
            raise # main()의 except 블록으로 예외를 전달

def main():
    if not run_once:
        if str(AUTO_UPDATE).upper() == 'YES':
            check_and_update()
    logger.info(f"toGmail {TO_GMAIL_VERSION} 시작(순차/Polling 모드).")
    
    # 4시 무한 재시작 방지를 위해, 스크립트가 기동된 날짜를 기록
    last_checked_day = datetime.now().day

    while True:
        # 하루 1번, 새벽 4시가 되면 정기 업데이트 체크를 위해 프로그램 종료
        # (세션 재연결 주기인 3분마다 한 번씩만 체크하여 자연스러운 분산 종료 효과 발생)
        now = datetime.now()
        if now.hour == 4 and now.day != last_checked_day:
            logger.info("새벽 4시 정기 재시작(업데이트 체크용)을 위해 프로그램을 종료합니다.")
            sys.exit(0)

        connections = []
        session_start_time = time.time()
        
        # 1. 순차적으로 모든 계정 연결 수립
        for config in ACCOUNTS_CONFIG:
            acc_id = config["id"]
            token_file = config["gmail"]["token_file"]
            
            if not os.path.exists(token_file):
                logger.info(f"[{acc_id}] 인증 토큰({token_file})이 없습니다. 인증 절차를 시작합니다.")
                get_gmail_service(token_file)
                logger.info(f"[{acc_id}] 새 인증 토큰이 생성되었습니다.")
                
            logger.info(f"[{acc_id}] IMAP/Gmail 서버 연결 중...")
            try:
                server = imaplib.IMAP4_SSL(config["imap"]["host"])
                server.login(config["imap"]["user"], config["imap"]["pass"])
                trash_folder = get_trash_folder(server)
                server.select("INBOX")
                
                service = get_gmail_service(token_file)
                profile = service.users().getProfile(userId='me').execute()
                my_email = profile.get('emailAddress')
                
                connections.append({
                    "acc_id": acc_id,
                    "server": server,
                    "service": service,
                    "trash_folder": trash_folder,
                    "my_email": my_email,
                    "polling_time": config.get("polling_time", 60),
                    "success_action": config.get("success_action", "휴지통이동"),
                    "error_action": config.get("error_action", "읽음처리"),
                    "next_check": time.time() # 시작하자마자 첫 검사를 위해 현재 시간 기록
                })
                logger.info(f"[{acc_id}] 연결 완료.")
            except Exception as e:
                logger.error(f"[{acc_id}] 초기 연결 실패: {e}")
                os._exit(1)
                
        # 2. 순차 반복 처리 루프
        while True:
            current_time = time.time()
            
            # 3분(180초)이 경과했으면 안전하게 연결을 종료하고 재접속 (run_once 모드가 아닐 때)
            if not run_once and (current_time - session_start_time) >= 180:
                logger.info("세션 유지 시간(3분) 경과. 재접속합니다.")
                break
                
            for conn in connections:
                # 해당 계정의 검사 주기가 돌아왔는지 확인
                if current_time >= conn["next_check"]:
                    try:
                        process_unread(
                            conn["server"], 
                            conn["service"], 
                            conn["acc_id"], 
                            conn["my_email"], 
                            conn["trash_folder"],
                            conn["success_action"],
                            conn["error_action"]
                        )
                    except Exception as e:
                        logger.error(f"[{conn['acc_id']}] 처리 중 치명적 오류 발생: {e}")
                        os._exit(1)
                    
                    # 처리 완료 후, 현재 시간 기준으로 다음 검사 시간 갱신
                    conn["next_check"] = time.time() + conn["polling_time"]
                    
            if run_once:
                logger.info("run_once 모드: 전체 계정 처리가 완료되어 프로그램을 종료합니다.")
                break
                
            # 모든 계정 중 가장 먼저 돌아오는 다음 검사 시간까지 대기
            sleep_time = min(c["next_check"] for c in connections) - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                time.sleep(1) # 시간 계산 오차 방지용 최소 대기
            
        # 3. 종료 시 연결 해제
        for conn in connections:
            try:
                conn["server"].close()
                conn["server"].logout()
            except:
                pass
                
        if run_once:
            break
            
    logger.info("프로그램 종료.")

if __name__ == "__main__":
    main()