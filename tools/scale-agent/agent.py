#!/usr/bin/env python3
"""برنامج مزامنة الموازين لسِينار — يعمل على جهاز في الفرع في نفس شبكة الموازين.

يسأل النظام عن أسعار الأصناف الموزونة كل بضع دقائق، وإذا تغيّر شيء يرسل التغييرات للموازين ثم يبلّغ النظام بالنتيجة.
الاتصال باتجاه واحد: من الفرع إلى النظام (HTTPS بمفتاح API)، ومن الفرع إلى الموازين في الشبكة المحلية.
خادم النظام لا يتصل بالفرع ولا بالموازين.

  python agent.py --server https://erp.example.com --key snr_xxx --interval 5 --out-dir C:\\ScalePLU

لا يحتاج أي مكتبات خارجية (Python 3.8+).
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime

VERSION = "1.0"
COLUMNS = ["PLU", "ItemCode", "Name", "ScaleName", "UnitPrice", "Unit", "ShelfLifeDays", "Barcode", "Action"]


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def row_hash(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class DriverError(Exception):
    pass


class FileDriver:
    """يكتب ملف الأصناف والأسعار لبرنامج الميزان ليستورده (أغلب برامج الموازين تستورد CSV/Excel).

    ‎plu-all.csv‎ : القائمة كاملة (يُستبدل في كل مرة).
    ‎plu-changes-*.csv‎ : ما تغيّر فقط مع عمود Action: ‎U‎ تحديث/إضافة، ‎D‎ حذف.
    """

    def __init__(self, out_dir, encoding="utf-8-sig", delimiter=","):
        self.out_dir, self.encoding, self.delimiter = out_dir, encoding, delimiter

    def _write(self, path, rows):
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="", encoding=self.encoding, errors="replace") as f:
                writer = csv.writer(f, delimiter=self.delimiter)
                writer.writerow(COLUMNS)
                for row, action in rows:
                    writer.writerow([row["plu"], row["code"], row["name"], row["scale_name"], row["price"], row["unit"],
                                     row.get("shelf_life_days") or "", row.get("barcode") or "", action])
            os.replace(tmp, path)  # استبدال ذري: برنامج الميزان لا يقرأ ملفاً نصف مكتوب
        except OSError:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def push(self, scale, changed, removed, full):
        if not self.out_dir:
            raise DriverError("حدّد مجلد الملفات (--out-dir) لإرسال الأسعار بملف.")
        folder = os.path.join(self.out_dir, re.sub(r'[\\/:*?"<>|]+', "_", scale["name"]).strip() or str(scale["id"]))
        try:
            self._write(os.path.join(folder, "plu-all.csv"), [(r, "U") for r in full])
            if changed or removed:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                self._write(os.path.join(folder, f"plu-changes-{stamp}.csv"),
                            [(r, "U") for r in changed] + [(r, "D") for r in removed])
        except OSError as exc:
            raise DriverError(f"تعذرت الكتابة في المجلد {folder}: {exc}")
        return f"حُدّث ملف الأسعار ({len(changed)} تغيير، {len(removed)} حذف، المجموع {len(full)})"


class CustomDriver:
    """قالب للإرسال المباشر عبر الشبكة لطراز معيّن.

    كل شركة (CAS, DIGI, Mettler Toledo, Bizerba, Aclas, Rongta, Dahua…) لها بروتوكولها أو مكتبتها (SDK).
    بعد الحصول على مواصفات طرازك نفّذ push() هنا: افتح اتصالاً بـ scale["ip"] و scale["port"]،
    أرسل لكل صنف في changed: رقم PLU والاسم وسعر الكيلو ومدة الصلاحية، واحذف ما في removed.
    """

    def push(self, scale, changed, removed, full):
        raise DriverError("الإرسال المباشر لهذا الطراز غير مُنفّذ بعد؛ يحتاج بروتوكول الشركة المصنعة. "
                          "استخدم «ملف استيراد» مؤقتاً.")


class Agent:
    def __init__(self, server, key, state_path, drivers, http=None, timeout=30):
        self.server = server.rstrip("/")
        self.key = key
        self.state_path = state_path
        self.drivers = drivers
        self.http = http or self._http
        self.timeout = timeout
        self.state = self._load()

    # ---------- الحالة المحلية: آخر ما أُرسل لكل ميزان ----------
    def _load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"etag": "", "scales": {}}

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ---------- الاتصال بالنظام ----------
    def _http(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.server + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.key}", "Accept": "application/json",
            "User-Agent": f"SynarScaleAgent/{VERSION}", **({"Content-Type": "application/json"} if data else {}),
            **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def report(self, scale_id, ok, message, version="", plu_count=0):
        try:
            self.http("POST", f"/api/v1/scales/{scale_id}/report/",
                      {"ok": ok, "message": message, "version": version, "plu_count": plu_count})
        except OSError as exc:
            log(f"تعذر إبلاغ النظام بنتيجة الميزان {scale_id}: {exc}")

    # ---------- دورة مزامنة واحدة ----------
    def sync_once(self):
        headers = {"If-None-Match": self.state["etag"]} if self.state.get("etag") else {}
        status, resp_headers, body = self.http("GET", "/api/v1/scales/", headers=headers)
        if status == 304:
            log("لا تغيير في الأسعار.")
            return []
        if status != 200:
            try:
                message = json.loads(body)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = body[:200].decode("utf-8", "replace") if body else ""
            raise RuntimeError(f"رد النظام {status}: {message}")
        payload = json.loads(body)
        results, all_ok = [], True
        seen = set()
        for scale in payload.get("scales", []):
            sid = str(scale["id"])
            seen.add(sid)
            previous = self.state["scales"].get(sid, {})
            if previous.get("version") == scale["version"] and previous.get("ok"):
                continue
            sent = previous.get("plus", {}) if previous.get("ok") else {}
            current = {str(r["plu"]): r for r in scale["plus"]}
            changed = [r for plu, r in current.items() if sent.get(plu) != row_hash(r)]
            removed = [{"plu": int(plu), "code": "", "name": "", "scale_name": "", "price": "0", "unit": "kg"}
                       for plu in sent if plu not in current]
            driver = self.drivers.get(scale.get("driver")) or self.drivers["custom"]
            try:
                message = driver.push(scale, changed, removed, scale["plus"])
                ok = True
            except Exception as exc:  # noqa: BLE001 — أي خطأ يُبلّغ ولا يوقف بقية الموازين
                message, ok = str(exc) or exc.__class__.__name__, False
            log(f"{scale['name']}: {'✓' if ok else '✗'} {message}")
            self.report(scale["id"], ok, message, scale["version"] if ok else "", len(scale["plus"]))
            if ok:
                self.state["scales"][sid] = {"version": scale["version"], "ok": True,
                                             "plus": {plu: row_hash(r) for plu, r in current.items()}}
            else:
                all_ok = False
                self.state["scales"].setdefault(sid, {})["ok"] = False
            results.append((scale["name"], ok, message))
        for sid in list(self.state["scales"]):
            if sid not in seen:
                del self.state["scales"][sid]
        # لا نحفظ النسخة العامة إلا إن نجح الكل، حتى يُعاد المحاولة للميزان الذي فشل
        etag = resp_headers.get("ETag") or resp_headers.get("Etag") or ""
        self.state["etag"] = etag if all_ok else ""
        self._save()
        return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="مزامنة أسعار الموازين مع سِينار")
    parser.add_argument("--server", required=True, help="عنوان النظام، مثل https://erp.example.com")
    parser.add_argument("--key", default=os.environ.get("SYNAR_API_KEY", ""), help="مفتاح API (أو المتغير SYNAR_API_KEY)")
    parser.add_argument("--interval", type=float, default=5, help="الفحص كل كم دقيقة (افتراضي 5)")
    parser.add_argument("--out-dir", default="", help="مجلد ملفات الأسعار لطريقة «ملف استيراد»")
    parser.add_argument("--encoding", default="utf-8-sig", help="ترميز الملف (utf-8-sig أو cp1256 للبرامج القديمة)")
    parser.add_argument("--delimiter", default=",", help="فاصل الأعمدة في الملف")
    parser.add_argument("--state", default="scale-agent-state.json", help="ملف حفظ آخر ما أُرسل")
    parser.add_argument("--once", action="store_true", help="مزامنة مرة واحدة ثم الخروج (لجدولة ويندوز أو cron)")
    args = parser.parse_args(argv)
    if not args.key:
        parser.error("مفتاح API مطلوب (--key أو SYNAR_API_KEY).")
    if not args.server.startswith("https://") and "localhost" not in args.server and "127.0.0.1" not in args.server:
        log("تحذير: الاتصال بالنظام بدون HTTPS يكشف مفتاح API في الشبكة.")
    agent = Agent(args.server, args.key, args.state, {
        "file": FileDriver(args.out_dir, args.encoding, args.delimiter), "custom": CustomDriver()})
    log(f"برنامج مزامنة الموازين {VERSION} — كل {args.interval:g} دقيقة")
    while True:
        try:
            agent.sync_once()
        except Exception as exc:  # noqa: BLE001 — انقطاع الإنترنت مثلاً: نعيد المحاولة في الدورة القادمة
            log(f"تعذرت المزامنة: {exc}")
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(args.interval, 0.5) * 60)


if __name__ == "__main__":
    sys.exit(main())
