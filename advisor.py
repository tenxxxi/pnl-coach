"""LLM 트레이딩 코치 — 사용자 본인 토큰으로 Claude/GPT 호출."""
import json

import requests

SYSTEM = (
    "너는 냉정한 선물 트레이딩 코치다. 제공된 통계와 룰 기반 진단은 사용자의 "
    "실제 거래 데이터에서 계산된 것이다. 데이터에 근거해서만 말하고, 일반론은 금지. "
    "목표는 승률과 기대값 개선. 한국어로, 구체적 행동 지침 중심으로 답하라. "
    "형식: ① 핵심 문제 진단 (2-3개, 숫자 인용) ② 즉시 실행할 행동 규칙 (3-5개, "
    "측정 가능하게) ③ 다음 검토 시 확인할 지표. 투자 권유가 아닌 데이터 코칭임을 "
    "마지막에 한 줄로 고지."
)


def build_user_prompt(stats: dict, findings: list[dict]) -> str:
    compact = {k: v for k, v in stats.items() if k not in ("curve", "hourly")}
    compact["symbols_top"] = stats["symbols"][-8:]
    compact["symbols_bottom"] = stats["symbols"][:8]
    del compact["symbols"]
    return (
        "다음은 내 선물 거래 통계(JSON)와 룰 기반 진단이다.\n\n"
        f"통계:\n{json.dumps(compact, ensure_ascii=False)}\n\n"
        f"진단:\n{json.dumps(findings, ensure_ascii=False)}\n\n"
        "승률과 손익비를 개선하기 위한 코칭을 해달라."
    )


def advise_anthropic(api_key: str, model: str | None, stats: dict,
                     findings: list[dict]) -> str:
    import anthropic
    if api_key.startswith("sk-ant-oat"):
        # 구독(Pro/Max) OAuth 토큰 — Bearer 인증 + oauth 베타 헤더, 구독 요금으로 청구
        client = anthropic.Anthropic(
            auth_token=api_key,
            default_headers={"anthropic-beta": "oauth-2025-04-20"},
        )
    else:
        client = anthropic.Anthropic(api_key=api_key)
    model = model or "claude-opus-4-8"
    kwargs = {}
    if "haiku" not in model:
        kwargs["thinking"] = {"type": "adaptive"}
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM,
        messages=[{"role": "user", "content": build_user_prompt(stats, findings)}],
        **kwargs,
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def advise_openai(api_key: str, model: str | None, stats: dict,
                  findings: list[dict]) -> str:
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model or "gpt-4o",
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": build_user_prompt(stats, findings)},
            ],
            "max_tokens": 4096,
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def generate(provider: str, api_key: str, model: str | None, stats: dict,
             findings: list[dict]) -> str:
    if provider == "anthropic":
        return advise_anthropic(api_key, model, stats, findings)
    if provider == "openai":
        return advise_openai(api_key, model, stats, findings)
    raise ValueError(f"unknown provider: {provider}")
