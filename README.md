# PnL Coach — 멀티거래소 선물 손익 코치

거래소 API 키(읽기 전용)를 등록하면 청산 내역을 DB에 계속 쌓고,
승률·손익비·습관 진단 + 본인 LLM 토큰으로 AI 코칭을 제공하는 셀프호스팅 웹앱.

## 지원

- **거래소**: MEXC, Binance(USDⓈ-M), Bybit, Bitget, Gate
- **AI**: Claude(Anthropic) / OpenAI — 사용자 본인 토큰 사용 (BYO key)
  - Claude **Pro/Max 구독자**는 API 비용 없이 구독 요금으로 사용 가능:
    터미널에서 `claude setup-token` → 나온 `sk-ant-oat…` 토큰 등록 (자동 판별)
  - OpenAI는 API 키만 지원 (ChatGPT Plus 구독에는 API 접근이 포함되지 않음)
- 멀티유저: 가입/로그인, 사용자별 키·데이터 분리

## 실행

```bash
./run.sh                 # venv 자동 생성 + 기동, http://localhost:8777
PORT=9000 ./run.sh       # 포트 변경
```

상시 구동(systemd user 서비스):

```bash
cp pnl_coach.service ~/.config/systemd/user/   # 경로 수정 필요시 편집
systemctl --user enable --now pnl_coach
```

## 사용 순서

1. 가입 → 로그인
2. 설정에서 거래소 키 등록 — **반드시 읽기 전용 키** (거래·출금 권한 OFF)
3. "지금 동기화" — 이후 30분마다 자동 동기화
4. 대시보드에서 손익·진단 확인, AI 토큰 등록 후 "조언 생성"

## 데이터 & 보안

- 모든 키는 Fernet 암호화되어 `data/pnl_coach.db`에 저장. 암호화 키는 `data/.secret`(600).
- `data/` 디렉토리는 배포·공유 대상에서 제외할 것 (.gitignore 처리됨).
- 서버는 조회 전용 API만 호출 — 주문·출금 코드 없음.

## 거래소별 구현 노트

| 거래소 | 방식 | 상태 |
|---|---|---|
| MEXC | contract API 직접 서명, `history_positions` (약 90일 한도) | 실계좌 검증 완료 |
| Bybit | v5 `closed-pnl` (7일 창 반복 조회) | 구현, 실키 검증 전 |
| Bitget | v2 mix `history-position` | 구현, 실키 검증 전 |
| Gate | futures `position_close` | 구현, 실키 검증 전 |
| Binance | income 기록 기반 (REALIZED_PNL 이벤트 = 1건 근사) | 구현, 실키 검증 전 |

동기화 실패는 UI의 거래소 목록에 에러 메시지로 표시됨.

## 법적 주의 (한국)

- 개인/지인 무료 사용: 문제 없음.
- **불특정 다수에게 서비스 + 수익화(구독료·광고 포함)**: AI "조언" 기능이 투자조언에
  해당할 수 있어 **유사투자자문업 신고**(자본시장법) 검토 필요. 객관적 통계 표시만으로
  운영하면 규제 대상 아님.
- 면책: 본 도구의 출력은 투자 권유가 아니며 투자 판단 책임은 사용자 본인에게 있음.

## 구조

```
app.py        FastAPI 라우트 + 30분 백그라운드 동기화
exchanges.py  거래소 어댑터 (공통 스키마 정규화)
stats.py      통계 + 룰 기반 습관 진단
advisor.py    Claude/OpenAI 코칭 생성
db.py         SQLite 스키마/헬퍼
security.py   pbkdf2 비밀번호 해시 + Fernet 키 암호화
static/       단일 페이지 UI
data/         런타임 데이터 (배포 제외)
```
