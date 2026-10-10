"""استقبال البصمات من أجهزة ZKTeco وما يتوافق معها عبر بروتوكول ADMS (Push/iclock).

الجهاز هو من يتصل بالنظام ويرسل الحركات؛ النظام لا يتصل بالجهاز. يُقبل فقط جهاز مسجل رقمه التسلسلي
في «أجهزة البصمة» ومفعّل، ومن عناوين IP المسموحة إن حُددت.
"""
import datetime
import logging

from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core.tenancy import tenant_context
from hr.models import AttendanceDevice, PunchSource

log = logging.getLogger(__name__)


def _text(body, status=200):
    return HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")


def _device(request):
    serial = (request.GET.get("SN") or "").strip()
    device = AttendanceDevice.all_objects.filter(serial_number=serial, enabled=True).select_related("company").first()
    if device is None:
        log.warning("جهاز بصمة غير مسجل أو موقوف: %s من %s", serial, _client_ip(request))
        return None
    ip = _client_ip(request)
    if not device.ip_allowed(ip):
        log.warning("جهاز البصمة %s من عنوان غير مسموح %s", serial, ip)
        return None
    AttendanceDevice.all_objects.filter(pk=device.pk).update(last_seen_at=timezone.now(), last_ip=ip[:64])
    return device


def parse_attlog(body):
    """سطور ATTLOG: الرقم\\tالوقت\\tالحالة\\tطريقة التحقق... ← [(الرقم، الوقت)]."""
    entries = []
    for line in body.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 2 or not parts[0].strip():
            continue
        try:
            moment = datetime.datetime.strptime(parts[1].strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        entries.append((parts[0].strip(), moment))
    return entries


@csrf_exempt
def cdata(request):
    device = _device(request)
    if device is None:
        return _text("UNKNOWN DEVICE", 401)
    if request.method == "GET":
        if request.GET.get("pushver") or request.GET.get("options"):
            fw = request.GET.get("pushver", "")
            if fw and fw != device.firmware:
                AttendanceDevice.all_objects.filter(pk=device.pk).update(firmware=fw[:100])
        # إعدادات الجهاز: إرسال الحركات فوراً وتشفير بلا (النقل عبر الشبكة)
        return _text("\n".join([
            f"GET OPTION FROM: {device.serial_number}",
            "ATTLOGStamp=None", "OPERLOGStamp=9999", "ATTPHOTOStamp=None",
            "ErrorDelay=30", "Delay=10", "TransTimes=00:00;14:05", "TransInterval=1",
            "TransFlag=TransData AttLog", "TimeZone=3", "Realtime=1", "Encrypt=None",
        ]))
    table = (request.GET.get("table") or "").upper()
    if table != "ATTLOG":
        return _text("OK")  # سجلات العمليات والصور والمستخدمين لا نحتاجها
    entries = parse_attlog(request.body.decode("utf-8", errors="ignore"))
    from hr.punches import record_punches

    with tenant_context(device.company):
        result = record_punches(entries, source=PunchSource.DEVICE, device=device)
    log.info("جهاز %s: %s بصمة جديدة", device.serial_number, result["stored"])
    return _text(f"OK: {len(entries)}")


@csrf_exempt
def getrequest(request):
    if _device(request) is None:
        return _text("UNKNOWN DEVICE", 401)
    return _text("OK")


@csrf_exempt
def devicecmd(request):
    if _device(request) is None:
        return _text("UNKNOWN DEVICE", 401)
    return _text("OK")
