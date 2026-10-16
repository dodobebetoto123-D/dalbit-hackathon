#!/usr/bin/env python3
"""
Pi 중계 — 아두이노 2(USB 시리얼) <-> Pi 서버(WiFi) 중계

연결: 아두이노2 USB를 이 Pi에 직접 연결 (/dev/ttyACM0)

실행 전: pip install pyserial
실행: python3 pi_relay_serial.py --port /dev/ttyACM0 --server <Pi서버IP>

동작:
  - 아두이노에서 오는 센서 줄(ANG:45,PIR:..,SND1:..,SND2:..,US1:..)을 읽어서
    Pi 서버(http://<Pi서버IP>:5000/sensor)로 POST 중계
  - 서버 응답의 alert가 true면 아두이노2에 "ALERT" 전송 (부저/LED)
  - 아두이노2에서 "CANCEL"이 오면 서버 /cancel 호출 (예비 경보 취소)
  - 카메라는 pi1_fall_detector.py를 이 Pi에서 같이 실행 (--server <Pi서버IP>)
"""
import argparse
import json
import time
import urllib.request

import serial


def post_json(url, payload, timeout=3.0):
    """POST 후 서버 응답 JSON(dict) 반환. 실패 시 None."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 200:
                return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[중계 실패] {e}", flush=True)
    return None


def parse_sensor(line):
    """PIR:1,SND:0,US1:120,... → dict"""
    out = {}
    for part in line.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0",
                    help="아두이노 USB 시리얼 포트")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--server", default="127.0.0.1", help="Pi 서버 IP")
    ap.add_argument("--server-port", type=int, default=5000)
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=1)
    time.sleep(2)
    url = f"http://{args.server}:{args.server_port}/sensor"
    cancel_url = f"http://{args.server}:{args.server_port}/cancel"
    print(f"[중계 시작] {args.port} → {url}", flush=True)

    while True:
        try:
            raw = ser.readline().decode(errors="ignore").strip()
        except Exception as e:
            print(f"[시리얼 오류] {e}", flush=True)
            time.sleep(1)
            continue
        if not raw:
            continue
        # 취소 버튼: 아두이노2 → 서버 /cancel
        if raw == "CANCEL":
            print("[취소] 아두이노2 취소 버튼 → 서버에 전달", flush=True)
            post_json(cancel_url, {})
            continue
        if "PIR:" not in raw:
            continue
        data = parse_sensor(raw)
        print(f"[센서] {data}", flush=True)
        resp = post_json(url, {"from": "arduino", "sensors": data})
        # 서버 판단이 alert면 아두이노2에 ALERT 전송 (부저/LED)
        if isinstance(resp, dict) and resp.get("alert"):
            try:
                ser.write(b"ALERT\n")
                print(f"[경고 전달] 서버 판단: {resp.get('reason')}", flush=True)
            except Exception as e:
                print(f"[전달 실패] {e}", flush=True)


if __name__ == "__main__":
    main()
