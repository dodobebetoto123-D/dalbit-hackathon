# pi_server.py — 독거노인 낙상 감지 시스템, 서버용 라즈베리파이 코드
# 아키텍처: [아두이노1:센서] -> [아두이노2:출력] -> [Pi 중계] --WiFi--> [Pi 서버(이 파일)]
# 실행: python3 pi_server.py (포트 5000)
# 작성자: 2026 달빛연합 해커톤 팀 '봄베'

import os
import json
import threading
from datetime import datetime
from urllib import request as urlrequest

from flask import Flask, request, jsonify

# ---------------- 사전 등록 정보 (시연용 더미 데이터) ----------------
REGISTRY = {
    "name": "김OO (시연용)",
    "address": "대구광역시 OO구 OO로 123 (시연용)",
    "doorlock_password": "1234* (시연용)",
    "guardian": "010-0000-0000 (시연용)",
    "blood_type": "O형",
    "disease": "고혈압",
    "medicine": "혈압약",
}

# ---------------- 전역 상태 ----------------
app = Flask(__name__)

LOGS = []  # 전체 이벤트 로그 (최대 200개, 초과 시 앞에서 삭제)
LOG_LOCK = threading.Lock()  # 로그 리스트 보호용 락

STATE_LOCK = threading.Lock()  # 아래 확정/예비 상태 보호용 락
confirmed_alert = False  # 확정 경보 상태 (True면 이후 모든 /sensor 응답에 alert=True)
pending_timer = None  # 10초 예비 경보 타이머 객체
pending_info = None  # 예비 경보 관련 정보 (낙상 확률 등 SMS용)
last_sensors = {}  # 마지막으로 수신한 센서값 (대시보드 표시용)
last_fall_event = {}  # 마지막으로 수신한 낙상 이벤트 (대시보드 표시용)
last_camera = {}  # 마지막 카메라 스트림 (이미지+확률, 대시보드 표시용)

LOG_MAX = 200  # 로그 최대 보관 개수
PRE_ALERT_SEC = 10  # 예비 경보 대기 시간(초)
GROQ_MODEL = "openai/gpt-oss-120b"  # Groq 2차 판단 모델 (llama-3.3-70b-versatile는 2026-08-16 종료됨)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"  # Groq OpenAI 호환 엔드포인트


# ---------------- 유틸 함수 ----------------
def now_str():
    """현재 시각 문자열 반환 (로그/대시보드/SMS용)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add_log(kind, message, detail=None):
    """이벤트 로그 1건 추가 (스레드 안전, 최대 200개 유지)."""
    with LOG_LOCK:
        entry = {
            "timestamp": now_str(),
            "type": kind,  # sensor / fall_event / preliminary / sms / cancel / info
            "message": message,
        }
        if detail is not None:
            entry["detail"] = detail
        LOGS.append(entry)
        # 최대 개수 초과 시 앞에서 삭제
        while len(LOGS) > LOG_MAX:
            LOGS.pop(0)


def get_logs_snapshot():
    """로그 전체 복사본 반환 (읽기 시 락 충돌 방지)."""
    with LOG_LOCK:
        return [dict(e) for e in LOGS]


def check_rule_sensor(sensors):
    """1차 규칙 기반 판단 (센서용). 의심이면 (True, 이유), 아니면 (False, 이유) 반환."""
    reasons = []
    # 값은 문자열로 오므로 문자열 기준 비교 ("1"/"0")
    pir = str(sensors.get("PIR", "0")).strip()
    snd1 = str(sensors.get("SND1", "0")).strip()
    snd2 = str(sensors.get("SND2", "0")).strip()

    # 조건1: 큰 소리 + 움직임 없음
    if (snd1 == "1" or snd2 == "1") and pir == "0":
        reasons.append("큰 소리 감지(SND=1) + 움직임 없음(PIR=0)")

    # 조건2: 초음파 5개 중 2개 이상이 50cm 이하 (바닥에 누운 것으로 추정)
    # 단, 0 = 센서 미응답, 999 = 타임아웃 → 둘 다 무시. HC-SR04 유효 범위는 2~400cm
    low_count = 0
    low_list = []
    for i in range(1, 6):
        key = "US%d" % i
        try:
            v = float(str(sensors.get(key, "")).strip())
        except (ValueError, TypeError):
            continue  # 파싱 실패 값은 무시
        if 2 <= v <= 50:
            low_count += 1
            low_list.append("%s=%s" % (key, sensors.get(key)))
    if low_count >= 2:
        reasons.append("초음파 %d개 50cm 이하(%s) → 누움 추정" % (low_count, ", ".join(low_list)))

    if reasons:
        return True, " / ".join(reasons)
    return False, "정상 (1차 규칙 해당 없음)"


def ask_llm_fall(summary):
    """Groq API로 2차 판단. FALL이면 True, OK면 False. 실패 시 True(1차 결과 폴백=의심 유지)."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        # 키가 없으면 API 호출 없이 1차 결과 그대로 사용 (폴백)
        print("[LLM 폴백] GROQ_API_KEY 없음 → 1차 판단(의심) 유지")
        return True
    # LLM 프롬프트: 센서 요약 + 한 단어 강제
    prompt = (
        "독거노인 낙상 감지 시스템의 2차 판단이다.\n"
        "센서 요약: %s\n"
        "위 상황이 낙상이면 FALL, 아니면 OK — 한 단어로만 답해라" % summary
    )
    print("[LLM 프롬프트]\n%s" % prompt, flush=True)
    body = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 5,
    }
    try:
        req = urlrequest.Request(
            GROQ_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
            method="POST",
        )
        # 타임아웃 5초 (스펙 요구)
        with urlrequest.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        text = data["choices"][0]["message"]["content"].strip().upper()
        print("[LLM 응답] %s" % text, flush=True)
        is_fall = "FALL" in text
        # 판단 과정을 대시보드 로그에 기록
        add_log("llm", "LLM 2차 판단: %s" % ("FALL(낙상)" if is_fall else "OK(정상)"),
                {"prompt": prompt, "response": text, "summary": summary})
        return is_fall
    except Exception as e:
        # API 실패/타임아웃 시 1차 판단 결과 그대로 사용 (폴백)
        # HTTPError면 응답 본문도 출력 (디버깅용)
        detail = ""
        try:
            import urllib.error
            if isinstance(e, urllib.error.HTTPError):
                detail = " | 응답본문: " + e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        print("[LLM 폴백] API 실패(%s%s) → 1차 판단(의심) 유지" % (e, detail))
        return True


def send_mock_sms(fall_prob_text):
    """모의 SMS 발송 (실제 문자 API 호출 절대 금지. 콘솔 로그 + 대시보드 로그만)."""
    ts = now_str()
    # 발송 대상 3곳 (모의)
    targets = ["행정복지센터(모의)", "119(모의)", "지정 보호자(모의, %s)" % REGISTRY["guardian"]]
    # SMS 본문: 발생 시각, 주소, 도어락 비번, 낙상 확률, 혈액형·지병·복용약
    content = (
        "[낙상 감지 모의 문자] 발생 시각: %s / 대상: %s / 주소: %s / "
        "도어락 비밀번호: %s / 낙상 확률: %s / 혈액형: %s / 지병: %s / 복용약: %s"
        % (
            ts,
            REGISTRY["name"],
            REGISTRY["address"],
            REGISTRY["doorlock_password"],
            fall_prob_text,
            REGISTRY["blood_type"],
            REGISTRY["disease"],
            REGISTRY["medicine"],
        )
    )
    # 콘솔에 모의 SMS 로그 출력
    print("[모의 SMS] 대상: %s" % ", ".join(targets))
    print("[모의 SMS] 내용: %s" % content)
    # 대시보드/로그용으로 저장
    add_log("sms", "모의 SMS 발송 → %s" % ", ".join(targets), {"content": content})


def start_preliminary_alert(reason, fall_prob_text):
    """10초 예비 경보 타이머 시작. 이미 진행 중이면 중복 시작하지 않음."""
    global pending_timer, pending_info
    with STATE_LOCK:
        if pending_timer is not None and pending_timer.is_alive():
            return False  # 이미 예비 경보 진행 중
        pending_info = {"reason": reason, "fall_prob": fall_prob_text}
        # 10초 후 확정 콜백 예약
        pending_timer = threading.Timer(PRE_ALERT_SEC, confirm_alert)
        pending_timer.daemon = True
        pending_timer.start()
        return True


def confirm_alert():
    """10초 경과 후 취소가 없으면 확정: 모의 SMS 발송 + confirmed_alert=True 저장."""
    global confirmed_alert, pending_timer, pending_info
    with STATE_LOCK:
        info = pending_info or {}
        pending_timer = None
        pending_info = None
        confirmed_alert = True  # 확정 상태 저장 (이후 모든 /sensor 응답에 alert=True)
    fall_prob_text = str(info.get("fall_prob", "미상"))
    reason = str(info.get("reason", ""))
    add_log("preliminary", "예비 경보 확정 (10초 경과, 취소 없음) → 모의 SMS 발송", {"reason": reason})
    send_mock_sms(fall_prob_text)


# ---------------- 엔드포인트 ----------------
@app.route("/", methods=["GET"])
def dashboard():
    """대시보드 HTML (5초 자동 새로고침, 시연용 다크 테마)."""
    logs = get_logs_snapshot()
    with STATE_LOCK:
        alert_state = confirmed_alert
        pending_alive = pending_timer is not None and pending_timer.is_alive()
        sensors_snapshot = dict(last_sensors)
        fall_snapshot = dict(last_fall_event)
        cam_snapshot = dict(last_camera)

    # 종류별 로그 분리 (최신이 위로)
    llm_logs = [e for e in logs if e.get("type") == "llm"][::-1][:5]
    sms_logs = [e for e in logs if e.get("type") == "sms"][::-1][:5]

    def esc(v):
        return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 카메라 이미지
    if cam_snapshot.get("image"):
        cam_img_tag = '<img src="data:image/jpeg;base64,%s" style="width:100%%;border-radius:8px;display:block">' % cam_snapshot["image"]
        probs = cam_snapshot.get("probabilities", {})
        fall_p = float(probs.get("fall", 0))
        fall_pct = int(fall_p * 100)
        fg_ratio = cam_snapshot.get("fg_ratio", 0)
        try:
            fg_pct = int(float(fg_ratio) * 100)
        except (ValueError, TypeError):
            fg_pct = 0
        # 낙상 감지 오버레이
        if fall_p >= 0.5:
            overlay = '<div style="position:absolute;top:12px;left:12px;background:#ff8c1a;color:#fff;padding:6px 14px;border-radius:6px;font-weight:bold;font-size:15px;">낙상 감지</div>'
            border_style = "border:3px solid #ff8c1a;"
        else:
            overlay = ""
            border_style = "border:3px solid transparent;"
        cam_box = '<div style="position:relative;%sborder-radius:10px;overflow:hidden;">%s%s</div>' % (border_style, cam_img_tag, overlay)
    else:
        cam_box = '<div style="background:#2a2a3e;border-radius:10px;padding:60px 20px;text-align:center;color:#888;">카메라 스트림 없음</div>'
        fall_pct = 0
        fg_pct = 0

    # LLM 판단 과정
    if llm_logs:
        llm_steps = ""
        for i, e in enumerate(llm_logs[:3], 1):
            detail = e.get("detail", {}) or {}
            summary = esc(detail.get("summary", "")[:80])
            response = esc(detail.get("response", ""))
            llm_steps += '<div style="margin-bottom:10px;"><span style="color:#ff8c1a;font-weight:bold;">%d.</span> <span style="color:#ccc;">%s</span><br><span style="color:#888;font-size:13px;">→ %s</span></div>' % (i, summary, response)
    else:
        llm_steps = '<div style="color:#666;">LLM 판단 기록 없음</div>'

    # SMS 로그
    if sms_logs:
        sms_items = ""
        for e in sms_logs:
            sms_items += '<div style="margin-bottom:12px;"><span style="color:#ff8c1a;font-weight:bold;">%s</span> <span style="color:#fff;">%s</span><br><span style="color:#aaa;font-size:13px;">"%s"</span></div>' % (
                esc(e.get("timestamp", "")), esc(e.get("message", "")), esc((e.get("detail", {}) or {}).get("content", "")[:60]))
    else:
        sms_items = '<div style="color:#666;">발송된 모의 문자 없음</div>'

    # 상태 배지
    if alert_state:
        state_html = '<span style="background:#dc3545;color:#fff;padding:8px 20px;border-radius:8px;font-weight:bold;">확정 경보 발령 중</span>'
    elif pending_alive:
        state_html = '<span style="background:#ffc107;color:#000;padding:8px 20px;border-radius:8px;font-weight:bold;">예비 경보 진행 중 (10초)</span>'
    else:
        state_html = '<span style="background:#28a745;color:#fff;padding:8px 20px;border-radius:8px;font-weight:bold;">정상 모니터링 중</span>'

    html = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>골든타임 키퍼 — 실시간 대시보드</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Malgun Gothic',sans-serif;background:#1a1a2e;color:#fff;min-height:100vh;padding:20px}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
.header h1{font-size:24px;color:#ff8c1a}
.grid{display:grid;grid-template-columns:1.2fr 1fr;gap:16px;max-width:1200px;margin:0 auto}
.card{background:#252540;border-radius:12px;padding:18px;margin-bottom:16px}
.card h3{font-size:15px;color:#aaa;margin-bottom:12px;font-weight:normal}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.stat{background:#1a1a2e;border-radius:10px;padding:16px;text-align:center}
.stat .label{font-size:13px;color:#888;margin-bottom:8px}
.stat .value{font-size:38px;font-weight:bold;color:#ff8c1a}
button{background:#ff8c1a;color:#fff;border:none;padding:10px 24px;border-radius:8px;font-size:15px;cursor:pointer;font-weight:bold}
button:hover{background:#e67e00}
.footer{text-align:center;margin-top:16px;color:#666;font-size:13px}
.footer a{color:#ff8c1a}
</style>
</head>
<body>
<div class="header">
<h1>골든타임 키퍼 — 실시간 대시보드</h1>
<div>__STATE__</div>
</div>
<div class="grid">
<div>
<div class="card">
<h3>카메라 실시간 화면</h3>
__CAM_BOX__
</div>
<div class="card">
<h3>모의 SMS 발송 로그</h3>
__SMS_ITEMS__
</div>
</div>
<div>
<div class="card">
<h3>대시보드</h3>
<div class="stat-grid">
<div class="stat"><div class="label">낙상 확률</div><div class="value">__FALL_PCT__%</div></div>
<div class="stat"><div class="label">전경 비율</div><div class="value">__FG_PCT__%</div></div>
</div>
</div>
<div class="card">
<h3>LLM 판단 과정</h3>
__LLM_STEPS__
</div>
<div class="card" style="text-align:center;">
<form method="POST" action="/cancel" onsubmit="fetch('/cancel',{method:'POST'}).then(()=>location.reload());return false;">
<button type="submit">경보 취소</button>
</form>
</div>
</div>
</div>
<div class="footer">
<p>5초마다 자동 새로고침 | <a href="/log">/log JSON 보기</a></p>
<p style="margin-top:8px;">등록: __REG_INFO__</p>
</div>
</body>
</html>"""
    html = (
        html.replace("__STATE__", state_html)
        .replace("__CAM_BOX__", cam_box)
        .replace("__FALL_PCT__", str(fall_pct))
        .replace("__FG_PCT__", str(fg_pct))
        .replace("__LLM_STEPS__", llm_steps)
        .replace("__SMS_ITEMS__", sms_items)
        .replace("__REG_INFO__", esc("%s / %s / 보호자 %s" % (REGISTRY["name"], REGISTRY["address"], REGISTRY["guardian"])))
    )
    return html


@app.route("/sensor", methods=["POST"])
def sensor():
    """중계 Pi의 매 스캔 데이터 수신 → 2단계 판단 → alert 응답."""
    global last_sensors
    data = request.get_json(force=True, silent=True) or {}
    sensors = data.get("sensors", {}) or {}
    if not isinstance(sensors, dict):
        sensors = {}
    # 마지막 센서값 저장 (대시보드용)
    with STATE_LOCK:
        last_sensors = {k: str(v) for k, v in sensors.items()}
    add_log("sensor", "센서 수신: %s" % json.dumps(last_sensors, ensure_ascii=False))

    # 확정 경보 상태면 이후 모든 /sensor 응답에 alert=True 포함
    with STATE_LOCK:
        if confirmed_alert:
            return jsonify({"ok": True, "alert": True, "reason": "확정 경보 유지 중 (ALERT ON)"})

    # 1차 규칙 기반 판단
    suspect, reason1 = check_rule_sensor(sensors)
    if not suspect:
        return jsonify({"ok": True, "alert": False, "reason": reason1})

    # 2차 LLM 판단
    summary = "ANG=%s, PIR=%s, SND1=%s, SND2=%s, US1~5=%s/%s/%s/%s/%s" % (
        sensors.get("ANG", "?"), sensors.get("PIR", "?"),
        sensors.get("SND1", "?"), sensors.get("SND2", "?"),
        sensors.get("US1", "?"), sensors.get("US2", "?"), sensors.get("US3", "?"),
        sensors.get("US4", "?"), sensors.get("US5", "?"),
    )
    is_fall = ask_llm_fall("센서 1차 의심(%s). %s" % (reason1, summary))
    if not is_fall:
        add_log("info", "LLM 2차 판단: OK (낙상 아님)", {"reason1": reason1})
        return jsonify({"ok": True, "alert": False, "reason": "LLM 2차 판단: 낙상 아님"})

    # LLM이 FALL → 10초 예비 경보 타이머 시작
    started = start_preliminary_alert("센서: %s" % reason1, "센서 기반 (1차: %s)" % reason1)
    if started:
        add_log("preliminary", "예비 경보 시작 (10초 후 확정, /cancel로 취소 가능)", {"reason": reason1})
    return jsonify({"ok": True, "alert": False, "reason": "예비 경보 시작 (10초 대기 중, 1차: %s)" % reason1})


@app.route("/fall_event", methods=["POST"])
def fall_event():
    """카메라 낙상 감지 이벤트 수신 → 2단계 판단 → alert 응답."""
    global last_fall_event
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    with STATE_LOCK:
        last_fall_event = {k: str(v) if not isinstance(v, dict) else v for k, v in data.items()}
    try:
        prob = float(data.get("fall_probability", 0))
    except (ValueError, TypeError):
        prob = 0.0
    add_log("fall_event", "낙상 이벤트 수신: %s" % json.dumps(data, ensure_ascii=False))

    # 확정 경보 상태면 alert=True 유지
    with STATE_LOCK:
        if confirmed_alert:
            return jsonify({"ok": True, "alert": True, "reason": "확정 경보 유지 중 (ALERT ON)"})

    # 1차: fall_probability >= 0.5 → 의심
    if prob < 0.5:
        return jsonify({"ok": True, "alert": False, "reason": "낙상 확률 낮음(%.2f < 0.5)" % prob})

    # 2차 LLM 판단
    summary = "카메라 낙상 이벤트: top_class=%s, fall_probability=%.2f, sustained_sec=%s" % (
        data.get("top_class", "?"), prob, data.get("sustained_sec", "?")
    )
    is_fall = ask_llm_fall(summary)
    if not is_fall:
        add_log("info", "LLM 2차 판단: OK (낙상 아님)", {"event": data})
        return jsonify({"ok": True, "alert": False, "reason": "LLM 2차 판단: 낙상 아님"})

    # LLM이 FALL → 10초 예비 경보 타이머 시작
    started = start_preliminary_alert("카메라: fall_probability=%.2f" % prob, "%.2f" % prob)
    if started:
        add_log("preliminary", "예비 경보 시작 (카메라 이벤트, 10초 후 확정)", {"event": data})
    return jsonify({"ok": True, "alert": False, "reason": "예비 경보 시작 (10초 대기 중, 확률 %.2f)" % prob})


@app.route("/cancel", methods=["POST"])
def cancel():
    """진행 중인 예비 경보 취소 + 확정 경보 리셋 → 이후 /sensor 응답 alert=False."""
    global confirmed_alert, pending_timer, pending_info
    with STATE_LOCK:
        cancelled_pending = pending_timer is not None and pending_timer.is_alive()
        if cancelled_pending:
            pending_timer.cancel()  # 예비 타이머 취소
        pending_timer = None
        pending_info = None
        was_confirmed = confirmed_alert
        confirmed_alert = False  # 확정 상태 리셋
    if cancelled_pending:
        add_log("cancel", "예비 경보 취소됨 (/cancel 수신)")
    elif was_confirmed:
        add_log("cancel", "확정 경보 해제됨 (/cancel 수신, 이후 /sensor alert=False)")
    else:
        add_log("cancel", "취소 요청 수신 (진행 중인 경보 없음)")
    return jsonify({"ok": True})


@app.route("/camera_update", methods=["POST"])
def camera_update():
    """Pi 카메라 스트림 수신 (이미지+확률, 대시보드 표시용)."""
    global last_camera
    data = request.get_json(force=True, silent=True) or {}
    if isinstance(data, dict) and "image" in data:
        with STATE_LOCK:
            last_camera = {
                "timestamp": str(data.get("timestamp", "")),
                "image": str(data.get("image", "")),
                "probabilities": data.get("probabilities", {}) if isinstance(data.get("probabilities"), dict) else {},
                "top_class": str(data.get("top_class", "")),
                "fg_ratio": data.get("fg_ratio"),
            }
    return jsonify({"ok": True})


@app.route("/log", methods=["GET"])
def get_log():
    """전체 이벤트 로그 JSON 반환 (디버그용)."""
    return jsonify(get_logs_snapshot())


# ---------------- 실행 ----------------
if __name__ == "__main__":
    add_log("info", "Pi 서버 시작 (포트 5000)")
    # 0.0.0.0 바인딩: 중계 Pi가 WiFi로 접속할 수 있게 함
    app.run(host="0.0.0.0", port=5000, threaded=True)

# ---------------- 테스트용 curl 예시 ----------------
# 센서 의심 케이스 (큰 소리+움직임 없음, 초음파 2개 50 이하 → 예비 경보):
# curl -X POST http://127.0.0.1:5000/sensor -H "Content-Type: application/json" -d '{"from":"arduino","sensors":{"ANG":"45","PIR":"0","SND1":"1","SND2":"0","US1":"40","US2":"45","US3":"200","US4":"210","US5":"190"}}'
# 센서 정상 케이스:
# curl -X POST http://127.0.0.1:5000/sensor -H "Content-Type: application/json" -d '{"from":"arduino","sensors":{"ANG":"45","PIR":"1","SND1":"0","SND2":"0","US1":"120","US2":"130","US3":"115","US4":"200","US5":"180"}}'
# 낙상 이벤트 케이스:
# curl -X POST http://127.0.0.1:5000/fall_event -H "Content-Type: application/json" -d '{"event":"fall_suspected","device":"pi1","timestamp":"2026-10-16T00:00:00","top_class":"fall","fall_probability":0.87,"sustained_sec":3,"probabilities":{"fall":0.87,"non_fall":0.13}}'
# 경보 취소:
# curl -X POST http://127.0.0.1:5000/cancel
# 로그 확인:
# curl http://127.0.0.1:5000/log
# 대시보드: 브라우저에서 http://127.0.0.1:5000/ 접속
