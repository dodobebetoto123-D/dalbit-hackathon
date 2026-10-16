# dalbit-hackathon
2026 대구-광주 달빛연합 해커톤 팀 봄베 — 독거노인 낙상 감지 시스템 (Pi 코드)

## 파일
- `pi1_fall_detector.py` — Pi 카메라 낙상 감지 (TFLite 실루엣 모델)
  - 고정 카메라: `python3 pi1_fall_detector.py --pi2 <서버IP> --silhouette`
  - 회전 카메라: `python3 pi1_fall_detector.py --pi2 <서버IP> --silhouette --rotate`
  - 필요: model.tflite + labels.txt (같은 폴더), tflite-runtime, numpy, opencv-python, picamera2(카메라 모듈)
- `pi_relay_serial.py` — 아두이노2(HC-06 블루투스) ↔ Pi 서버(WiFi) 중계
  - `python3 pi_relay_serial.py --port /dev/rfcomm0 --server <서버IP>`
  - 필요: pyserial, HC-06 페어링 후 `sudo rfcomm bind 0 <MAC> 1`

## 데이터 형식
아두이노2 → Pi: `ANG:45,PIR:1,SND1:0,SND2:0,US1:120,US2:130,US3:115,US4:200,US5:180`
