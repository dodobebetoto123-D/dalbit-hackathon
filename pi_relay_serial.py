#!/usr/bin/env python3
"""
Pi 중계 — 아두이노 2(HC-06 블루투스) <-> Pi 서버(WiFi) 중계

HC-06 페어링 (최초 1회, Pi 중계에서 실행):
  bluetoothctl
  [bluetoothctl] power on
  [bluetoothctl] agent on
  [bluetoothctl] scan on
  # HC-06이 보이면:
  [bluetoothctl] scan off
  [bluetoothctl] pair XX:XX:XX:XX:XX:XX   (PIN 물으면 1234)
  [bluetoothctl] trust XX:XX:XX:XX:XX:XX
  [bluetoothctl] quit

RFCOMM 바인딩 (부팅할 때마다 실행):
  sudo rfcomm bind 0 XX:XX:XX:XX:XX:XX 1
  ls /dev/rfcomm0   # 확인

실행 전: pip install pyserial
실행: python3 pi_relay_serial.py --port /dev/rfcomm0 --server <Pi서버IP>

동작:
  - 아두이노에서 오는 센서 줄(ANG:45,PIR:..,SND1:..,SND2:..,US1:..)을 읽어서
    Pi 서버(http://<Pi서버IP>:5000/sensor)로 POST 중계
  - 서버 응답의 alert가 true면 아두이노2에 "ALERT" 전송 (부저/LED)
  - HC-06 빨간 LED: 깜빡임=미연결, 계속 켜짐=연결됨
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
    ap.add_argument("--port", default="/dev/rfcomm0",
                    help="HC-06 RFCOMM 포트 (USB면 /dev/ttyACM0)")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--server", default="127.0.0.1", help="Pi 서버 IP")
    ap.add_argument("--server-port", type=int, default=5000)
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=1)
    time.sleep(2)
    url = f"http://{args.server}:{args.server_port}/sensor"
    print(f"[중계 시작] {args.port} → {url}", flush=True)
    print("아두이노에 명령 보내기: 별도 터미널에서 minicom 등으로 /dev/rfcomm0에 ALERT 입력",
          flush=True)

    while True:
        try:
            raw = ser.readline().decode(errors="ignore").strip()
        except Exception as e:
            print(f"[시리얼 오류] {e}", flush=True)
            time.sleep(1)
            continue
        if not raw or "PIR:" not in raw:
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
