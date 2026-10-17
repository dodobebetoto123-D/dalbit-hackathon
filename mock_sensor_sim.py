# mock_sensor_sim.py — 아두이노 없이 센서 데이터 모의 전송 (시연용 백업)
# 사용법: python3 mock_sensor_sim.py [--server 127.0.0.1] [--scenario fall|normal]
#   fall: 낙상 시나리오 (초음파 근접 + PIR 감지)
#   normal: 정상 시나리오 (평상시 데이터)

import argparse
import json
import time
import urllib.request

def send_sensor(server, data):
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"http://{server}:5000/sensor",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"[전송] {data} → 응답: {result}")
            return result
    except Exception as e:
        print(f"[오류] 전송 실패: {e}")
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="127.0.0.1")
    parser.add_argument("--scenario", default="fall", choices=["fall", "normal"])
    parser.add_argument("--interval", type=float, default=2.0, help="전송 간격(초)")
    parser.add_argument("--count", type=int, default=0, help="전송 횟수 (0=무한)")
    args = parser.parse_args()

    print(f"모의 센서 시작 — 시나리오: {args.scenario}, 서버: {args.server}:5000")
    print("중지: Ctrl+C")

    count = 0
    try:
        while True:
            if args.scenario == "fall":
                # 낙상 시나리오: 초음파 3개가 30cm 이하 (누움), PIR 감지
                data = {
                    "ANG": 90,
                    "PIR": 1,
                    "SND1": 1,
                    "SND2": 0,
                    "US1": 25,
                    "US2": 30,
                    "US3": 28,
                    "US4": 150,
                    "US5": 180,
                }
            else:
                # 정상 시나리오: 모두 멀리 있음
                data = {
                    "ANG": 90,
                    "PIR": 0,
                    "SND1": 0,
                    "SND2": 0,
                    "US1": 200,
                    "US2": 210,
                    "US3": 195,
                    "US4": 220,
                    "US5": 205,
                }

            send_sensor(args.server, data)
            count += 1
            if args.count > 0 and count >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n종료 (총 {count}회 전송)")

if __name__ == "__main__":
    main()
