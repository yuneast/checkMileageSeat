import asyncio
import datetime
import json
import logging
import os
import random
import sys

# Ensure utf-8 stdout on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import MAJOR_AIRPORTS, DESTINATIONS_BY_REGION, load_config
from kal_seat_finder import KALAwardFinder
from notifier import TelegramNotifier
from tracker import SeatTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_static_data")

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "data", "latest_seats.json")
DATA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
HISTORY_DIR = os.path.join(DATA_ROOT, "history")
HISTORY_RETENTION_DAYS = 30
# web_app.py의 /api/trigger_scan 이 보는 것과 동일한 잠금파일 경로.
# launchd/버튼/수동실행 어떤 경로로 시작하든 이 파일 하나로 중복 실행을 막는다.
SCAN_PID_FILE = os.path.join(DATA_ROOT, "scan.pid")


def acquire_lock_or_exit():
    """이미 다른 build_static_data.py가 돌고 있으면(크롬 프로필 충돌 방지) 즉시 종료.
    (web_app.py가 Popen 직후 자식 PID를 미리 적어두는 경우, 파일의 PID가 내 PID와
    같으면 '이미 내가 등록된 것'이므로 통과시킨다.)"""
    my_pid = os.getpid()
    if os.path.isfile(SCAN_PID_FILE):
        try:
            with open(SCAN_PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
        except ValueError:
            logger.warning("잠금파일 내용이 손상됨 - 정리하고 계속 진행합니다.")
            old_pid = None

        if old_pid is not None and old_pid != my_pid:
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                logger.warning("오래된 잠금파일 발견(죽은 프로세스) - 정리하고 계속 진행합니다.")
            except PermissionError:
                # 신호를 보낼 권한은 없지만 프로세스 자체는 존재함 -> 실행 중으로 간주
                logger.error(f"이미 다른 수집 프로세스(PID {old_pid})가 실행 중인 것으로 보입니다. 중복 실행을 막기 위해 종료합니다.")
                sys.exit(1)
            else:
                logger.error(f"이미 다른 수집 프로세스(PID {old_pid})가 실행 중입니다. 중복 실행을 막기 위해 종료합니다.")
                sys.exit(1)

    os.makedirs(DATA_ROOT, exist_ok=True)
    with open(SCAN_PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    try:
        if os.path.isfile(SCAN_PID_FILE):
            with open(SCAN_PID_FILE, "r") as f:
                pid_in_file = f.read().strip()
            if pid_in_file == str(os.getpid()):
                os.remove(SCAN_PID_FILE)
    except OSError:
        pass


def today_history_path() -> str:
    return os.path.join(HISTORY_DIR, f"{datetime.date.today().isoformat()}.json")


def load_today_checkpoint():
    """오늘 날짜 history 파일이 이미 있으면(이전 실행이 중간에 죽었던 경우) 불러와서
    이어서 스캔할 수 있도록 (완료된 노선 데이터, 완료된 노선 키 집합)을 반환합니다."""
    path = today_history_path()
    if not os.path.isfile(path):
        return {}, [], set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        all_routes_data = data.get("routes_data", {})
        destinations_summary = data.get("destinations_summary", [])
        completed = set(all_routes_data.keys())
        if completed:
            logger.info(f"오늘자 체크포인트 발견: 이미 완료된 노선 {len(completed)}개 - 이어서 스캔합니다.")
        return all_routes_data, destinations_summary, completed
    except Exception as e:
        logger.warning(f"체크포인트 로드 실패({e}) - 처음부터 다시 시작합니다.")
        return {}, [], set()


def write_dataset_files(dataset: dict):
    """현재까지 누적된 데이터셋을 latest_seats.json과 오늘자 history 파일에 즉시 반영(체크포인트 겸용)."""
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, separators=(',', ':'))

    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(today_history_path(), "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, separators=(',', ':'))


def cleanup_old_history():
    """30일 지난 history 파일 삭제. 전체 스캔이 끝난 뒤 한 번만 호출."""
    if not os.path.isdir(HISTORY_DIR):
        return
    cutoff = datetime.date.today() - datetime.timedelta(days=HISTORY_RETENTION_DAYS)
    for fname in os.listdir(HISTORY_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            file_date = datetime.date.fromisoformat(fname[:-5])
        except ValueError:
            continue
        if file_date < cutoff:
            os.remove(os.path.join(HISTORY_DIR, fname))
            logger.info(f"오래된 history 파일 삭제: {fname}")


def notify_new_seats(destinations_summary: list):
    """오늘 스캔 결과 중 '자동 감시 노선 목록'(config.json routes)에 등록된 노선만
    이전 캐시와 비교해서, 신규로 열린 좌석이 있으면 텔레그램으로 알림.
    (routes가 비어있으면 알림 대상도 없음 - 전체 스캔 결과를 멋대로 알리지 않음)"""
    cfg = load_config()
    tg = cfg.get("telegram", {})
    notifier = TelegramNotifier(
        bot_token=tg.get("bot_token", ""),
        chat_id=tg.get("chat_id", ""),
        enabled=tg.get("enabled", False)
    )
    if not notifier.is_configured():
        logger.info("텔레그램 미설정 - 알림 생략")
        return

    routes = cfg.get("routes", [])
    if not routes:
        logger.info("자동 감시 노선 목록이 비어있음 - 알림 생략 (노선 관리 탭에서 노선을 추가하세요)")
        return

    # 노선별 허용 월/좌석등급을 빠르게 조회하기 위한 맵: (departure, arrival) -> {months:set, classes:set}
    route_filters = {}
    for r in routes:
        key = (r.get("departure"), r.get("arrival"))
        route_filters[key] = {
            "months": set(r.get("months", [])),
            "classes": set(r.get("seat_classes", []))
        }

    filtered_summary = []
    for item in destinations_summary:
        key = (item.get("departure"), item.get("destination"))
        rf = route_filters.get(key)
        if not rf:
            continue
        if item.get("month") not in rf["months"]:
            continue
        matched_classes = [c for c in item.get("classes", []) if c in rf["classes"]]
        if not matched_classes:
            continue
        filtered_item = dict(item)
        filtered_item["classes"] = matched_classes
        filtered_summary.append(filtered_item)

    if not filtered_summary:
        logger.info("감시 노선 목록 기준으로 신규 대상 없음 - 알림 생략")
        return

    tracker = SeatTracker(cache_file=os.path.join(DATA_ROOT, "seat_cache.json"))

    flat_seats = []
    for item in filtered_summary:
        for d in item.get("dates", []):
            for cls in item.get("classes", []):
                flat_seats.append({
                    "departure": item["departure"],
                    "departure_name": item["departure_name"],
                    "arrival": item["destination"],
                    "arrival_name": item["destination_name"],
                    "date": d,
                    "flight_number": f"{item['departure']}-{item['destination']}",
                    "booking_class": cls,
                })

    new_seats = tracker.update_and_get_new_seats(flat_seats)
    if new_seats:
        notifier.notify_seats(new_seats)
        logger.info(f"텔레그램 알림 전송: 신규 좌석 {len(new_seats)}건")
    else:
        logger.info("신규 좌석 없음 - 알림 생략")

async def build_static_dataset(max_months: int = 14, target_departure: str = "ICN"):
    """
    주요 목적지들의 마일리지 좌석을 일괄 스캔하여 GitHub Pages용 정적 데이터셋(latest_seats.json)을 생성합니다.
    대한항공 마일리지 예약 가능한 최대 기간(향후 361일, 당월 포함 최대 14개월)을 지원합니다.
    """
    acquire_lock_or_exit()
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    # 한국 표준시(KST) 기준 현재 일자 및 361일(대한항공 마일리지 예약 가능 기간) 산출
    KST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(KST)
    today = now.date()
    max_date = today + datetime.timedelta(days=361)

    today_str = today.strftime("%Y%m%d")
    max_date_str = max_date.strftime("%Y%m%d")
    now_time_str = now.strftime("%H:%M")

    # 오늘이 속한 년월부터 361일 후가 속한 년월까지 동적 산출 (과거 월 자동 배제, 내년 오픈 월 포함)
    curr_y, curr_m = today.year, today.month
    end_y, end_m = max_date.year, max_date.month
    months_to_scan = []
    while (curr_y < end_y) or (curr_y == end_y and curr_m <= end_m):
        months_to_scan.append(f"{curr_y}{curr_m:02d}")
        curr_m += 1
        if curr_m > 12:
            curr_m = 1
            curr_y += 1

    # 스캔 대상 목적지: DESTINATIONS_BY_REGION(config.py)에 등록된 전체 공항을 그대로 사용
    # (하드코딩 목록을 따로 두면 설정과 실제 스캔 대상이 어긋나기 쉬워서 단일 소스로 통일)
    key_destinations = []
    for region_info in DESTINATIONS_BY_REGION.values():
        key_destinations.extend(region_info["airports"])
    key_destinations = list(dict.fromkeys(key_destinations))  # 중복 제거, 순서 유지
    if target_departure in key_destinations:
        key_destinations.remove(target_departure)

    # 스캔 대상 노선 목록 (출국편: ICN ➔ 해외 / 귀국편: 해외 ➔ ICN)
    routes_to_scan = []
    # 1. 서울 출발 (OUTBOUND)
    for city in key_destinations:
        if city != target_departure:
            routes_to_scan.append({
                "direction": "OUTBOUND",
                "dep": target_departure,
                "arr": city
            })
    # 2. 서울 귀국 (INBOUND)
    for city in key_destinations:
        if city != target_departure:
            routes_to_scan.append({
                "direction": "INBOUND",
                "dep": city,
                "arr": target_departure
            })

    finder = KALAwardFinder(headless=False)
    all_routes_data, destinations_summary, completed_route_keys = load_today_checkpoint()

    def current_dataset() -> dict:
        return {
            "updated_at": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S (KST)"),
            "departure": target_departure,
            "departure_name": MAJOR_AIRPORTS.get(target_departure, target_departure),
            "booking_window_days": 361,
            "start_date": today_str,
            "end_date": max_date_str,
            "months": months_to_scan,
            "airports": MAJOR_AIRPORTS,
            "regions": DESTINATIONS_BY_REGION,
            "destinations_summary": destinations_summary,
            "routes_data": all_routes_data
        }

    try:
        logger.info(f"GitHub Pages 데이터 빌드 시작: 대상 노선={len(routes_to_scan)}개 (출국 {len(key_destinations)}개 + 귀국 {len(key_destinations)}개), 월 목록={months_to_scan}")
        if completed_route_keys:
            logger.info(f"남은 노선: {len(routes_to_scan) - len(completed_route_keys)}개")

        today_str = now.strftime("%Y%m%d")
        now_time_str = now.strftime("%H:%M")

        for idx, r in enumerate(routes_to_scan):
            route_key_check = f"{r['dep']}_{r['arr']}"
            if route_key_check in completed_route_keys:
                continue
            if idx > 0 and idx % 12 == 0:
                logger.info(f"스캔 진행 중({idx}/{len(routes_to_scan)}): 브라우저 안정성을 위해 세션을 클린 갱신합니다...")
                try:
                    await finder.refresh_session()
                except Exception as e:
                    logger.warning(f"세션 주기 갱신 예외({e}), 계속 진행...")

            direction = r["direction"]
            dep = r["dep"]
            arr = r["arr"]
            dep_name = MAJOR_AIRPORTS.get(dep, dep)
            arr_name = MAJOR_AIRPORTS.get(arr, arr)

            route_key = f"{dep}_{arr}"
            logger.info(f"스캔 중 [{direction}]: {dep_name}({dep}) ➔ {arr_name}({arr}) ...")

            all_routes_data[route_key] = {
                "direction": direction,
                "departure": dep,
                "departure_name": dep_name,
                "arrival": arr,
                "arrival_name": arr_name,
                "flights": []
            }

            for ym in months_to_scan:
                try:
                    month_seats = await finder.fetch_month_seats(dep, arr, ym)

                    # 과거 날짜, 361일 초과 미래 항공편, 이미 출발한 당일 항공편 제외 처리
                    for s in month_seats:
                        f_date = s.get("date", "")
                        f_time = s.get("departure_time", "")
                        if f_date < today_str or f_date > max_date_str:
                            s["available"] = False
                        elif f_date == today_str and f_time and f_time <= now_time_str:
                            s["available"] = False

                    # 유효한 빈좌석만 필터링 및 경량화 저장
                    avail = [s for s in month_seats if s.get("available")]
                    compact_avail = [
                        {
                            "date": s["date"],
                            "flight_number": s["flight_number"],
                            "departure_time": s.get("departure_time", ""),
                            "booking_class": s["booking_class"]
                        }
                        for s in avail
                    ]
                    all_routes_data[route_key]["flights"].extend(compact_avail)

                    if avail:
                        destinations_summary.append({
                            "direction": direction,
                            "departure": dep,
                            "departure_name": dep_name,
                            "destination": arr,
                            "destination_name": arr_name,
                            "month": ym,
                            "total_seats": len(avail),
                            "classes": sorted(list(set(s["booking_class"] for s in avail))),
                            "dates": sorted(list(set(s["date"] for s in avail)))
                        })

                    # WAF 부하 방지를 위한 랜덤 딜레이 (0.8s ~ 1.5s)
                    await asyncio.sleep(random.uniform(0.8, 1.5))
                except Exception as e:
                    logger.error(f"  [{direction}] {dep}->{arr} {ym} 실패: {e}")

            # 이 노선의 전체 월 스캔이 끝났으므로 체크포인트 겸 즉시 저장
            completed_route_keys.add(route_key)
            write_dataset_files(current_dataset())

            # 노선 간 쿨다운 (1.5s ~ 2.5s)
            await asyncio.sleep(random.uniform(1.5, 2.5))

        # 노선마다 이미 즉시 저장(체크포인트)해왔으므로 여기선 완료 집계만 함
        total_seats_found = sum(len(r.get("flights", [])) for r in all_routes_data.values())
        if len(completed_route_keys) < len(routes_to_scan):
            logger.warning(
                f"⚠️ 일부 노선 실패: {len(completed_route_keys)}/{len(routes_to_scan)}개 완료. "
                f"이미 완료된 노선까지는 저장되어 있으니, 같은 명령으로 다시 실행하면 나머지부터 이어서 스캔합니다."
            )
        logger.info(f"성공! 총 {total_seats_found}건의 좌석 데이터셋이 저장되었습니다: {OUTPUT_PATH}")
        logger.info(f"날짜별 기록 저장됨: {today_history_path()}")

        cleanup_old_history()

        try:
            notify_new_seats(destinations_summary)
        except Exception as e:
            logger.error(f"텔레그램 알림 처리 중 오류: {e}")

    finally:
        await finder.close()
        release_lock()

if __name__ == "__main__":
    # 기본 14개월(최대 예약 가능 범위 361일) 빌드
    months_count = 14
    if len(sys.argv) > 1:
        try:
            months_count = int(sys.argv[1])
        except ValueError:
            pass

    asyncio.run(build_static_dataset(max_months=months_count))
