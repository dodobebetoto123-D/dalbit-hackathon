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
GROQ_MODEL = "llama-3.3-70b-versatile"  # Groq 2차 판단 모델
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
    low_count = 0
    low_list = []
    for i in range(1, 6):
        key = "US%d" % i
        try:
            v = float(str(sensors.get(key, "")).strip())
        except (ValueError, TypeError):
            continue  # 파싱 실패 값은 무시
        if v <= 50:
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
        print("[LLM 폴백] API 실패(%s) → 1차 판단(의심) 유지" % e)
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
    """대시보드 HTML (5초 자동 새로고침, 심사위원 시연용)."""
    logs = get_logs_snapshot()
    with STATE_LOCK:
        alert_state = confirmed_alert
        pending_alive = pending_timer is not None and pending_timer.is_alive()
        sensors_snapshot = dict(last_sensors)
        fall_snapshot = dict(last_fall_event)
        cam_snapshot = dict(last_camera)

    # 카메라 스트림 표시
    if cam_snapshot.get("image"):
        cam_img_tag = '<img src="data:image/jpeg;base64,%s" style="max-width:448px;border:2px solid #333">' % cam_snapshot["image"]
        probs = cam_snapshot.get("probabilities", {})
        prob_str = " / ".join("%s: %.2f" % (k, v) for k, v in probs.items())
        # 낙상 확률 바 (시각화)
        fall_p = float(probs.get("fall", 0))
        bar_w = int(fall_p * 300)
        bar_color = "#dc3545" if fall_p >= 0.5 else "#28a745"
        prob_bar = '<div style="width:300px;background:#ddd"><div style="width:%dpx;height:20px;background:%s"></div></div>' % (bar_w, bar_color)
        cam_info = "Top: <b>%s</b> | %s | 전경: %s | %s<br>%s" % (
            esc(cam_snapshot.get("top_class", "")),
            esc(prob_str),
            esc(str(cam_snapshot.get("fg_ratio", ""))),
            esc(cam_snapshot.get("timestamp", "")),
            prob_bar,
        )
    else:
        cam_img_tag = "<p>카메라 스트림 없음 (pi1에서 --no-stream 없이 실행 중인지 확인)</p>"
        cam_info = ""

    # 종류별 로그 분리 (표 표시용, 최신이 위로)
    sensor_logs = [e for e in logs if e.get("type") == "sensor"][::-1][:20]
    fall_logs = [e for e in logs if e.get("type") == "fall_event"][::-1][:20]
    llm_logs = [e for e in logs if e.get("type") == "llm"][::-1][:20]
    sms_logs = [e for e in logs if e.get("type") == "sms"][::-1][:20]
    recent_logs = logs[::-1][:30]

    def esc(v):
        """간단 HTML 이스케이프."""
        return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def row(cells):
        return "<tr>" + "".join("<td>%s</td>" % esc(c) for c in cells) + "</tr>"

    # 상태 배지
    if alert_state:
        state_badge = '<span class="badge danger">확정 경보 발령 중 (ALERT ON)</span>'
    elif pending_alive:
        state_badge = '<span class="badge warn">예비 경보 진행 중 (10초 대기)</span>'
    else:
        state_badge = '<span class="badge ok">정상 모니터링 중</span>'

    # 센서 표 행
    if sensor_logs:
        sensor_rows = "".join(
            row([e.get("timestamp", ""), e.get("message", "")]) for e in sensor_logs
        )
    else:
        sensor_rows = '<tr><td colspan="2">수신된 센서 데이터 없음</td></tr>'

    if fall_logs:
        fall_rows = "".join(
            row([e.get("timestamp", ""), e.get("message", "")]) for e in fall_logs
        )
    else:
        fall_rows = '<tr><td colspan="2">수신된 낙상 이벤트 없음</td></tr>'

    if llm_logs:
        llm_rows = "".join(
            row([
                e.get("timestamp", ""),
                e.get("message", ""),
                (e.get("detail", {}) or {}).get("summary", ""),
                (e.get("detail", {}) or {}).get("response", ""),
            ])
            for e in llm_logs
        )
    else:
        llm_rows = '<tr><td colspan="4">LLM 판단 기록 없음</td></tr>'

    if sms_logs:
        sms_rows = "".join(
            row([
                e.get("timestamp", ""),
                e.get("message", ""),
                (e.get("detail", {}) or {}).get("content", ""),
            ])
            for e in sms_logs
        )
    else:
        sms_rows = '<tr><td colspan="3">발송된 모의 문자 없음</td></tr>'

    if recent_logs:
        recent_rows = "".join(
            row([e.get("timestamp", ""), e.get("type", ""), e.get("message", "")])
            for e in recent_logs
        )
    else:
        recent_rows = '<tr><td colspan="3">로그 없음</td></tr>'

    html = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>봄베 — 독거노인 낙상 감지 대시보드 (서버)</title>
<style>
body{font-family:monospace,sans-serif;max-width:1000px;margin:20px auto;padding:0 16px;background:#f7f7f7;color:#222}
h1{font-size:22px} h2{font-size:17px;margin-top:28px;border-bottom:2px solid #333;padding-bottom:4px}
table{width:100%;border-collapse:collapse;background:#fff;font-size:13px}
td,th{border:1px solid #ccc;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#eee}
.badge{display:inline-block;padding:6px 12px;border-radius:6px;font-weight:bold}
.ok{background:#d4edda}.warn{background:#fff3cd}.danger{background:#f8d7da}
.info{background:#fff;padding:10px;border:1px solid #ccc;margin:8px 0}
button{padding:8px 16px;font-size:14px;cursor:pointer}
</style>
</head>
<body>
<h1>봄베 — 독거노인 낙상 감지 대시보드 (Pi 서버)</h1>
<p>상태: __STATE_BADGE__ (5초마다 자동 새로고침)</p>
<div class="info">
<b>등록 정보(시연용):</b> __REG_NAME__ / __REG_ADDR__ / 보호자 __REG_GUARD__ / 혈액형 __REG_BLOOD__ / 지병 __REG_DIS__ / 복용약 __REG_MED__<br>
<b>마지막 센서:</b> __LAST_SENSORS__<br>
<b>마지막 낙상 이벤트:</b> __LAST_FALL__
</div>
<form method="POST" action="/cancel" onsubmit="fetch('/cancel',{method:'POST'}).then(()=>location.reload());return false;">
<button type="submit">예비 경보 / 확정 경보 취소</button>
</form>
<h2>실시간 카메라 (Pi1 낙상 감지)</h2>
__CAM_IMG__
<p>__CAM_INFO__</p>
<h2>센서 데이터 수신 로그 (최신 20건)</h2>
<table><tr><th>시각</th><th>내용</th></tr>__SENSOR_ROWS__</table>
<h2>낙상 이벤트 수신 로그 (최신 20건)</h2>
<table><tr><th>시각</th><th>내용</th></tr>__FALL_ROWS__</table>
<h2>LLM 2차 판단 과정 (최신 20건)</h2>
<table><tr><th>시각</th><th>판단</th><th>입력 요약</th><th>LLM 응답</th></tr>__LLM_ROWS__</table>
<h2>모의 문자 발송 로그</h2>
<table><tr><th>시각</th><th>대상</th><th>내용</th></tr>__SMS_ROWS__</table>
<h2>전체 이벤트 로그 (최신 30건)</h2>
<table><tr><th>시각</th><th>종류</th><th>내용</th></tr>__RECENT_ROWS__</table>
<p><a href="/log">/log JSON 보기</a></p>
</body>
</html>"""
    html = (
        html.replace("__STATE_BADGE__", state_badge)
        .replace("__REG_NAME__", esc(REGISTRY["name"]))
        .replace("__REG_ADDR__", esc(REGISTRY["address"]))
        .replace("__REG_GUARD__", esc(REGISTRY["guardian"]))
        .replace("__REG_BLOOD__", esc(REGISTRY["blood_type"]))
        .replace("__REG_DIS__", esc(REGISTRY["disease"]))
        .replace("__REG_MED__", esc(REGISTRY["medicine"]))
        .replace("__LAST_SENSORS__", esc(json.dumps(sensors_snapshot, ensure_ascii=False) if sensors_snapshot else "없음"))
        .replace("__LAST_FALL__", esc(json.dumps(fall_snapshot, ensure_ascii=False) if fall_snapshot else "없음"))
        .replace("__CAM_IMG__", cam_img_tag)
        .replace("__CAM_INFO__", cam_info)
        .replace("__SENSOR_ROWS__", sensor_rows)
        .replace("__FALL_ROWS__", fall_rows)
        .replace("__LLM_ROWS__", llm_rows)
        .replace("__SMS_ROWS__", sms_rows)
        .replace("__RECENT_ROWS__", recent_rows)
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
