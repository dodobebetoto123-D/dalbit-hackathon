// 아두이노 2 — 출력 + 초음파 스캔 담당 (단독 테스트 가능)
//
// 필요 라이브러리 (아두이노 IDE → 스케치 → 라이브러리 포함하기 → 라이브러리 관리):
//   Adafruit SSD1306, Adafruit GFX Library
//   (Stepper는 아두이노 기본 내장 — 설치 불필요)
//
// 배선:
//   스텝모터(28BYJ-48+ULN2003): IN1→D4, IN2→D5, IN3→D6, IN4→D7, VCC→5V, GND→GND
//   OLED 0.91" (I2C): SDA→A4, SCL→A5, VCC→5V, GND→GND (주소 보통 0x3C)
//   LED (초록=정상): A0 → 220Ω → LED(+) → GND
//   LED (빨강=경고): A1 → 220Ω → LED(+) → GND
//   부저 (액티브): IN→D12, VCC→5V, GND→GND
//   TX/RX 링크: D10(RX)→상대 D11, D11(TX)→상대 D10, GND→GND
//   USB: Pi와 직접 연결 (데이터 전송용)
//
// 스캔 프로토콜 (아두이노1과 약속):
//   1) 이 보드가 모터를 한 칸 회전시킨 뒤 링크로 "READ" 전송
//   2) 아두이노1이 "PIR:0,SND1:0,SND2:0,US1:120,US2:130,US3:115,US4:200,US5:180" 로 응답
//   3) 이 보드가 각도를 붙여 Pi로 USB 전송:
//      "ANG:45,PIR:0,SND1:0,SND2:0,US1:120,US2:130,US3:115,US4:200,US5:180"
//   ※ 모터는 왕복 회전 (0°→180°→0°) — 한 방향으로 계속 돌리면 선이 꼬여 끊어짐!
//
// 단독 테스트 (아두이노1 없이):
//   - USB 연결 후 시리얼 모니터(9600)에서 "ALERT" 입력 → 빨강LED+부저+OLED 경고
//   - "RESET" 입력 → 정상 상태로 복귀
//   - "CANCEL" 입력 → Pi 서버에 취소 신호 전송
//   - 스캔은 아두이노1 응답 없이도 계속 돔 (센서값 0으로 표시)

#include <SoftwareSerial.h>
#include <Stepper.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "korean_font.h" // 미니 한글 폰트 (같은 폴더에 있어야 함)

SoftwareSerial link(10, 11); // 아두이노 1 (RX, TX)
// USB로 Pi와 직접 연결 (블루투스 제거됨)

const int LED_OK = A0;   // 초록
const int LED_WARN = A1; // 빨강
const int BUZZ = 12;     // 액티브 부저

const int STEPS_REV = 2048;   // 28BYJ-48 한 바퀴
// ※ 핀 순서 주의: Stepper 라이브러리는 (IN1, IN3, IN2, IN4) 순서로 넣어야 제대로 돎
//    배선은 그대로 IN1→D4, IN2→D5, IN3→D6, IN4→D7
Stepper scanner(STEPS_REV, 4, 6, 5, 7);
const int SWEEP_STEPS = 1024; // 180도
const int SCAN_INCR = 128;    // 약 22.5도 간격 → 한 방향 9지점

#define OLED_W 128
#define OLED_H 32
Adafruit_SSD1306 oled(OLED_W, OLED_H, &Wire, -1);

String lastSensor = "PIR:0,SND1:0,SND2:0,US1:0,US2:0,US3:0,US4:0,US5:0";
bool alerted = false;
int scanPos = 0;   // 현재 모터 위치 (스텝 수)
int scanDir = 1;   // +1: 0°→180°, -1: 180°→0°

void oledMsg(const char *l1, const char *l2) {
  oled.clearDisplay();
  kf_draw(oled, l1, 0, 0);   // 한글 16px 첫 줄
  kf_draw(oled, l2, 0, 16);  // 한글 16px 둘째 줄
  oled.display();
}

void setNormal() {
  digitalWrite(LED_OK, HIGH);
  digitalWrite(LED_WARN, LOW);
  digitalWrite(BUZZ, LOW);
  oledMsg("정상", "감시중...");
  alerted = false;
}

void doAlert(const char *why) {
  if (alerted) return;
  alerted = true;
  digitalWrite(LED_OK, LOW);
  digitalWrite(LED_WARN, HIGH);
  oledMsg("낙상 감지!", "긴급 문자");
  Serial.print("[ALERT] ");
  Serial.println(why);
  for (int i = 0; i < 3; i++) {  // 경고음 3회
    digitalWrite(BUZZ, HIGH); delay(200);
    digitalWrite(BUZZ, LOW); delay(200);
  }
  oledMsg("긴급 문자", "모의 발송중");
}

void doReset() {
  setNormal();
  Serial.println("[RESET]");
}

void handleCmd(String cmd) {
  cmd.trim();
  if (cmd == "ALERT") {
    doAlert("Pi 서버 판단");
  } else if (cmd == "RESET") {
    doReset();
  } else if (cmd == "CANCEL") {
    // Pi 서버에 취소 신호 전송 (USB를 통해)
    Serial.println("CANCEL");
  }
}

// 현장 즉시 반응: "쿵" 소리 + 움직임 없음 → 경고
void quickCheck(String &m) {
  if ((m.indexOf("SND1:1") >= 0 || m.indexOf("SND2:1") >= 0) && m.indexOf("PIR:0") >= 0) {
    doAlert("소리+무움직임");
  }
}

// 링크에서 한 줄 읽기 (타임아웃 ms). 없으면 "" 반환.
String readLinkLine(unsigned long timeoutMs) {
  String buf = "";
  unsigned long start = millis();
  link.listen();
  while (millis() - start < timeoutMs) {
    while (link.available()) {
      char c = link.read();
      if (c == '\n') {
        buf.trim();
        return buf;
      } else if (c != '\r') {
        buf += c;
      }
    }
  }
  return ""; // 타임아웃 (아두이노1 미연결 등)
}

// 스캔 한 칸: 모터 회전 → 아두이노1에 READ → 각도 태깅 → Pi 전송
void scanStep() {
  scanner.step(SCAN_INCR * scanDir);
  scanPos += SCAN_INCR * scanDir;
  int deg = (int)(scanPos * 360L / STEPS_REV);

  link.listen();
  link.println("READ");
  String resp = readLinkLine(800); // 아두이노1 응답 대기 (최대 0.8초)
  if (resp.length() > 0) {
    lastSensor = resp;
    quickCheck(resp);
  }

  String merged = "ANG:" + String(deg) + "," + lastSensor;
  Serial.println(merged); // USB로 Pi에 전송

  if (scanPos >= SWEEP_STEPS || scanPos <= 0) {
    scanDir = -scanDir; // 왕복 회전
  }
}

void handleUsb() {
  while (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    handleCmd(cmd);
  }
}

void setup() {
  Serial.begin(9600);  // USB (Pi 연결)
  link.begin(9600);    // 아두이노 1
  pinMode(LED_OK, OUTPUT);
  pinMode(LED_WARN, OUTPUT);
  pinMode(BUZZ, OUTPUT);
  digitalWrite(BUZZ, LOW);
  scanner.setSpeed(12); // rpm

  if (!oled.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("OLED 없음! 0x3C 확인");
  }
  setNormal();
  Serial.println("[아두이노2 출력+스캔 시작]");
  Serial.println("테스트: ALERT / RESET 입력");
}

void loop() {
  handleUsb();  // USB 명령 수신 (Pi 릴레이)
  scanStep();   // 스캔 한 칸 진행
}
