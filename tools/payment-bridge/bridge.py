#!/usr/bin/env python3
"""برنامج وسيط بين شاشة نقطة البيع في سِينار وجهاز مدى (ECR) على جهاز الكاشير.

يعمل محلياً فقط (127.0.0.1) ولا يتصل بالإنترنت ولا بخادم سِينار:
  متصفح الكاشير  ──HTTP محلي──▶  هذا البرنامج  ──USB/تسلسلي/شبكة محلية──▶  جهاز مدى

الواجهة:
  GET  /status                         حالة البرنامج والجهاز
  POST /sale    {amount, currency, reference}
  POST /refund  {amount, currency, reference, original_rrn, original_auth_code}
  POST /void    {rrn, auth_code}
النتيجة: {status: approved|declined|voided|error, amount, auth_code, rrn, scheme, masked_pan, terminal_id, message}

لا يُعاد رقم البطاقة كاملاً أبداً (masked_pan فقط)، ولا يُخزن أي بيانات بطاقة.

التشغيل للتجربة:
  python3 bridge.py --driver simulator --allow-origin https://erp.example.com
"""
import argparse
import json
import random
import threading
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0"


class TerminalError(Exception):
    pass


class TerminalDriver:
    """واجهة مشغّل الجهاز. كل مزود (جيديا، نيوليب، سيرفباي…) يحتاج مشغّلاً حسب مواصفات ECR الخاصة به."""

    name = "base"
    terminal_id = ""

    def status(self):
        return {"connected": True}

    def sale(self, amount, reference):
        raise NotImplementedError

    def refund(self, amount, reference, original_rrn="", original_auth_code=""):
        raise NotImplementedError

    def void(self, rrn, auth_code=""):
        raise NotImplementedError


class SimulatorDriver(TerminalDriver):
    """جهاز وهمي للتدريب والاختبار: يوافق دائماً إلا المبالغ المنتهية بـ .51 فيرفضها."""

    name = "simulator"

    def __init__(self, terminal_id="SIM00001", delay=1.0):
        self.terminal_id = terminal_id
        self.delay = delay
        self.done = {}

    def _approve(self, amount):
        time.sleep(self.delay)
        if int(amount * 100) % 100 == 51:
            return {"status": "declined", "message": "رفض البنك المصدر (محاكاة)"}
        rrn = f"{int(time.time()) % 10**8:08d}{random.randint(1000, 9999)}"
        result = {"status": "approved", "amount": f"{amount:.2f}", "auth_code": f"{random.randint(0, 999999):06d}",
                  "rrn": rrn, "scheme": "mada", "masked_pan": f"588848******{random.randint(0, 9999):04d}",
                  "terminal_id": self.terminal_id}
        self.done[rrn] = result
        return result

    def sale(self, amount, reference):
        return self._approve(amount)

    def refund(self, amount, reference, original_rrn="", original_auth_code=""):
        return self._approve(amount)

    def void(self, rrn, auth_code=""):
        if self.done.pop(rrn, None) is None:
            return {"status": "error", "message": "العملية غير موجودة"}
        return {"status": "voided", "rrn": rrn}


class VendorECRDriver(TerminalDriver):
    """قالب لمشغّل جهاز حقيقي.

    أجهزة مدى في السعودية لا تشترك في بروتوكول ربط واحد: كل مزود يعطي التاجر مواصفات ECR
    (عبر USB/تسلسلي أو TCP على الشبكة المحلية) أو مكتبة SDK بعد توقيع اتفاقية الربط.
    لإضافة جهازك: انسخ هذا الصنف، ونفّذ sale/refund/void بحسب المواصفات، وأعِد القاموس بالحقول الموضحة أعلاه.
    """

    name = "vendor"

    def __init__(self, terminal_id="", **options):
        self.terminal_id = terminal_id
        self.options = options

    def status(self):
        return {"connected": False, "message": "لم يُضبط مشغّل الجهاز بعد — يحتاج مواصفات ECR من مزود الجهاز."}

    def sale(self, amount, reference):
        raise TerminalError("مشغّل الجهاز غير مُنفّذ. اطلب مواصفات ECR من مزود جهاز مدى.")

    refund = void = sale


DRIVERS = {"simulator": SimulatorDriver, "vendor": VendorECRDriver}


def parse_amount(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise TerminalError("مبلغ غير صالح")
    if amount <= 0 or amount > Decimal("1000000"):
        raise TerminalError("مبلغ غير صالح")
    return amount


def make_handler(driver, allowed_origins, lock):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"SynarPaymentBridge/{VERSION}"

        def log_message(self, fmt, *args):  # لا نطبع تفاصيل الطلبات
            pass

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            # الطلبات بلا Origin (مثل curl على نفس الجهاز) مسموحة للتشخيص؛ أما المواقع فيجب أن تكون مسجلة
            return origin is None or origin in allowed_origins

        def _send(self, code, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            origin = self.headers.get("Origin")
            if origin and origin in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            if not self._origin_ok():
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            # مطلوب في كروم ليسمح لصفحة على الإنترنت بالوصول إلى برنامج على جهاز المستخدم
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            self.end_headers()

        def do_GET(self):
            if not self._origin_ok():
                return self._send(403, {"status": "error", "message": "مصدر غير مسموح"})
            if self.path.rstrip("/") != "/status":
                return self._send(404, {"status": "error", "message": "غير موجود"})
            info = {"status": "ok", "version": VERSION, "driver": driver.name, "terminal_id": driver.terminal_id,
                    "busy": lock.locked()}
            info.update(driver.status())
            self._send(200, info)

        def do_POST(self):
            if not self._origin_ok():
                return self._send(403, {"status": "error", "message": "مصدر غير مسموح"})
            path = self.path.rstrip("/")
            if path not in ("/sale", "/refund", "/void"):
                return self._send(404, {"status": "error", "message": "غير موجود"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 10_000:
                    raise TerminalError("طلب كبير")
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TerminalError):
                return self._send(400, {"status": "error", "message": "بيانات غير صالحة"})
            # جهاز مدى ينفّذ عملية واحدة في كل مرة
            if not lock.acquire(blocking=False):
                return self._send(409, {"status": "error", "message": "جهاز الدفع مشغول بعملية أخرى"})
            try:
                if path == "/void":
                    result = driver.void(str(data.get("rrn") or ""), str(data.get("auth_code") or ""))
                else:
                    amount = parse_amount(data.get("amount"))
                    if str(data.get("currency") or "SAR").upper() != "SAR":
                        raise TerminalError("جهاز مدى يقبل الريال السعودي فقط")
                    reference = str(data.get("reference") or "")[:40]
                    if path == "/sale":
                        result = driver.sale(amount, reference)
                    else:
                        result = driver.refund(amount, reference, str(data.get("original_rrn") or ""),
                                               str(data.get("original_auth_code") or ""))
            except TerminalError as exc:
                return self._send(200, {"status": "error", "message": str(exc)})
            except Exception:  # noqa: BLE001 — لا نكشف تفاصيل داخلية للمتصفح
                return self._send(500, {"status": "error", "message": "خطأ في الاتصال بجهاز الدفع"})
            finally:
                lock.release()
            result.setdefault("terminal_id", driver.terminal_id)
            self._send(200, result)

    return Handler


def build_server(driver, allowed_origins, host="127.0.0.1", port=8099):
    return ThreadingHTTPServer((host, port), make_handler(driver, set(allowed_origins), threading.Lock()))


def main():
    parser = argparse.ArgumentParser(description="البرنامج الوسيط لجهاز مدى — سِينار")
    parser.add_argument("--driver", choices=sorted(DRIVERS), default="simulator")
    parser.add_argument("--allow-origin", action="append", required=True,
                        help="عنوان نظام سِينار المسموح له، مثل https://erp.example.com (يمكن تكراره)")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--terminal-id", default="")
    args = parser.parse_args()
    driver_cls = DRIVERS[args.driver]
    driver = driver_cls(terminal_id=args.terminal_id) if args.terminal_id else driver_cls()
    server = build_server(driver, [o.rstrip("/") for o in args.allow_origin], port=args.port)
    print(f"البرنامج الوسيط يعمل على http://127.0.0.1:{args.port} — المشغّل: {driver.name}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
