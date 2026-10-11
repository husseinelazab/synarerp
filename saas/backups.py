"""حالة النسخ الاحتياطي كما يكتبها deploy/backup.sh في مجلد النسخ (قراءة فقط، دون فتح النسخ نفسها)."""
import datetime
import json
import os

from django.conf import settings
from django.utils import timezone

STALE_HOURS = 26


def backup_status():
    folder = settings.SYNAR_BACKUP_DIR
    if not os.path.isdir(folder):
        return {"available": False}
    files = []
    for name in os.listdir(folder):
        if name.startswith("synar-") and (name.endswith(".sql.gz") or name.endswith(".sql.gz.gpg")):
            st = os.stat(os.path.join(folder, name))
            files.append({"name": name, "size": st.st_size, "encrypted": name.endswith(".gpg"),
                          "time": datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc)})
    files.sort(key=lambda f: f["time"], reverse=True)
    last = None
    try:
        with open(os.path.join(folder, "status.json"), encoding="utf-8") as fh:
            last = json.load(fh)
        last["time"] = datetime.datetime.fromisoformat(last["time"])
    except (OSError, ValueError, KeyError, TypeError):
        last = None
    latest = files[0] if files else None
    problems = []
    if last is not None and not last.get("ok"):
        problems.append(f"فشلت آخر محاولة نسخ: {last.get('message', '')}")
    if latest is None:
        problems.append("لا توجد أي نسخة احتياطية بعد.")
    elif timezone.now() - latest["time"] > datetime.timedelta(hours=STALE_HOURS):
        problems.append(f"آخر نسخة ناجحة أقدم من {STALE_HOURS} ساعة.")
    if latest is not None and not latest["encrypted"]:
        problems.append("آخر نسخة غير مشفّرة: اضبط SYNAR_BACKUP_PASSPHRASE في ملف .env.")
    return {"available": True, "latest": latest, "last_attempt": last, "count": len(files),
            "total_size": sum(f["size"] for f in files), "problems": problems}
