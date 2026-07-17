import asyncio
import aiohttp
import requests
import json
import os
import base64
import re
import random
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from tqdm.asyncio import tqdm_asyncio
from aiohttp_retry import RetryClient, ExponentialRetry
import xml.etree.ElementTree as ET

print("📦 Imports loaded successfully")

# ================= CONFIGURATION =================
MOVIES = [
    {
        "name": "Jana Nayagan",
        "fstIds": [4407, 4413],
        "tgvIds": ["d313fe4c-671c-4ac6-b8fc-c76b9b5dcdea", "a8a96421-9761-4bc8-bf92-963924ba2d2f"],
        "gscId": "5772",
        "dateStart": "2026-07-23",
        "dateEnd": "2026-07-23"
    }
]

CONCURRENCY_SHOWTIMES = 20
CONCURRENCY_SEATMAPS = 50

GITHUB_TOKEN = os.getenv("GH_PAT")
if not GITHUB_TOKEN:
    raise EnvironmentError("Environment variable GH_PAT is not set")

REPO_OWNER = "text2027mail"
REPO_NAME = "malaysiabo2026"

# ================= HELPERS =================
def to_fst_date(d: date) -> str:
    return d.strftime("%d-%m-%Y")

def to_tgv_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")

def to_gsc_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")

def format_display(d: date) -> str:
    return d.strftime("%d %B %Y")

def get_random_user_agent():
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ]
    return random.choice(uas)

# ================= GITHUB HELPERS =================
def github_get_file(path):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]
    elif resp.status_code == 404:
        return None, None
    else:
        raise Exception(f"GitHub GET error {resp.status_code}: {resp.text}")

def github_put_file(path, content, sha=None):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    payload = {
        "message": f"Update {path}",
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "sha": sha,
    }
    resp = requests.put(url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        return True
    else:
        raise Exception(f"GitHub PUT error {resp.status_code}: {resp.text}")

# ================= LOAD / SAVE DATA =================
def load_boxoffice_file(date_obj):
    year = date_obj.strftime("%Y")
    filename = date_obj.strftime("%d-%m.json")
    path = f"my-boxoffice/{year}/{filename}"
    content, _ = github_get_file(path)
    if content is None:
        return []
    try:
        data = json.loads(content)
        if "shows" in data and isinstance(data["shows"], list):
            show_dicts = []
            for arr in data["shows"]:
                if len(arr) >= 15:
                    d = {
                        "showtime_id": arr[0],
                        "date": arr[1],
                        "chain": arr[2],
                        "movie_title": arr[3],
                        "movie_id": arr[4],
                        "theatre": arr[5],
                        "city": arr[6],
                        "state": arr[7],
                        "format": arr[8],
                        "language": arr[9],
                        "totalSeatSold": arr[10],
                        "totalSeatCount": arr[11],
                        "occupancy": arr[12],
                        "adultTicketPrice": arr[13],
                        "grossRevenueMYR": arr[14],
                    }
                    show_dicts.append(d)
            return show_dicts
    except Exception as e:
        print(f"⚠️ Failed to parse boxoffice file {path}: {e}")
    return []

def save_boxoffice_file(date_obj, shows_dict, error_shows=None):
    if not shows_dict:
        print(f"No shows for {date_obj}, skipping boxoffice file.")
        return

    seen = set()
    unique = []
    for s in shows_dict:
        sid = str(s.get("showtime_id"))
        if sid not in seen:
            seen.add(sid)
            unique.append(s)

    compact = []
    for s in unique:
        compact.append([
            s.get("showtime_id"),
            s.get("date"),
            s.get("chain", "Unknown"),
            s.get("movie_title", "Unknown"),
            s.get("movie_id", ""),
            s.get("theatre", "Unknown"),
            s.get("city", ""),
            s.get("state", ""),
            s.get("format", "Standard"),
            s.get("language", "Unknown"),
            s.get("totalSeatSold", 0),
            s.get("totalSeatCount", 0),
            s.get("occupancy", 0.0),
            s.get("adultTicketPrice", 0.0),
            s.get("grossRevenueMYR", 0.0),
        ])

    movie_summary = defaultdict(lambda: {
        "shows": 0,
        "tickets": 0,
        "seats": 0,
        "gross": 0.0,
        "occupancy_sum": 0.0,
    })
    for s in unique:
        if "error" in s:
            continue
        movie_id = s.get("movie_id")
        movie_title = s.get("movie_title", "Unknown")
        key = (movie_id, movie_title)
        summary = movie_summary[key]
        summary["shows"] += 1
        summary["tickets"] += s.get("totalSeatSold", 0)
        summary["seats"] += s.get("totalSeatCount", 0)
        summary["gross"] += s.get("grossRevenueMYR", 0.0)
        summary["occupancy_sum"] += s.get("occupancy", 0.0)

    summary_list = []
    for (movie_id, movie_title), data in sorted(movie_summary.items(), key=lambda x: x[1]["gross"], reverse=True):
        occupancy_avg = round(data["occupancy_sum"] / data["shows"], 2) if data["shows"] else 0.0
        summary_list.append([
            movie_title,
            movie_id,
            data["shows"],
            round(data["gross"], 2),
            occupancy_avg,
            data["tickets"],
            data["seats"],
        ])

    output = {
        "shows": compact,
        "summary": summary_list
    }

    year = date_obj.strftime("%Y")
    base_path = f"my-boxoffice/{year}"

    filename = date_obj.strftime("%d-%m.json")
    path = f"{base_path}/{filename}"
    _, sha = github_get_file(path)
    github_put_file(path, json.dumps(output, separators=(',', ':')), sha)

    error_path = f"{base_path}/{date_obj.strftime('%d-%m')}_errors.json"
    _, sha = github_get_file(error_path)
    error_payload = {
        "last_updated": datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).strftime("%Y-%m-%d %I:%M:%S %p"),
        "errors": error_shows if error_shows else []
    }
    github_put_file(error_path, json.dumps(error_payload, indent=2, ensure_ascii=False), sha)

    logs_path = f"{base_path}/{date_obj.strftime('%d-%m')}_logs.json"
    existing_logs = []
    content, sha = github_get_file(logs_path)
    if content:
        try:
            existing_logs = json.loads(content)
            if not isinstance(existing_logs, list):
                existing_logs = []
        except Exception:
            existing_logs = []

    total_gross = 0.0
    total_shows = 0
    total_sold = 0
    total_capacity = 0
    venues = set()
    for s in unique:
        if "error" in s:
            continue
        total_gross += s.get("grossRevenueMYR", 0.0)
        total_shows += 1
        total_sold += s.get("totalSeatSold", 0)
        total_capacity += s.get("totalSeatCount", 0)
        venues.add(s.get("theatre"))

    avg_occupancy = round((total_sold / total_capacity) * 100, 2) if total_capacity else 0.0
    log_entry = {
        "time": datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).strftime("%Y-%m-%d %I:%M:%S %p"),
        "total_gross_myr": round(total_gross, 2),
        "total_shows": total_shows,
        "avg_occupancy": avg_occupancy,
        "tickets_sold": total_sold,
        "unique_venues": len(venues),
    }
    existing_logs.append(log_entry)
    github_put_file(logs_path, json.dumps(existing_logs, indent=2, ensure_ascii=False), sha)

    print(f"💾 Saved/updated all files for {date_obj} in {REPO_OWNER}/{REPO_NAME}")

# ================= SEMAPHORES =================
seat_sem = asyncio.Semaphore(CONCURRENCY_SEATMAPS)
showtime_sem = asyncio.Semaphore(CONCURRENCY_SHOWTIMES)

# ================= FETCH FUNCTIONS =================

# ---------- FST (LFS) ----------
async def fetch_fst_seat(session, movie_id, cinema_id, show_id, date_str):
    async with seat_sem:
        try:
            url = "https://fst.com.my/SeatLayout/GetSeatLayout"
            data = {
                "CinemaId": cinema_id,
                "ShowId": show_id,
                "MovieId": movie_id,
                "Gender": "All"
            }
            async with session.post(url, data=data, timeout=10) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
                # Count all seat icons
                total = len(re.findall(r'<div class="seat-icons', html))
                booked = len(re.findall(r'class="seat-icons booked-clr', html))
                # Extract adult ticket price – mimics JS logic
                price = 0.0
                # Look for ADULT radio
                match = re.search(r'type-name="ADULT".*?ticket-price="([\d.]+)"', html)
                if match:
                    price = float(match.group(1))
                else:
                    # fallback: any radio with ticket-price
                    match = re.search(r'ticket-price="([\d.]+)"', html)
                    if match:
                        price = float(match.group(1))
                # Gross = booked * adult price (same as JS)
                gross = round(price * booked, 2)
                return {"total": total, "sold": booked, "price": price, "gross": gross}
        except Exception as e:
            print(f"      FST seat fetch error: {e}")
            return None

async def fetch_fst_showtimes(session, movie_id, cinema_id, date_str):
    async with showtime_sem:
        try:
            url = "https://fst.com.my/Movies/GetShowTimes"
            payload = {"cinemaId": cinema_id, "movieId": movie_id, "showDate": date_str}
            async with session.post(url, data=payload, timeout=10) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                showtimes = data.get("Result", [])
                return [{"cinema_id": cinema_id, "show_id": st["Id"]} for st in showtimes]
        except:
            return []

async def fetch_fst_for_date(date_obj, movie_ids):
    date_str = to_fst_date(date_obj)
    shows = []
    async with aiohttp.ClientSession() as session:
        for movie_id in movie_ids:
            print(f"    📽️ FST: Movie ID {movie_id}")
            url_cinemas = "https://fst.com.my/Movies/MovieView"
            payload = {"id": movie_id, "showDate": date_str}
            try:
                async with session.post(url_cinemas, data=payload, timeout=10) as resp:
                    if resp.status != 200:
                        print(f"      ⚠️ Cinema fetch failed (HTTP {resp.status})")
                        continue
                    data = await resp.json()
                    cinemas = data.get("Result", [])
                    if not cinemas:
                        print(f"      ⚠️ No cinemas found for {date_str}")
                        continue
                    print(f"      🏢 Found {len(cinemas)} cinemas")
            except Exception as e:
                print(f"      ❌ Error fetching cinemas: {e}")
                continue

            cinema_tasks = []
            for cinema in cinemas:
                cinema_tasks.append(fetch_fst_showtimes(session, movie_id, cinema["Id"], date_str))
            showtime_results = await asyncio.gather(*cinema_tasks, return_exceptions=True)
            all_shows = []
            for idx, res in enumerate(showtime_results):
                if isinstance(res, list):
                    all_shows.extend(res)
                    print(f"        Cinema {cinemas[idx]['Id']}: {len(res)} showtimes")
                elif isinstance(res, Exception):
                    print(f"        Cinema {cinemas[idx]['Id']}: error - {res}")
            if not all_shows:
                print(f"      ⚠️ No showtimes found for movie {movie_id}")
                continue
            print(f"      🎬 Total showtimes: {len(all_shows)}")

            print(f"      💺 Fetching seat data for {len(all_shows)} shows...")
            seat_tasks = []
            for show in all_shows:
                seat_tasks.append(fetch_fst_seat(session, movie_id, show["cinema_id"], show["show_id"], date_str))
            seat_results = []
            for coro in tqdm_asyncio.as_completed(seat_tasks, desc="      Seats", total=len(seat_tasks), leave=False):
                seat_results.append(await coro)

            for idx, seat_data in enumerate(seat_results):
                if isinstance(seat_data, dict) and seat_data:
                    show = all_shows[idx]
                    shows.append({
                        "showtime_id": f"FST_{show['show_id']}",
                        "date": to_tgv_date(date_obj),
                        "chain": "FST",
                        "movie_title": "",
                        "movie_id": str(movie_id),
                        "theatre": str(show["cinema_id"]),
                        "city": "",
                        "state": "",
                        "format": "Standard",
                        "language": "Unknown",
                        "totalSeatSold": seat_data["sold"],
                        "totalSeatCount": seat_data["total"],
                        "occupancy": round((seat_data["sold"] / seat_data["total"]) * 100, 2) if seat_data["total"] else 0.0,
                        "adultTicketPrice": seat_data["price"],
                        "grossRevenueMYR": seat_data["gross"],
                    })
    return shows

# ---------- TGV ----------
async def fetch_tgv_sessions(session, cinemaid, movieid, date_str):
    async with showtime_sem:
        try:
            url = "https://api.tgv.com.my/api/boxoffice/v1/moviesession_get"
            payload = {"cinemaid": cinemaid, "businessdate": date_str, "movieid": movieid, "retrieveexpired": False}
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                cinema_data = data["results"]["businessday"]["cinemas"][0]
                movies = cinema_data.get("movies", [])
                sessions = []
                for movie in movies:
                    for exp in movie.get("experiences", []):
                        for s in exp.get("sessions", []):
                            sessions.append({"cinemaid": cinemaid, "sessionid": s["sessionid"]})
                return sessions
        except:
            return []

async def fetch_tgv_seat(session, cinemaid, sessionid, date_str):
    async with seat_sem:
        try:
            url_seat = "https://api.tgv.com.my/api/boxoffice/v1/moviesession_getseatplan"
            payload = {"cinemaid": cinemaid, "sessionid": sessionid}
            async with session.post(url_seat, json=payload, timeout=10) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                areas = data["results"]["seatlayout"]["areas"]

            url_ticket = "https://api.tgv.com.my/api/boxoffice/v1/moviesession_gettickets"
            payload_ticket = {"cinemaid": cinemaid, "sessionid": sessionid, "areacategorycodes": "", "usetemplateuser": True}
            async with session.post(url_ticket, json=payload_ticket, timeout=10) as resp2:
                if resp2.status != 200:
                    return None
                ticket_data = await resp2.json()
                tickets = ticket_data["results"]["tickets"]
                price_map = {}
                for t in tickets:
                    code = t["areaCategoryCode"]
                    price = t["priceInCents"] / 100.0
                    if code not in price_map or price > price_map[code]:
                        price_map[code] = price

                total_sold = 0
                total_capacity = 0
                gross = 0.0
                # Iterate areas and rows to count sold and sum prices
                for area in areas:
                    code = area["areaCategoryCode"]
                    price = price_map.get(code, 0.0)
                    for row in area.get("rows", []):
                        for seat in row.get("seats", []):
                            total_capacity += 1
                            if seat.get("status") == 1:  # sold
                                total_sold += 1
                                gross += price
                avg_price = gross / total_sold if total_sold else 0.0
                return {"total": total_capacity, "sold": total_sold, "price": avg_price, "gross": round(gross, 2)}
        except Exception as e:
            print(f"      TGV seat fetch error: {e}")
            return None

async def fetch_tgv_for_date(date_obj, movie_ids):
    date_str = to_tgv_date(date_obj)
    api_base = "https://api.tgv.com.my/api/boxoffice/v1"
    shows = []
    async with aiohttp.ClientSession() as session:
        for movie_id in movie_ids:
            print(f"    📽️ TGV: Movie ID {movie_id}")
            try:
                url_cinemas = f"{api_base}/moviesession_getmoviecinemas"
                payload = {"businessday": date_str, "movieid": movie_id, "experienceGroup": ""}
                async with session.post(url_cinemas, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        print(f"      ⚠️ Cinema fetch failed (HTTP {resp.status})")
                        continue
                    data = await resp.json()
                    cinemas = data["results"]["locations"]
                    all_cinemas = []
                    for loc in cinemas:
                        for c in loc["cinemaids"]:
                            all_cinemas.append({"cinemaid": c["cinemaid"], "state": loc["state"]})
                    if not all_cinemas:
                        print(f"      ⚠️ No cinemas found for {date_str}")
                        continue
                    print(f"      🏢 Found {len(all_cinemas)} cinemas")
            except Exception as e:
                print(f"      ❌ Error fetching cinemas: {e}")
                continue

            cinema_tasks = []
            for cinema in all_cinemas:
                cinema_tasks.append(fetch_tgv_sessions(session, cinema["cinemaid"], movie_id, date_str))
            session_results = await asyncio.gather(*cinema_tasks, return_exceptions=True)
            all_sessions = []
            for idx, res in enumerate(session_results):
                if isinstance(res, list):
                    all_sessions.extend(res)
                    print(f"        Cinema {all_cinemas[idx]['cinemaid']}: {len(res)} sessions")
                elif isinstance(res, Exception):
                    print(f"        Cinema {all_cinemas[idx]['cinemaid']}: error - {res}")
            if not all_sessions:
                print(f"      ⚠️ No sessions found for movie {movie_id}")
                continue
            print(f"      🎬 Total sessions: {len(all_sessions)}")

            print(f"      💺 Fetching seat data for {len(all_sessions)} sessions...")
            seat_tasks = []
            for sess in all_sessions:
                seat_tasks.append(fetch_tgv_seat(session, sess["cinemaid"], sess["sessionid"], date_str))
            seat_results = []
            for coro in tqdm_asyncio.as_completed(seat_tasks, desc="      Seats", total=len(seat_tasks), leave=False):
                seat_results.append(await coro)

            for idx, seat_data in enumerate(seat_results):
                if isinstance(seat_data, dict) and seat_data:
                    sess = all_sessions[idx]
                    shows.append({
                        "showtime_id": f"TGV_{sess['sessionid']}",
                        "date": date_str,
                        "chain": "TGV",
                        "movie_title": "",
                        "movie_id": movie_id,
                        "theatre": str(sess["cinemaid"]),
                        "city": "",
                        "state": "",
                        "format": "Standard",
                        "language": "Unknown",
                        "totalSeatSold": seat_data["sold"],
                        "totalSeatCount": seat_data["total"],
                        "occupancy": round((seat_data["sold"] / seat_data["total"]) * 100, 2) if seat_data["total"] else 0.0,
                        "adultTicketPrice": seat_data["price"],
                        "grossRevenueMYR": seat_data["gross"],
                    })
    return shows

# ---------- GSC ----------
async def fetch_gsc_seat(session, show, date_str):
    async with seat_sem:
        try:
            # Fetch pricing first
            price_url = f"https://epaymentapi.gsc.com.my/showtimews/service.asmx/getTicketPricingEpaySpecialV5?locationid={show['location_id']}&hallid={show['hall']}&filmid={show['film_id']}&showdate={date_str}&showtime={show['time']}"
            async with session.get(price_url, timeout=10) as resp_price:
                if resp_price.status != 200:
                    price_map = {}
                else:
                    price_xml = await resp_price.text()
                    price_root = ET.fromstring(price_xml)
                    price_map = {}
                    for ticket in price_root.findall(".//ticket"):
                        cat = ticket.get("seatcategory")
                        if cat:
                            price = float(ticket.get("price", "0"))
                            price_map[cat] = price
                    # fallback if ADULT not found – use first available
                    if not price_map:
                        first = price_root.find(".//ticket")
                        if first is not None:
                            cat = first.get("seatcategory", "ADULT")
                            price = float(first.get("price", "0"))
                            price_map[cat] = price

            # Fetch seat status
            seat_url = f"https://epaymentapi.gsc.com.my/showtimews/service.asmx/getHallSeatStatus?locationid={show['location_id']}&hallid={show['hall']}&showdate={date_str}&showtime={show['time']}"
            async with session.get(seat_url, timeout=10) as resp_seat:
                if resp_seat.status != 200:
                    return None
                seat_xml = await resp_seat.text()
                seat_root = ET.fromstring(seat_xml)
                cols = seat_root.findall(".//col")
                total = len(cols)
                sold = 0
                gross = 0.0
                for col in cols:
                    status = col.get("status")
                    if status != "A":  # sold
                        sold += 1
                        cat = col.get("seatcategory")
                        price = price_map.get(cat, 0.0)
                        gross += price
                avg_price = gross / sold if sold else 0.0
                return {"total": total, "sold": sold, "price": avg_price, "gross": round(gross, 2)}
        except Exception as e:
            print(f"      GSC seat fetch error: {e}")
            return None

async def fetch_gsc_for_date(date_obj, gsc_id):
    date_str = to_gsc_date(date_obj)
    base_show = f"https://epaymentapi.gsc.com.my/showtimews/service.asmx/getShowTimesByMovie_ParentChild_V2?parentid={gsc_id}&oprndate={date_str}"
    shows = []
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(base_show, timeout=10) as resp:
                if resp.status != 200:
                    print(f"    GSC: fetch failed (HTTP {resp.status})")
                    return []
                xml_text = await resp.text()
                root = ET.fromstring(xml_text)
                show_list = []
                for loc in root.findall(".//location"):
                    theatre = loc.get("name")
                    location_id = loc.get("id")
                    for child in loc.findall("child"):
                        film_id = child.get("code")
                        for show_elem in child.findall("show"):
                            hid = show_elem.get("hid")
                            time = show_elem.get("time")
                            show_list.append({
                                "location_id": location_id,
                                "film_id": film_id,
                                "theatre": theatre,
                                "hall": hid,
                                "time": time,
                            })
                print(f"    GSC: Found {len(show_list)} shows across {len(set(s['location_id'] for s in show_list))} locations")
                if not show_list:
                    return []

                print(f"      💺 Fetching seat data for {len(show_list)} shows...")
                seat_tasks = []
                for show_obj in show_list:
                    seat_tasks.append(fetch_gsc_seat(session, show_obj, date_str))
                seat_results = []
                for coro in tqdm_asyncio.as_completed(seat_tasks, desc="      Seats", total=len(seat_tasks), leave=False):
                    seat_results.append(await coro)

                for idx, seat_data in enumerate(seat_results):
                    if isinstance(seat_data, dict) and seat_data:
                        show_obj = show_list[idx]
                        shows.append({
                            "showtime_id": f"GSC_{show_obj['location_id']}_{show_obj['hall']}_{show_obj['time']}",
                            "date": date_str,
                            "chain": "GSC",
                            "movie_title": "",
                            "movie_id": gsc_id,
                            "theatre": show_obj["theatre"],
                            "city": "",
                            "state": "",
                            "format": "Standard",
                            "language": "Unknown",
                            "totalSeatSold": seat_data["sold"],
                            "totalSeatCount": seat_data["total"],
                            "occupancy": round((seat_data["sold"] / seat_data["total"]) * 100, 2) if seat_data["total"] else 0.0,
                            "adultTicketPrice": seat_data["price"],
                            "grossRevenueMYR": seat_data["gross"],
                        })
        except Exception as e:
            print(f"GSC fetch error for {date_str}: {e}")
    return shows

# ================= MERGE LOGIC =================
def merge_show(old, new):
    if not old:
        return new
    if "error" in new:
        return old
    new_sold = new.get("totalSeatSold", 0)
    old_sold = old.get("totalSeatSold", 0)
    if new_sold > old_sold:
        chosen = new.copy()
    else:
        chosen = old.copy()
    total = chosen.get("totalSeatCount", 0)
    sold = chosen.get("totalSeatSold", 0)
    chosen["occupancy"] = round((sold / total) * 100, 2) if total else 0.0
    # Recompute gross from price * sold (price may have changed)
    price = chosen.get("adultTicketPrice", 0.0)
    chosen["grossRevenueMYR"] = round(price * sold, 2)
    return chosen

# ================= MAIN =================
async def main():
    try:
        tz = ZoneInfo("Asia/Kuala_Lumpur")
    except Exception:
        print("⚠️ ZoneInfo failed; falling back to UTC")
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).date()
    print(f"📅 Today in Malaysia: {today.strftime('%Y-%m-%d')}")

    if not MOVIES:
        print("❌ No movies configured. Exiting.")
        return

    movies_by_date = defaultdict(list)
    for movie in MOVIES:
        start_date = date.fromisoformat(movie["dateStart"])
        end_date = date.fromisoformat(movie["dateEnd"])
        if today <= end_date:
            first_date = max(today, start_date)
            if first_date <= end_date:
                delta_days = (end_date - first_date).days
                for i in range(delta_days + 1):
                    scrape_date = first_date + timedelta(days=i)
                    movies_by_date[scrape_date].append(movie)
        else:
            movies_by_date[today].append(movie)

    for target_date, movies_for_date in movies_by_date.items():
        print(f"\n📅 Processing date: {target_date.strftime('%Y-%m-%d')}")

        existing_shows = load_boxoffice_file(target_date)
        print(f"📂 Loaded {len(existing_shows)} shows from existing boxoffice data (remote).")

        merged_dict = {}
        for s in existing_shows:
            sid = str(s.get("showtime_id"))
            merged_dict[sid] = s

        all_fresh = []
        for movie in movies_for_date:
            movie_name = movie["name"]
            print(f"  🎬 Scraping {movie_name} for {target_date.strftime('%Y-%m-%d')}")
            tasks = []
            if movie.get("fstIds"):
                tasks.append(fetch_fst_for_date(target_date, movie["fstIds"]))
            else:
                tasks.append(asyncio.sleep(0, result=[]))
            if movie.get("tgvIds"):
                tasks.append(fetch_tgv_for_date(target_date, movie["tgvIds"]))
            else:
                tasks.append(asyncio.sleep(0, result=[]))
            if movie.get("gscId"):
                tasks.append(fetch_gsc_for_date(target_date, movie["gscId"]))
            else:
                tasks.append(asyncio.sleep(0, result=[]))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for chain_shows in results:
                if isinstance(chain_shows, list):
                    for s in chain_shows:
                        s["movie_title"] = movie_name
                        all_fresh.append(s)
                elif isinstance(chain_shows, Exception):
                    print(f"    ⚠️ Error in chain fetch: {chain_shows}")

        print(f"  📊 Total fresh shows fetched: {len(all_fresh)}")

        for fresh in all_fresh:
            sid = str(fresh.get("showtime_id"))
            if sid in merged_dict:
                merged_dict[sid] = merge_show(merged_dict[sid], fresh)
            else:
                if "error" not in fresh:
                    merged_dict[sid] = fresh

        merged_shows = list(merged_dict.values())
        print(f"🔄 After merging: {len(merged_shows)} shows.")

        error_shows = [s for s in merged_shows if "error" in s]
        save_boxoffice_file(target_date, merged_shows, error_shows)

    print("\n✅ Done.")

if __name__ == "__main__":
    print("🚀 Script started")
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ CRASH: {e}")
        import traceback
        traceback.print_exc()
        raise
