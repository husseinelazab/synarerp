"""طابور المهام الخلفية: يُضاف من الطلب، وينفذه المجدول (run_scheduler) كل بضع ثوانٍ.

يعمل عند ضبط SYNAR_BACKGROUND_JOBS=1 (مفعّل في docker-compose)؛ وإلا تُنفذ المهام فوراً داخل الطلب.
"""
import logging
import os

from django.db import transaction
from django.utils import timezone

log = logging.getLogger(__name__)

TASKS = {}  # الاسم ← الدالة (لا يُنفذ إلا ما سُجل هنا)


def task(name):
    def register(func):
        TASKS[name] = func
        return func
    return register


def background_enabled():
    return os.environ.get("SYNAR_BACKGROUND_JOBS", "0") == "1"


def enqueue(name, company=None, user=None, **kwargs):
    """يضيف المهمة للطابور، أو ينفذها فوراً إن لم تُفعّل المهام الخلفية. يعيد سجل المهمة."""
    from core.models import BackgroundJob

    _load_tasks()
    if name not in TASKS:
        raise ValueError(f"مهمة غير معروفة: {name}")
    job = BackgroundJob.objects.create(company=company, user=user, task=name, kwargs=kwargs)
    if not background_enabled():
        execute(job)
    return job


def execute(job):
    from core.models import JobStatus
    from core.tenancy import set_current_user, tenant_context

    _load_tasks()
    job.status, job.started_at, job.attempts = JobStatus.RUNNING, timezone.now(), job.attempts + 1
    job.save(update_fields=["status", "started_at", "attempts"])
    token = set_current_user(job.user)
    try:
        with tenant_context(job.company):
            TASKS[job.task](job=job, **job.kwargs)
    except Exception as exc:  # المهمة الفاشلة تُسجل ولا توقف العامل
        log.exception("فشلت المهمة الخلفية %s", job)
        job.status, job.error = JobStatus.FAILED, str(exc)[:2000]
    else:
        job.status = JobStatus.DONE
    finally:
        from core.tenancy import _current_user

        _current_user.reset(token)
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "error", "finished_at"])
    return job


def run_pending(limit=20):
    """ينفذ المهام المنتظرة بالترتيب؛ يحجز كل مهمة قبل تنفيذها حتى لا ينفذها عاملان."""
    from core.models import BackgroundJob, JobStatus

    done = 0
    for _ in range(limit):
        with transaction.atomic():
            job = (BackgroundJob.objects.select_for_update(skip_locked=True)
                   .filter(status=JobStatus.QUEUED).order_by("id").first())
            if job is None:
                break
            job.status = JobStatus.RUNNING
            job.save(update_fields=["status"])
        execute(job)
        done += 1
    return done


def _load_tasks():
    from core import import_views  # noqa: F401  (تسجيل مهمة الاستيراد)
