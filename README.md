# 📈 PnL Coach

> 멀티거래소 선물 손익 대시보드 + 데이터 기반 AI 트레이딩 코칭 (셀프호스팅)

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-self--hosted-009688)

거래소 API 키(읽기 전용)만 등록하면 청산 내역을 자동으로 수집·누적하고,
**"내가 왜 계속 본전인지"** 를 숫자로 보여주는 도구입니다.
수수료·펀딩비·심볼별 성과·시간대별 승률을 분해하고, 나쁜 습관을 룰 기반으로
자동 진단하며, 본인의 Claude/OpenAI 토큰으로 실데이터 기반 코칭을 받을 수 있습니다.

## 왜 만들었나

선물 잔고가 계속 원점으로 돌아온다면 이유는 보통 셋 중 하나입니다 —
비용(수수료·펀딩)이 갉아먹거나, 특정 습관이 수익을 반납하거나, 애초에 엣지가 없거나.
거래소 앱은 이걸 안 보여줍니다. PnL Coach는 청산 데이터 전체를 긁어서
**어디서 벌고 어디서 새는지** 를 확정해 줍니다.

## 주요 기능

- **멀티거래소 동기화** — 읽기 전용 키 등록 → 30분마다 자동 수집, SQLite 누적
- **손익 대시보드** — 순실현·승률·Profit Factor·평균 익절/손절·수수료·펀딩 손익,
  월별 차트 + 누적 손익 곡선 (다크모드 지원)
- **습관 진단 (룰 기반, LLM 불필요)**
  - 심볼 난사: 수익은 소수 종목, 반납은 다수 종목 패턴 검출
  - 처분효과: 익절은 빨리 자르고 손실은 버티는 홀딩 시간 비대칭
  - 복수매매: 손실 직후 30분 내 재진입 빈도
  - 고레버리지 비중, 취약 시간대, 비용/총익 비율
- **AI 코칭 (BYO token)** — 내 통계·진단을 근거로 한 구체적 행동 규칙 생성.
  서버 운영자가 아니라 **사용자 본인 토큰**으로 호출, 이력 저장
- **멀티유저** — 가입/로그인, 사용자별 키·데이터 완전 분리

## 지원 거래소

| 거래소 | 데이터 소스 | 상태 |
|---|---|---|
| MEXC | contract API `history_positions` | ✅ 안정 (실계좌 검증) |
| Bybit | v5 `closed-pnl` | 🧪 베타 |
| Bitget | v2 mix `history-position` | 🧪 베타 |
| Gate | futures `position_close` | 🧪 베타 |
| Binance (USDⓈ-M) | income 기록 (REALIZED_PNL 이벤트 단위) | 🧪 베타 |

베타 거래소에서 동기화 오류가 나면 UI에 에러 메시지가 그대로 표시됩니다.
이슈로 남겨주시면 반영합니다.

## 빠른 시작

```bash
git clone https://github.com/tenxxxi/pnl-coach.git
cd pnl-coach
./run.sh            # venv 자동 생성 + 의존성 설치 + 기동
```

브라우저에서 `http://localhost:8777` 접속 → 가입 → 끝.

```bash
PORT=9000 ./run.sh   # 포트 변경
```

### 상시 구동 (systemd)

```bash
cp pnl_coach.service ~/.config/systemd/user/
systemctl --user enable --now pnl_coach
```

## 사용법

1. **거래소 키 등록** — 설정 섹션에서 거래소 선택 후 API 키 입력.
   ⚠️ **반드시 읽기 전용 키** (거래·출금 권한 OFF). 이 앱에는 주문·출금 코드가
   존재하지 않지만, 키 권한 자체를 최소화하는 것이 원칙입니다.
2. **동기화** — "지금 동기화" 클릭. 이후 30분마다 자동.
3. **대시보드 확인** — 기간(30일/90일/전체) 바꿔가며 손익 분해 확인.
4. **AI 코칭 (선택)** — AI 토큰 등록 후 "조언 생성".

### AI 토큰 설정

| 방식 | 입력 | 과금 |
|---|---|---|
| Claude API 키 | `sk-ant-api…` | Anthropic 종량 과금 |
| **Claude Pro/Max 구독** | 터미널에서 `claude setup-token` → `sk-ant-oat…` | **구독 요금에 포함** (추가 비용 없음) |
| OpenAI API 키 | `sk-…` | OpenAI 종량 과금 |

토큰 프리픽스로 자동 판별됩니다. ChatGPT Plus 구독에는 API 접근이 포함되지
않으므로 OpenAI는 API 키만 지원합니다. 모델은 비워두면 기본값
(Claude `claude-opus-4-8` / OpenAI `gpt-4o`), 원하는 모델명을 직접 입력해도 됩니다.

## 보안 설계

- 모든 키는 **Fernet 대칭 암호화** 후 `data/pnl_coach.db`에 저장.
  암호화 키는 최초 실행 시 `data/.secret`(권한 600)으로 자동 생성
- 비밀번호는 PBKDF2-HMAC-SHA256 (240k iterations)
- 키는 마스킹된 형태로만 UI에 반환, 평문 재노출 경로 없음
- 조회(read-only) 엔드포인트만 호출 — 주문·출금·이체 코드 없음
- `data/` 는 `.gitignore` 처리 — 포크/배포 시 유출 위험 없음

## FAQ

**Q. 과거 데이터는 얼마나 가져오나요?**
거래소 API가 주는 만큼. MEXC는 청산 이력 약 90일 한도. 등록 후부터는
동기화가 계속 쌓이므로 오래 쓸수록 데이터가 길어집니다.

**Q. 서버에 내 키를 맡기는 게 불안한데요.**
셀프호스팅이 기본 설계입니다. 본인 PC/서버에서 돌리세요.
남의 인스턴스에 키를 넣는 것은 권장하지 않습니다.

**Q. 데이터는 어디에 있나요?**
전부 `data/pnl_coach.db` (SQLite) 한 파일. 백업도 이 파일 하나면 됩니다.

## 고지

- 본 도구의 모든 출력(통계·진단·AI 코칭)은 정보 제공 목적이며 **투자 권유가
  아닙니다.** 투자 판단과 손익의 책임은 사용자 본인에게 있습니다.
- 이 소프트웨어를 **불특정 다수 대상 유료 서비스**로 운영할 경우, 관할 법령
  (한국: 자본시장법상 유사투자자문업 신고 등)의 적용 여부를 직접 확인하세요.
  개인·소규모 무료 사용을 전제로 만들어졌습니다.

## 구조

```
app.py        FastAPI 라우트 + 백그라운드 동기화 루프
exchanges.py  거래소 어댑터 (청산 내역 → 공통 스키마 정규화)
stats.py      통계 집계 + 룰 기반 습관 진단
advisor.py    Claude / OpenAI 코칭 생성
db.py         SQLite 스키마·헬퍼
security.py   비밀번호 해시 + 키 암호화
static/       단일 페이지 UI (의존성 없는 vanilla JS + SVG 차트)
```

## License

[MIT](LICENSE)
