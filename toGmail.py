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

# [v1.3 업데이트 내역]
# 1. 자동 업데이트 기능 추가 (GitHub 원격 버전 체크 및 자가 파일 교체)
# 2. 매일 새벽 4시 정기 종료 로직 추가 (Cron에 의한 재기동 및 자동 업데이트 유도)

# [v1.2 업데이트 내역]
# 1. 메일 제목의 HTML 엔티티(&#40; 등) 디코딩 처리 추가

# [v1.1 업데이트 내역]
# 1. 지메일 import 에러 발생 시 재처리하도록 로직 순서 개선.
#   - (이전) imap 메일 읽기 => 휴지통 이동 => 지메일 import
#   - (개선) imap 메일 읽기 => 지메일 import => 휴지통 이동

TO_GMAIL_VERSION="v1.3"
COMMON_CREDENTIALS = "client_secret.json"
TIMEOUT = 60
socket.setdefaulttimeout(TIMEOUT)

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

def decode_mime_header(text):
    """MIME 인코딩된 헤더를 한글 텍스트로 변환하는 공통 함수"""
    if not text: return "Unknown"
    text_str = str(text)
    decoded = []
    try:
        for part, encoding in decode_header(text_str):
            if isinstance(part, bytes):
                decoded.append(part.decode(encoding or 'utf-8', errors='replace'))
            else:
                decoded.append(str(part))
    except: 
        return text_str
    return html.unescape("".join(decoded))

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
          <tr><td style="padding: 10px; background: #fafafa; font-weight: bold;">제목</td><td style="padding: 10px;">{pretty_subject}</td></tr>
          <tr><td style="padding: 10px; background: #fafafa; font-weight: bold;">발신</td><td style="padding: 10px;">{pretty_sender}</td></tr>
          <tr><td style="padding: 10px; background: #fafafa; font-weight: bold; color: #d93025;">사유</td><td style="padding: 10px; color: #d93025;">{reason}</td></tr>
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
        
    msg_uids = messages[0].split()
    
    for uid_bytes in msg_uids:
        uid = uid_bytes.decode()
        raw_content = None
        email_msg = None
        subject = ""
        sender = ""

        try:
            # 1. 메일 데이터 가져오기 (FETCH)
            status, msg_data = server.uid('FETCH', uid, '(RFC822)')
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

            # 실제 Gmail에 업로드되는 원본 메일 객체의 제목도 디코딩된 정상 텍스트로 교체
            if 'Subject' in email_msg:
                email_msg.replace_header('Subject', Header(subject, 'utf-8').encode())
            modified_raw_content = email_msg.as_bytes()

            # 2. Gmail로 가져오기 (Import)
            try:
                encoded_raw = base64.urlsafe_b64encode(modified_raw_content).decode()
                
                request = service.users().messages().import_(
                    userId='me',
                    internalDateSource='dateHeader',
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