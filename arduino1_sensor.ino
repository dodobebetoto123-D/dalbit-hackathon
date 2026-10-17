// 아두이노 1 — 센서용 (초음파 5개 + PIR + 음향센서 1개)
// 아두이노 2의 "READ" 요청에 응답하는 방식
//
// 배선:
//   초음파 1: TRIG→D2,  ECHO→D3
//   초음파 2: TRIG→D4,  ECHO→D5
//   초음파 3: TRIG→D6,  ECHO→D7
//   초음파 4: TRIG→D8,  ECHO→D9
//   초음파 5: TRIG→D10, ECHO→D11
//   PIR OUT     → A2  (VCC→5V, GND→GND)
//   음향센서 AO → A4  (VCC→5V, GND→GND) — 아날로그 방식
//   TX/RX 링크: D13(TX)→상대 D10, D12(RX)→상대 D11, GND→GND
//   USB: PC 디버그용
//
// 동작:
//   아두이노2가 "READ"를 보내면 전 센서를 읽어서 한 줄로 응답:
//   PIR:0,SND1:0,SND2:0,US1:120,US2:130,US3:115,US4:200,US5:180
//
// ※ 초음파 5개는 회전 빵판에 장착 (스텝모터가 빵판을 왕복 회전)
// ※ 음향센서는 아날로그 방식: 기준값보다 150 이상 튀면 감지

#include <SoftwareSerial.h>
SoftwareSerial link(12, 13); // RX=D12, TX=D13

const int TRIG[5] = {2, 4, 6, 8, 10};
const int ECHO[5] = {3, 5, 7, 9, 11};
const int PIR = A2;
const int SND = A4;  // 음향센서 AO (아날로그)

// 음향센서 기준값 (setup에서 자동 측정)
int sndBaseline = 512;
const int SND_THRESHOLD = 150;  // 기준값보다 이만큼 튀면 감지

long readUS(int t, int e) {
  digitalWrite(t, LOW); delayMicroseconds(2);
  digitalWrite(t, HIGH); delayMicroseconds(10);
  digitalWrite(t, LOW);
  long d = pulseIn(e, HIGH, 30000); // 30ms 타임아웃
  if (d == 0) return 999; // 감지 실패
  return d / 58; // cm
}

int readSND() {
  int v = analogRead(SND);
  return (v > sndBaseline + SND_THRESHOLD) ? 1 : 0;
}

void setup() {
  Serial.begin(9600);
  link.begin(9600);
  pinMode(PIR, INPUT);
  for (int i = 0; i < 5; i++) {
    pinMode(TRIG[i], OUTPUT);
    pinMode(ECHO[i], INPUT);
  }
  // 음향센서 기준값 측정 (1초)
  long sum = 0;
  for (int i = 0; i < 50; i++) {
    sum += analogRead(SND);
    delay(20);
  }
  sndBaseline = sum / 50;
  Serial.print("음향 기준값: ");
  Serial.println(sndBaseline);
  delay(30000); // PIR 안정화 30초
  Serial.println("[아두이노1] READ 대기 중");
}

void loop() {
  if (link.available()) {
    String cmd = link.readStringUntil('\n');
    cmd.trim();
    if (cmd == "READ") {
      String msg = "PIR:" + String(digitalRead(PIR));
      msg += ",SND1:" + String(readSND());
      msg += ",SND2:0";
      for (int i = 0; i < 5; i++) {
        msg += ",US" + String(i + 1) + ":" + String(readUS(TRIG[i], ECHO[i]));
        delay(10); // 센서 간 간섭 방지
      }
      link.println(msg);
      Serial.println(msg);
    }
  }
}
