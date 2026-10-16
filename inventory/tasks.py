"""Celery 异步任务：密码轮换（每 12 小时）与实例数量日统计（每天 00:00）。"""

import logging
from datetime import date, timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import Cluster, Credential, DBInstance, InstanceDailyStat

logger = logging.getLogger("inventory.tasks")


@shared_task(name="inventory.rotate_instance_passwords")
def rotate_instance_passwords() -> dict:
    """轮换所有到期（默认 12 小时）的实例密码。

    ponytail: 直接改本地加密记录，未真正连库执行 ALTER USER；接真实数据库时在
    rotate() 返回明文之后补一次驱动调用即可。
    """
    due = [
        credential
        for credential in Credential.objects.select_related("instance").all()
        if credential.is_due
    ]
    for credential in due:
        credential.rotate()
    logger.info("密码轮换完成：%d 个实例", len(due))
    return {"rotated": len(due), "skipped": Credential.objects.count() - len(due)}


@shared_task(name="inventory.ensure_credentials")
def ensure_credentials() -> dict:
    """为缺少凭据的实例补建账号，避免新实例在下一个 12 小时窗口前无密码可用。"""
    missing = DBInstance.objects.filter(credential__isnull=True)
    for instance in missing:
        Credential.for_instance(instance)
    return {"created": missing.count()}


@shared_task(name="inventory.collect_daily_stats")
def collect_daily_stats(stat_date: str | None = None) -> dict:
    """按部门和集群维度统计实例数量并落库。

    参数 stat_date 为 ISO 日期字符串，默认统计"昨天"——任务在 00:00 触发，此刻
    当天的实例数还没稳定，统计刚结束的那一天才准确。同一 (日期,部门,集群) 重复
    执行走 upsert，重跑不会产生重复行。
    """
    target = date.fromisoformat(stat_date) if stat_date else timezone.localdate() - timedelta(days=1)

    rows = (
        Cluster.objects.values("id", "department_id")
        .annotate(total=Count("instances"))
        .order_by()
    )
    with transaction.atomic():
        for row in rows:
            InstanceDailyStat.objects.update_or_create(
                stat_date=target,
                department_id=row["department_id"],
                cluster_id=row["id"],
                defaults={"instance_count": row["total"]},
            )
    written = len(rows)
    logger.info("日统计完成：%s 共 %d 行", target, written)
    return {"stat_date": target.isoformat(), "rows": written}
