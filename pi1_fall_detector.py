#!/usr/bin/env python3
"""
Pi 낙상 감지 추론 스크립트 — 독거노인 안심 지킴이 (2026 달빛연합 해커톤)

동작: 카메라 캡처 → 오픈소스 실루엣 TFLite 모델 추론
      → 낙상 확률 50% 이상이 3초 지속되면 Pi 서버로 JSON 전송 (WiFi/HTTP)

프라이버시: 캡처 이미지를 파일로 저장하지 않음. 메모리에만 두고 추론 후 버림.

오픈소스 실루엣 모델용 전처리:
  학습한 모델은 실루엣(검은 배경+흰 사람)을 입력으로 기대한다.
  --silhouette 옵션으로 실행하면 MOG2 배경 제거로 실루엣을 만들어 추론한다.
  고정 카메라는 처음 30프레임이 배경 학습 구간이다.

회전 카메라 모드 (--rotate):
  스텝모터에 카메라를 올리면 배경이 계속 바뀌므로 MOG2가 흔들린다.
  --rotate를 켜면 (1) history를 짧게 해서 빨리 적응하고,
  (2) 전경 비율이 너무 크면(배경 미적응/이동 중) 추론을 스킵하고,
  (3) 전경이 거의 없으면(사람 없음) 추론을 스킵한다.
  그래도 고정 카메라보다 불안정할 수 있다. 현장에서 임계값을 튜닝할 것.
  팁: 아두이노 스캔이 각도마다 1초 정도 멈추면(dwell) 인식률이 크게 오른다.

준비물 (이 Pi에):
  model.tflite + labels.txt (학습 완료본, 이 스크립트와 같은 폴더)
  pip install tflite-runtime numpy opencv-python
  카메라 모듈이면: pip install picamera2

실행:
  고정 카메라:  python3 pi1_fall_detector.py --pi2 192.168.0.20 --silhouette
  회전 카메라:  python3 pi1_fall_detector.py --pi2 192.168.0.20 --silhouette --rotate
  (서버 IP는 현장에서 hostname -I 로 확인)

Pi 서버 수신 계약:
  - 엔드포인트: POST http://<서버IP>:5000/fall_event
  - 바디(JSON): {"event","device","timestamp","top_class",
                 "fall_probability","sustained_sec","probabilities"}
  - 응답: HTTP 200 + {"ok": true}
"""

import argparse
import base64
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

import numpy as np

KST = timezone(timedelta(hours=9))


def load_labels(path):
    with open(path, encoding="utf-8") as f:
        labels = [line.strip() for line in f if line.strip()]
    if not labels:
        raise ValueError(f"labels.txt이 비어 있음: {path}")
    return labels


def load_interpreter(model_path):
    try:
        from tflite_runtime.interpreter import Interpreter
    except ImportError:
        try:
            from ai_edge_litert.interpreter import Interpreter  # tflite-runtime 후속 패키지
        except ImportError:
            from tensorflow.lite.python.interpreter import Interpreter  # 폴백
    it = Interpreter(model_path=model_path)
    it.allocate_tensors()
    return it


class Camera:
    """Pi 카메라 모듈(picamera2) 우선, 없으면 USB 웹캠(OpenCV) 폴백."""

    def __init__(self, size=(224, 224)):
        self.size = size
        self.backend = None
        try:
            from picamera2 import Picamera2
            self.picam = Picamera2()
            cfg = self.picam.create_still_configuration(
                main={"size": size, "format": "RGB888"})
            self.picam.configure(cfg)
            self.picam.start()
            time.sleep(1.0)  # 센서 안정화
            self.backend = "picamera2"
        except Exception as e:
            print(f"[카메라] picamera2 실패({e}) → OpenCV 폴백", flush=True)
            import cv2
            self.cv2 = cv2
            self.cap = cv2.VideoCapture(0)
            if not self.cap.isOpened():
                raise RuntimeError("카메라를 열 수 없음 (picamera2·OpenCV 둘 다 실패)")
            self.backend = "opencv"

    def capture(self):
        """RGB uint8 numpy 배열 반환. 저장하지 않음."""
        if self.backend == "picamera2":
            return self.picam.capture_array()
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("프레임 캡처 실패")
        rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
        if (rgb.shape[1], rgb.shape[0]) != self.size:
            rgb = self.cv2.resize(rgb, self.size)
        return rgb


class SilhouetteMaker:
    """오픈소스 데이터셋(실루엣) 학습 모델용 전처리.

    MOG2 배경 제거로 사람 실루엣을 만든다.
    처음 warmup 프레임은 배경 학습 구간이라 (0.0, None)을 반환한다.
    make()는 (전경 비율, 실루엣 RGB) 튜플을 반환한다.
    """

    def __init__(self, warmup=30, history=200, varThreshold=25):
        import cv2
        self.cv2 = cv2
        self.mog = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=varThreshold, detectShadows=False)
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.warmup = warmup
        self.n = 0

    def make(self, rgb):
        """RGB 프레임 → (전경 비율 0~1, 실루엣 RGB). 배경 학습 중이면 (0.0, None)."""
        gray = self.cv2.cvtColor(rgb, self.cv2.COLOR_RGB2GRAY)
        fg = self.mog.apply(gray)
        self.n += 1
        _, fg = self.cv2.threshold(fg, 200, 255, self.cv2.THRESH_BINARY)
        fg = self.cv2.morphologyEx(fg, self.cv2.MORPH_OPEN, self.kernel)
        if self.n <= self.warmup:
            return 0.0, None
        ratio = float((fg > 0).mean())
        return ratio, self.cv2.cvtColor(fg, self.cv2.COLOR_GRAY2RGB)


class StatusLed:
    """정상=켜짐, 낙상 의심=깜빡임, 경보 전송 후=꺼짐."""

    def __init__(self, pin):
        self.led = None
        if pin is None:
            return
        try:
            from gpiozero import LED
            self.led = LED(pin)
            self.led.on()
        except Exception as e:
            print(f"[LED] 초기화 실패, LED 없이 진행: {e}", flush=True)

    def normal(self):
        if self.led:
            self.led.on()

    def suspect(self):
        if self.led:
            self.led.blink(0.25, 0.25)

    def alerted(self):
        if self.led:
            self.led.off()


def post_event(url, payload, timeout=3.0, retries=3):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status == 200:
                    return True
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    print(f"[전송 실패] Pi 2로 이벤트 전송 실패 (계속 감시): {last_err}", flush=True)
    return False


def post_camera_update(url, frame, probs, labels, fg_ratio):
    """카메라 화면 + 확률을 서버에 전송 (웹 대시보드용). 실패해도 무시하고 감지는 계속."""
    try:
        import cv2
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            return
        payload = {
            "timestamp": now_kst(),
            "image": base64.b64encode(buf.tobytes()).decode("ascii"),
            "probabilities": {l: round(float(p), 3) for l, p in zip(labels, probs)},
            "top_class": labels[int(np.argmax(probs))],
            "fg_ratio": round(float(fg_ratio), 3) if fg_ratio is not None else None,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception:
        pass  # 스트리밍 실패는 무시


def now_kst():
    return datetime.now(KST).isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser(description="Pi 1 낙상 감지 (TM TFLite → Pi 2 전송)")
    ap.add_argument("--model", default="model.tflite")
    ap.add_argument("--labels", default="labels.txt")
    ap.add_argument("--pi2", default="127.0.0.1", help="Pi 2 IP (현장 hostname -I로 확인)")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--interval", type=float, default=1.0, help="캡처 간격(초)")
    ap.add_argument("--threshold", type=float, default=0.5, help="낙상 확률 임계값")
    ap.add_argument("--sustain", type=int, default=3, help="지속 초(프레임) 수")
    ap.add_argument("--led-pin", type=int, default=27, help="상태 LED BCM 핀 (없으면 --no-led)")
    ap.add_argument("--no-led", action="store_true")
    ap.add_argument("--device-name", default="pi1")
    ap.add_argument("--silhouette", action="store_true",
                    help="오픈소스 실루엣 모델용 전처리 (MOG2 배경 제거). 카메라 고정 필요")
    ap.add_argument("--rotate", action="store_true",
                    help="스텝모터 회전 카메라용: 빠른 배경 적응 + 전경 비율 가드")
    ap.add_argument("--fg-max", type=float, default=0.75,
                    help="전경 비율이 이 값을 넘으면 배경 미적응/이동 중으로 보고 스킵")
    ap.add_argument("--fg-min", type=float, default=0.01,
                    help="전경 비율이 이 값 미만이면 사람 없음으로 보고 추론 스킵")
    ap.add_argument("--verbose", action="store_true",
                    help="매 프레임 추론 확률 출력 (판단 과정 확인용)")
    ap.add_argument("--no-stream", action="store_true",
                    help="웹 대시보드용 카메라 스트리밍 끄기")
    args = ap.parse_args()

    if args.rotate and not args.silhouette:
        print("[경고] --rotate는 --silhouette와 함께 쓰는 것을 권장", flush=True)

    labels = load_labels(args.labels)
    it = load_interpreter(args.model)
    in_d = it.get_input_details()[0]
    out_d = it.get_output_details()[0]
    _, h, w, _ = in_d["shape"]
    in_float = in_d["dtype"] == np.float32
    out_uint8 = out_d["dtype"] == np.uint8

    fall_idx = next((i for i, l in enumerate(labels) if "fall" in l.lower()), len(labels) - 1)

    cam = Camera(size=(w, h))
    led = StatusLed(None if args.no_led else args.led_pin)
    sil_maker = None
    if args.silhouette:
        # 회전 모드: history를 짧게 해서 새 각도의 배경에 빨리 적응
        sil_maker = SilhouetteMaker(
            history=10 if args.rotate else 200,
            varThreshold=30 if args.rotate else 25)
    if sil_maker:
        mode = "회전 모드" if args.rotate else "고정 모드"
        print(f"[실루엣 모드] MOG2 배경 제거 사용 ({mode})", flush=True)
    url = f"http://{args.pi2}:{args.port}/fall_event"
    stream_url = f"http://{args.pi2}:{args.port}/camera_update"

    print(f"[시작] {now_kst()} | 카메라={cam.backend} 입력={w}x{h} "
          f"클래스={labels} fallen인덱스={fall_idx}", flush=True)
    print(f"[시작] 전송 대상: {url} | 임계값={args.threshold} 지속={args.sustain}초", flush=True)

    suspect = 0
    alerted = False
    tick = 0

    while True:
        t0 = time.monotonic()
        try:
            frame = cam.capture()
        except Exception as e:
            print(f"[오류] 캡처 실패, 1초 후 재시도: {e}", flush=True)
            time.sleep(1.0)
            continue

        no_person = False  # 전경이 거의 없으면 추론 스킵하고 p_fall=0 처리
        fg_ratio = None
        if sil_maker is not None:
            fg_ratio, sil = sil_maker.make(frame)
            if sil is None:  # 배경 학습 중
                if tick % 10 == 0:
                    print(f"[배경 학습] {sil_maker.n}/{sil_maker.warmup} 프레임째", flush=True)
                dt = time.monotonic() - t0
                if dt < args.interval:
                    time.sleep(args.interval - dt)
                continue
            if fg_ratio > args.fg_max:
                # 카메라 이동 중이거나 배경 미적응 → 판단 불가, 상태 유지
                if tick % 10 == 0:
                    print(f"[스킵] 전경 {fg_ratio:.2f} — 이동/미적응", flush=True)
                dt = time.monotonic() - t0
                if dt < args.interval:
                    time.sleep(args.interval - dt)
                continue
            if fg_ratio < args.fg_min:
                no_person = True  # 사람 없음
            else:
                frame = sil

        if no_person:
            p_fall, top = 0.0, 0
            probs = np.zeros(len(labels), dtype=np.float32)
        else:
            x = frame.astype(np.uint8)
            if in_float:
                x = x.astype(np.float32) / 255.0
            it.set_tensor(in_d["index"], np.expand_dims(x, 0))
            it.invoke()
            raw = it.get_tensor(out_d["index"])[0]
            probs = (raw.astype(np.float32) / 255.0) if out_uint8 else raw.astype(np.float32)
            # 수치 오차 방지용 클리핑
            probs = np.clip(probs, 0.0, 1.0)

            top = int(np.argmax(probs))
            p_fall = float(probs[fall_idx])
        tick += 1

        # 판단 과정 출력 (verbose 모드)
        if args.verbose and not no_person:
            prob_str = " ".join(f"{l}={p:.2f}" for l, p in zip(labels, probs))
            fg_info = f" 전경={fg_ratio:.2f}" if sil_maker is not None else ""
            print(f"[추론] {prob_str} top={labels[top]}{fg_info}", flush=True)

        # 웹 대시보드용 스트리밍 (매 프레임 전송, 실패해도 무시)
        if not args.no_stream:
            post_camera_update(stream_url, frame, probs, labels, fg_ratio)

        if p_fall >= args.threshold:
            suspect += 1
            led.suspect()
            print(f"[의심 {suspect}/{args.sustain}] {now_kst()} "
                  f"fallen={p_fall:.2f} top={labels[top]}", flush=True)
            if suspect >= args.sustain and not alerted:
                payload = {
                    "event": "fall_suspected",
                    "device": args.device_name,
                    "timestamp": now_kst(),
                    "top_class": labels[top],
                    "fall_probability": round(p_fall, 3),
                    "sustained_sec": suspect,
                    "probabilities": {l: round(float(p), 3) for l, p in zip(labels, probs)},
                }
                ok = post_event(url, payload)
                alerted = True
                led.alerted()
                print(f"[경보 전송 {'성공' if ok else '실패'}] {json.dumps(payload, ensure_ascii=False)}",
                      flush=True)
        else:
            if suspect > 0:
                print(f"[리셋] 일시적 동작으로 간주 (최대 fallen={p_fall:.2f})", flush=True)
            suspect = 0
            if alerted:
                alerted = False
                print("[재무장] 정상 복귀, 다시 감시", flush=True)
            led.normal()
            if tick % 60 == 0:
                print(f"[정상] {now_kst()} top={labels[top]}({float(probs[top]):.2f}) "
                      f"fallen={p_fall:.2f}", flush=True)

        dt = time.monotonic() - t0
        if dt < args.interval:
            time.sleep(args.interval - dt)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[종료] 사용자 중단", flush=True)
        sys.exit(0)
