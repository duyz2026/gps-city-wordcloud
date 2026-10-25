# -*- coding: utf-8 -*-
"""把只有坐标的记录补成城市 —— 腾讯地图版

用途
----
手上的运动手表、行车记录、照片 EXIF 里只有经纬度，没有地名。
本脚本把每一行坐标送去腾讯地图「逆地址解析」，换回 省 / 市 / 区。

用法
----
    python geocode_tencent.py --in points.csv --key 你的KEY --out cities.csv

    # 先拿 20 条试跑，确认能通再全量
    python geocode_tencent.py --in points.csv --key 你的KEY --out demo.csv --limit 20

    # 加速（个人 key 一般 5~10 QPS，别开太大，超了会被限流）
    python geocode_tencent.py --in points.csv --key 你的KEY --out cities.csv --qps 8

输入
----
    CSV，至少两列坐标。表头支持：lat / latitude / 纬度，lon / lng / longitude / 经度
    其它列原样保留，不做任何改动。

输出
----
    原表 + 六列：province, city, district, adcode, level, note

    level 用来标「这个判定有多可信」：
        A  省市区都拿到了          —— 可以直接用
        B  有市、没区（常见于郊县）—— 可以用
        C  只到省                 —— 城市统计时单独归一类，别硬塞
        D  判定不了（境外/海上/无返回）—— 留空，不猜

为什么要做 WGS-84 → GCJ-02 转换
-------------------------------
    运动手表记的是 WGS-84（GPS 原始坐标），腾讯地图用的是 GCJ-02（国测局坐标）。
    两者在国内差几百米。在城市中心无所谓，但在城市边界，差几百米就会把一条记录
    判到隔壁市去。所以送进去之前必须先转。

申请 key
--------
    https://lbs.qq.com/dev/console/key/manage
    个人开发者免费额度够跑几千条。key 不要写进代码，用命令行传或放环境变量
    TENCENT_MAP_KEY。

注意
----
    1) 脚本会把结果缓存到 --cache（默认 .geocode_cache.json），重复跑不再花钱。
    2) 原始坐标文件只读不写，输出另存 --out。
    3) 这里的坐标是个人轨迹，属于隐私。别把原始 csv 传上 GitHub。
"""

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://apis.map.qq.com/ws/geocoder/v1/"

# ── WGS-84 → GCJ-02 ──────────────────────────────────────────────────
A = 6378245.0
EE = 0.00669342162296594323


def _out_of_china(lat, lon):
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _tf_lat(x, y):
    r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
         + 0.2 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tf_lon(x, y):
    r = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
         + 0.1 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def wgs2gcj(lat, lon):
    """GPS 原始坐标 → 腾讯地图坐标"""
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _tf_lat(lon - 105.0, lat - 35.0)
    dlon = _tf_lon(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - EE * math.sin(rad) ** 2
    sm = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sm) * math.pi)
    dlon = (dlon * 180.0) / (A / sm * math.cos(rad) * math.pi)
    return lat + dlat, lon + dlon


# ── 找列 ──────────────────────────────────────────────────────────────
LAT_NAMES = ("lat", "latitude", "纬度", "start_lat", "纬度(度)")
LON_NAMES = ("lon", "lng", "longitude", "经度", "start_lon", "start_lng", "经度(度)")


def find_col(head, names):
    low = [h.strip().lower() for h in head]
    for n in names:
        if n.lower() in low:
            return low.index(n.lower())
    return -1


# ── 请求 ──────────────────────────────────────────────────────────────
def query(lat, lon, key, retry=2):
    """返回 dict：province/city/district/adcode/level/note"""
    glat, glon = wgs2gcj(lat, lon)
    qs = urllib.parse.urlencode({
        "location": "%.6f,%.6f" % (glat, glon),
        "key": key,
        "get_poi": 0,
    })
    last = ""
    for i in range(retry + 1):
        try:
            req = urllib.request.Request(API + "?" + qs,
                                         headers={"User-Agent": "gps-city-wordcloud/1.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("status") != 0:
                last = "%s %s" % (data.get("status"), data.get("message", ""))
                if data.get("status") in (120, 121, 122):   # 限流 / 配额
                    time.sleep(1.5 * (i + 1))
                    continue
                return _empty("接口错误 " + last)
            info = (data.get("result") or {}).get("ad_info") or {}
            prov = (info.get("province") or "").strip()
            city = (info.get("city") or "").strip()
            dist = (info.get("district") or "").strip()
            adcode = str(info.get("adcode") or "").strip()
            if not city and prov:          # 直辖市 / 特别行政区
                city = prov
            if city and dist:
                lvl = "A"
            elif city:
                lvl = "B"
            elif prov:
                lvl = "C"
            else:
                lvl = "D"
            return {"province": prov, "city": city, "district": dist,
                    "adcode": adcode, "level": lvl, "note": ""}
        except urllib.error.URLError as e:
            last = str(e)
            time.sleep(1.2 * (i + 1))
        except Exception as e:              # noqa: BLE001
            last = str(e)
            time.sleep(0.8)
    return _empty("请求失败 " + last)


def _empty(note):
    return {"province": "", "city": "", "district": "", "adcode": "",
            "level": "D", "note": note}


# ── 主流程 ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="坐标 → 城市（腾讯地图逆地址解析）")
    ap.add_argument("--in", dest="src", required=True, help="输入 CSV")
    ap.add_argument("--out", dest="dst", required=True, help="输出 CSV")
    ap.add_argument("--key", default=os.environ.get("TENCENT_MAP_KEY", ""),
                    help="腾讯地图 key，默认读环境变量 TENCENT_MAP_KEY")
    ap.add_argument("--qps", type=float, default=5.0, help="每秒请求数，默认 5")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条，0=全部")
    ap.add_argument("--cache", default=".geocode_cache.json", help="缓存文件")
    args = ap.parse_args()

    if not args.key:
        sys.exit("缺 key：加 --key，或设环境变量 TENCENT_MAP_KEY")

    with open(args.src, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        sys.exit("输入文件没有数据行")

    head, body = rows[0], rows[1:]
    ilat, ilon = find_col(head, LAT_NAMES), find_col(head, LON_NAMES)
    if ilat < 0 or ilon < 0:
        sys.exit("找不到坐标列。表头需要含：%s / %s" % (LAT_NAMES[0], LON_NAMES[0]))

    if args.limit:
        body = body[:args.limit]

    cache = {}
    if os.path.exists(args.cache):
        try:
            cache = json.load(open(args.cache, encoding="utf-8"))
        except Exception:                    # noqa: BLE001
            cache = {}

    gap = 1.0 / max(args.qps, 0.1)
    stats = {"A": 0, "B": 0, "C": 0, "D": 0, "缓存命中": 0}
    out_rows = []

    for i, row in enumerate(body, 1):
        try:
            lat, lon = float(row[ilat]), float(row[ilon])
            valid = True
        except (ValueError, IndexError):
            valid = False

        if not valid:
            r = _empty("坐标为空或不是数字")
        else:
            ck = "%.6f,%.6f" % (lat, lon)
            if ck in cache:
                r = cache[ck]
                stats["缓存命中"] += 1
            else:
                r = query(lat, lon, args.key)
                cache[ck] = r
                time.sleep(gap)

        stats[r["level"]] = stats.get(r["level"], 0) + 1
        out_rows.append(list(row) + [r["province"], r["city"], r["district"],
                                     r["adcode"], r["level"], r["note"]])

        if i % 50 == 0 or i == len(body):
            print("  %d/%d   A%d B%d C%d D%d   缓存命中 %d"
                  % (i, len(body), stats["A"], stats["B"], stats["C"],
                     stats["D"], stats["缓存命中"]))

    with open(args.dst, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(head + ["province", "city", "district", "adcode", "level", "note"])
        w.writerows(out_rows)

    json.dump(cache, open(args.cache, "w", encoding="utf-8"), ensure_ascii=False)

    total = len(out_rows)
    judged = stats["A"] + stats["B"]
    print("\n完成：%d 条 → %s" % (total, args.dst))
    print("  可判定(A+B) %d 条，占 %.1f%%" % (judged, judged * 100.0 / max(total, 1)))
    print("  只到省(C) %d 条，判定不了(D) %d 条 —— 这两类别硬塞城市" % (stats["C"], stats["D"]))


if __name__ == "__main__":
    main()
