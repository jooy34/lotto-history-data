from __future__ import annotations

import json
import time
from pathlib import Path

import requests


API_URL = "https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do"
OUTPUT_PATH = Path("lotto_draws.json")

REQUEST_DELAY_SECONDS = 1.2
TIMEOUT_SECONDS = 10

MAX_RETRIES_PER_DRAW = 5
RETRY_DELAY_SECONDS = 3


def _format_date(yyyymmdd: str) -> str:
    """
    '20260411' -> '2026-04-11'
    """
    if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return yyyymmdd

    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def load_existing_draws(output_path: Path) -> list[dict]:
    if not output_path.exists():
        return []

    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] 기존 JSON 로드 실패: {exc}")
        return []

    if not isinstance(data, list):
        print("[WARN] 기존 JSON 형식이 리스트가 아닙니다.")
        return []

    valid = [item for item in data if isinstance(item, dict) and "drawNo" in item]
    valid.sort(key=lambda x: x["drawNo"])
    return valid


def backup_existing_file(output_path: Path) -> None:
    if not output_path.exists():
        return

    backup_path = output_path.with_suffix(".backup.json")
    backup_path.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")


def fetch_draw(draw_no: int) -> dict | None:
    """
    특정 회차 데이터를 가져온다.
    성공 시 앱 내부 JSON 형식(dict)으로 변환해서 반환한다.
    회차가 없거나 응답이 비정상이면 None 반환.
    """
    params = {
        "srchStrLtEpsd": draw_no,
        "srchEndLtEpsd": draw_no,
    }

    response = requests.get(
        API_URL,
        params=params,
        timeout=TIMEOUT_SECONDS,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.dhlottery.co.kr/",
        },
    )
    response.raise_for_status()

    text = response.text.strip()

    if text.startswith("<!DOCTYPE") or text.startswith("<html") or "<html" in text.lower():
        print(f"[HTML] {draw_no}회 응답이 JSON이 아니라 HTML입니다.")
        print(text[:200])
        return None

    try:
        payload = response.json()
    except ValueError:
        print(f"[JSON ERROR] {draw_no}회 응답을 JSON으로 해석하지 못했습니다.")
        print(text[:200])
        return None

    data = payload.get("data", {})
    draw_list = data.get("list", [])

    if not draw_list:
        return None

    row = draw_list[0]

    if not row.get("ltEpsd"):
        return None

    return {
        "drawNo": row["ltEpsd"],
        "drawDate": _format_date(str(row["ltRflYmd"])),
        "numbers": [
            row["tm1WnNo"],
            row["tm2WnNo"],
            row["tm3WnNo"],
            row["tm4WnNo"],
            row["tm5WnNo"],
            row["tm6WnNo"],
        ],
        "bonusNumber": row["bnsWnNo"],
        "firstWinAmount": row.get("rnk1WnAmt"),
        "firstPrizeWinnerCount": row.get("rnk1WnNope"),
    }


def fetch_draw_with_retry(draw_no: int) -> dict | None:
    for attempt in range(1, MAX_RETRIES_PER_DRAW + 1):
        try:
            draw = fetch_draw(draw_no)
        except requests.RequestException as exc:
            print(f"[ERROR] {draw_no}회 요청 실패 (시도 {attempt}/{MAX_RETRIES_PER_DRAW}): {exc}")
            draw = None

        if draw is not None:
            return draw

        if attempt < MAX_RETRIES_PER_DRAW:
            print(f"[RETRY] {draw_no}회 {RETRY_DELAY_SECONDS}초 후 재시도")
            time.sleep(RETRY_DELAY_SECONDS)

    print(f"[FAIL] {draw_no}회 최종 실패")
    return None


def fetch_all_draws(
    start_draw_no: int = 1,
    max_consecutive_failures: int = 3,
    full_scan_mode: bool = False,
) -> list[dict]:
    """
    start_draw_no부터 최신 회차까지 순차 조회.
    - full_scan_mode=True  : 초기 전체 수집용 (실패 허용 폭 크게)
    - full_scan_mode=False : 증분 갱신용 (최신 회차 지난 것으로 빠르게 판단)
    """
    results: list[dict] = []
    consecutive_failures = 0
    draw_no = start_draw_no

    allowed_failures = 10 if full_scan_mode else max_consecutive_failures

    while True:
        draw = fetch_draw_with_retry(draw_no)

        if draw is None:
            print(f"[END?] {draw_no}회 데이터 없음 또는 수집 실패")
            consecutive_failures += 1

            if consecutive_failures >= allowed_failures:
                if full_scan_mode:
                    print("[STOP] 전체 수집 중 연속 실패가 많아 종료합니다.")
                else:
                    print("[DONE] 최신 회차까지 수집한 것으로 보고 종료합니다.")
                break
        else:
            results.append(draw)
            consecutive_failures = 0
            print(
                f"[OK] {draw['drawNo']}회 "
                f"{draw['drawDate']} "
                f"{draw['numbers']} + {draw['bonusNumber']}"
            )

        draw_no += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    return results


def save_json(draws: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    draws = sorted(draws, key=lambda x: x["drawNo"])

    output_path.write_text(
        json.dumps(draws, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    existing_draws = load_existing_draws(OUTPUT_PATH)

    if existing_draws:
        last_draw_no = max(draw["drawNo"] for draw in existing_draws)
        start_draw_no = last_draw_no + 1
        print(f"기존 데이터 {len(existing_draws)}개 로드 완료")
        print(f"{start_draw_no}회부터 신규 회차 확인 시작")
    else:
        start_draw_no = 1
        print("기존 데이터 없음")
        print("1회부터 전체 수집 시작")

    new_draws = fetch_all_draws(
        start_draw_no=start_draw_no,
        full_scan_mode=(len(existing_draws) == 0),
    )

    if existing_draws and not new_draws:
        print("신규 회차 없음")
        return

    all_draws = existing_draws + new_draws

    deduped = {draw["drawNo"]: draw for draw in all_draws}
    final_draws = sorted(deduped.values(), key=lambda x: x["drawNo"])

    if not final_draws:
        raise RuntimeError("수집된 회차 데이터가 없습니다.")

    backup_existing_file(OUTPUT_PATH)
    save_json(final_draws, OUTPUT_PATH)

    print()
    print(f"총 {len(final_draws)}개 회차 저장 완료")
    print(f"이번 실행 신규 추가: {len(new_draws)}개")
    print(f"파일 위치: {OUTPUT_PATH.resolve()}")


if __name__ == "__main__":
    main()
