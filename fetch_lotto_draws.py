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
MAX_CONSECUTIVE_MISSING = 3


class FetchNetworkError(RuntimeError):
    pass


def _format_date(yyyymmdd: str) -> str:
    """
    '20260411' -> '2026-04-11'
    """
    if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return yyyymmdd

    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def load_existing_draws(output_path: Path) -> list[dict]:
    if not output_path.exists():
        raise RuntimeError(
            "기존 lotto_draws.json 파일이 없습니다. "
            "GitHub Actions에서는 전체 수집을 하지 말고, "
            "로컬에서 완성한 전체 JSON을 먼저 업로드해주세요."
        )

    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"기존 JSON 로드 실패: {exc}") from exc

    if not isinstance(data, list):
        raise RuntimeError("기존 JSON 형식이 리스트가 아닙니다.")

    valid = [item for item in data if isinstance(item, dict) and "drawNo" in item]
    valid.sort(key=lambda x: x["drawNo"])

    if not valid:
        raise RuntimeError(
            "기존 JSON이 비어 있습니다. "
            "GitHub Actions에서는 전체 수집을 하지 말고, "
            "로컬에서 전체 데이터를 먼저 채워 넣어주세요."
        )

    return valid


def backup_existing_file(output_path: Path) -> None:
    if not output_path.exists():
        return

    backup_path = output_path.with_suffix(".backup.json")
    backup_path.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")


def fetch_draw_once(draw_no: int) -> tuple[str, dict | None]:
    """
    반환값:
    - ("success", draw_dict)
    - ("missing", None)        : 진짜 없는 회차로 보이는 경우
    예외:
    - FetchNetworkError        : 네트워크/타임아웃/HTML 응답 등 비정상
    """
    params = {
        "srchStrLtEpsd": draw_no,
        "srchEndLtEpsd": draw_no,
    }

    try:
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
    except requests.RequestException as exc:
        raise FetchNetworkError(f"{draw_no}회 요청 실패: {exc}") from exc

    text = response.text.strip()

    if text.startswith("<!DOCTYPE") or text.startswith("<html") or "<html" in text.lower():
        raise FetchNetworkError(
            f"{draw_no}회 응답이 JSON이 아니라 HTML입니다. "
            f"서버 차단/오류 가능성"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise FetchNetworkError(
            f"{draw_no}회 응답을 JSON으로 해석하지 못했습니다."
        ) from exc

    data = payload.get("data", {})
    draw_list = data.get("list", [])

    if not draw_list:
        return ("missing", None)

    row = draw_list[0]

    if not row.get("ltEpsd"):
        return ("missing", None)

    return (
        "success",
        {
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
        },
    )


def fetch_draw_with_retry(draw_no: int) -> tuple[str, dict | None]:
    """
    success / missing 반환
    네트워크 실패는 재시도 후에도 안 되면 예외 발생
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES_PER_DRAW + 1):
        try:
            result_type, draw = fetch_draw_once(draw_no)
            return (result_type, draw)
        except FetchNetworkError as exc:
            last_error = exc
            print(f"[ERROR] {exc} (시도 {attempt}/{MAX_RETRIES_PER_DRAW})")

            if attempt < MAX_RETRIES_PER_DRAW:
                print(f"[RETRY] {draw_no}회 {RETRY_DELAY_SECONDS}초 후 재시도")
                time.sleep(RETRY_DELAY_SECONDS)

    raise FetchNetworkError(f"{draw_no}회 최종 실패: {last_error}")


def fetch_incremental_draws(start_draw_no: int) -> list[dict]:
    """
    기존 마지막 회차 다음부터 신규 회차만 조회.
    - success면 추가
    - missing이 연속 3회 나오면 최신 회차 이후로 보고 종료
    - network error는 실패로 종료
    """
    results: list[dict] = []
    consecutive_missing = 0
    draw_no = start_draw_no

    while True:
        result_type, draw = fetch_draw_with_retry(draw_no)

        if result_type == "missing":
            consecutive_missing += 1
            print(f"[MISSING] {draw_no}회 데이터 없음")

            if consecutive_missing >= MAX_CONSECUTIVE_MISSING:
                print("[DONE] 최신 회차까지 확인 완료")
                break
        else:
            assert draw is not None
            results.append(draw)
            consecutive_missing = 0
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

    last_draw_no = max(draw["drawNo"] for draw in existing_draws)
    start_draw_no = last_draw_no + 1

    print(f"기존 데이터 {len(existing_draws)}개 로드 완료")
    print(f"마지막 회차: {last_draw_no}회")
    print(f"{start_draw_no}회부터 신규 회차 확인 시작")

    new_draws = fetch_incremental_draws(start_draw_no=start_draw_no)

    if not new_draws:
        print("신규 회차 없음")
        return

    all_draws = existing_draws + new_draws
    deduped = {draw["drawNo"]: draw for draw in all_draws}
    final_draws = sorted(deduped.values(), key=lambda x: x["drawNo"])

    backup_existing_file(OUTPUT_PATH)
    save_json(final_draws, OUTPUT_PATH)

    print()
    print(f"총 {len(final_draws)}개 회차 저장 완료")
    print(f"이번 실행 신규 추가: {len(new_draws)}개")
    print(f"파일 위치: {OUTPUT_PATH.resolve()}")


if __name__ == "__main__":
    main()
