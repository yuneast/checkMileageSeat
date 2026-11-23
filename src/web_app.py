import asyncio
import datetime
import json
import logging
import os
import subprocess
import sys
from flask import Flask, render_template, request, jsonify

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# src 폴더를 파이썬 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config, save_config, SEAT_CLASS_NAMES, MAJOR_AIRPORTS, DESTINATIONS_BY_REGION
from kal_seat_finder import KALAwardFinder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kal_webapp")

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates"),
    static_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/config", methods=["GET"])
def get_config_api():
    return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
def update_config_api():
    try:
        data = request.json
        save_config(data)
        return jsonify({"success": True, "message": "설정이 성공적으로 저장되었습니다."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 400

@app.route("/api/airports", methods=["GET"])
def get_airports_api():
    return jsonify(MAJOR_AIRPORTS)

@app.route("/api/seat_classes", methods=["GET"])
def get_seat_classes_api():
    return jsonify(SEAT_CLASS_NAMES)

@app.route("/api/regions", methods=["GET"])
def get_regions_api():
    return jsonify(DESTINATIONS_BY_REGION)

def _load_latest_dataset():
    """data/history/의 가장 최신 스냅샷을 읽어온다. 없으면 docs/data/latest_seats.json으로 폴백."""
    if os.path.isdir(HISTORY_DIR):
        files = sorted([f for f in os.listdir(HISTORY_DIR) if f.endswith(".json")], reverse=True)
        if files:
            with open(os.path.join(HISTORY_DIR, files[0]), "r", encoding="utf-8") as f:
                return json.load(f), files[0][:-5]
    fallback_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "data", "latest_seats.json")
    if os.path.isfile(fallback_path):
        with open(fallback_path, "r", encoding="utf-8") as f:
            return json.load(f), None
    return None, None


@app.route("/api/explore", methods=["POST"])
def explore_destinations_api():
    """저장된 최신 스냅샷(data/history/)에서 목적지별 빈 좌석을 조회 (실시간 스크래핑 안 함)"""
    data = request.json or {}
    dep = data.get("departure", "ICN")
    region_key = data.get("region", "ALL")
    custom_airports = data.get("airports", [])
    month = data.get("month")
    classes = set(data.get("seat_classes", ["X", "O", "A", "Z"]))
    direction = data.get("direction", "OUTBOUND")

    if not month:
        now = datetime.datetime.now()
        month = now.strftime("%Y%m")

    # 대상 공항 목록 결정
    target_dests = []
    if custom_airports:
        target_dests = custom_airports
    elif region_key == "ALL":
        for r_info in DESTINATIONS_BY_REGION.values():
            target_dests.extend(r_info["airports"])
        target_dests = list(dict.fromkeys(target_dests))
    elif region_key in DESTINATIONS_BY_REGION:
        target_dests = DESTINATIONS_BY_REGION[region_key]["airports"]
    else:
        target_dests = ["NRT", "KIX", "FUK", "BKK", "DAD", "SIN", "CDG", "HNL"]
    target_dests_set = set(target_dests)

    dataset, snapshot_date = _load_latest_dataset()
    if not dataset:
        return jsonify({
            "success": False,
            "message": "저장된 데이터가 아직 없습니다. 배치(build_static_data.py)가 최소 1회 실행된 뒤에 조회할 수 있습니다."
        }), 404

    routes_data = dataset.get("routes_data", {})
    results = []

    for item in dataset.get("destinations_summary", []):
        if item.get("direction") != direction:
            continue
        if item.get("month") != month:
            continue
        if direction == "OUTBOUND":
            if item.get("departure") != dep or item.get("destination") not in target_dests_set:
                continue
        else:
            if item.get("destination") != dep or item.get("departure") not in target_dests_set:
                continue

        route_key = f"{item['departure']}_{item['destination']}"
        route_flights = routes_data.get(route_key, {}).get("flights", [])
        matched_flights = [
            fl for fl in route_flights
            if fl.get("booking_class") in classes and str(fl.get("date", "")).startswith(month)
        ]
        if not matched_flights:
            continue

        results.append({
            "direction": item["direction"],
            "departure": item["departure"],
            "departure_name": item["departure_name"],
            "destination": item["destination"],
            "destination_name": item["destination_name"],
            "total_available_seats": len(matched_flights),
            "available_classes": sorted(set(fl["booking_class"] for fl in matched_flights)),
            "available_dates": sorted(set(fl["date"] for fl in matched_flights)),
            "sample_flights": matched_flights[:15],
            "all_flights": matched_flights
        })

    results.sort(key=lambda x: x["total_available_seats"], reverse=True)

    return jsonify({
        "success": True,
        "data": results,
        "scanned_count": len(target_dests),
        "found_count": len(results),
        "snapshot_date": snapshot_date,
        "snapshot_updated_at": dataset.get("updated_at")
    })

@app.route("/api/search", methods=["POST"])
def direct_search_api():
    """웹 화면에서 즉시 특정 노선/월을 검색하여 반환"""
    data = request.json or {}
    dep = data.get("departure", "ICN")
    arr = data.get("arrival", "NRT")
    months = data.get("months", [])
    classes = data.get("seat_classes", ["X", "O", "A", "Z"])

    if not months:
        # 기본 이번 달 및 다음 달
        now = datetime.datetime.now()
        months = [now.strftime("%Y%m")]

    async def do_search():
        finder = KALAwardFinder(headless=False)
        try:
            return await finder.search_route_award_seats(
                dep=dep,
                arr=arr,
                months=months,
                target_classes=classes,
                only_available=False  # 전체 캘린더 구성을 위해 빈좌석 여부 모두 가져옴
            )
        finally:
            await finder.close()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(do_search())
        return jsonify({"success": True, "data": results})
    except Exception as e:
        logger.error(f"Search API error: {e}", exc_info=True)
        return jsonify({"success": False, "message": str(e)}), 500

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
BATCH_LOG_PATH = os.path.join(DATA_DIR, "daily_batch.log")
SCAN_PID_FILE = os.path.join(DATA_DIR, "scan.pid")


def is_scan_running() -> bool:
    if not os.path.isfile(SCAN_PID_FILE):
        return False
    try:
        with open(SCAN_PID_FILE, "r") as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # 존재 여부만 확인, 신호는 안 보냄
        return True
    except (OSError, ValueError):
        return False


@app.route("/api/batch_log", methods=["GET"])
def get_batch_log():
    """매일 23:05 launchd 배치(build_static_data.py)의 로그 꼬리와 최근 수집 시각을 반환"""
    lines = []
    if os.path.isfile(BATCH_LOG_PATH):
        with open(BATCH_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-200:]
        lines = [l.rstrip("\n") for l in lines]

    dataset, snapshot_date = _load_latest_dataset()
    updated_at = dataset.get("updated_at") if dataset else None

    return jsonify({
        "success": True,
        "lines": lines,
        "updated_at": updated_at,
        "snapshot_date": snapshot_date,
        "is_running": is_scan_running()
    })


@app.route("/api/trigger_scan", methods=["POST"])
def trigger_scan():
    """지금 바로 전체 스캔(build_static_data.py)을 백그라운드로 시작. 이미 돌고 있으면 거부."""
    if is_scan_running():
        return jsonify({"success": False, "message": "이미 수집이 진행 중입니다. 로그 탭에서 진행상황을 확인하세요."})

    src_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(src_dir, "build_static_data.py")

    os.makedirs(DATA_DIR, exist_ok=True)
    log_f = open(BATCH_LOG_PATH, "a", encoding="utf-8")
    log_f.write(f"\n===== 수동 수집 시작: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    log_f.flush()

    proc = subprocess.Popen(
        [sys.executable, script_path, "14"],
        cwd=src_dir,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True
    )
    with open(SCAN_PID_FILE, "w") as f:
        f.write(str(proc.pid))

    return jsonify({"success": True, "message": "수집을 시작했습니다. 진행상황은 로그 탭에서 확인할 수 있습니다."})

@app.route("/api/history", methods=["GET"])
def list_history():
    """저장된 날짜별 배치 결과 목록 (최신순)"""
    if not os.path.isdir(HISTORY_DIR):
        return jsonify({"success": True, "dates": []})
    dates = sorted(
        [f[:-5] for f in os.listdir(HISTORY_DIR) if f.endswith(".json")],
        reverse=True
    )
    return jsonify({"success": True, "dates": dates})

@app.route("/api/history/<date_str>", methods=["GET"])
def get_history_day(date_str):
    """특정 날짜(YYYY-MM-DD)의 저장된 배치 결과 조회"""
    safe_name = os.path.basename(date_str)
    path = os.path.join(HISTORY_DIR, f"{safe_name}.json")
    if not os.path.isfile(path):
        return jsonify({"success": False, "message": "해당 날짜의 저장된 데이터가 없습니다."}), 404
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return jsonify({"success": True, "data": data})

def run_server(port: int = 5000):
    print(f"\n========================================================")
    print(f"🚀 대한항공 마일리지 빈좌석 모니터링 웹 대시보드 실행")
    print(f"🌐 웹 브라우저 주소: http://127.0.0.1:{port}")
    print(f"========================================================\n")
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    run_server()
