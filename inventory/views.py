from django.db.models import Count
from django.db.models.deletion import ProtectedError
from rest_framework import status, viewsets
from rest_framework.exceptions import APIException
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Cluster, Credential, DBInstance, Department, InstanceDailyStat
from .probe import check_tcp
from .serializers import (
    ClusterSerializer,
    CredentialSerializer,
    DBInstanceSerializer,
    DepartmentSerializer,
    InstanceDailyStatSerializer,
)


class ProtectedDeleteMixin:
    """被下级资源引用时返回 409 而不是 500。

    Department/Cluster 对下级用 PROTECT 是刻意的（删部门不该悄悄带走集群和实例），
    但 ProtectedError 默认冒泡成 500，调用方拿不到"为什么删不掉"。
    """

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError as exc:
            raise Conflict(str(exc)) from exc


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "protected_relation"


# annotate 带 GROUP BY 时 Django 会丢弃 Meta.ordering，不显式排序分页结果不稳定。
class DepartmentViewSet(ProtectedDeleteMixin, viewsets.ModelViewSet):
    queryset = Department.objects.annotate(cluster_count=Count("clusters", distinct=True)).order_by(
        "name"
    )
    serializer_class = DepartmentSerializer


class ClusterViewSet(ProtectedDeleteMixin, viewsets.ModelViewSet):
    queryset = (
        Cluster.objects.select_related("department")
        .annotate(instance_count=Count("instances", distinct=True))
        .order_by("department__name", "name")
    )
    serializer_class = ClusterSerializer


class DBInstanceViewSet(viewsets.ModelViewSet):
    queryset = DBInstance.objects.select_related("cluster__department")
    serializer_class = DBInstanceSerializer

    def _probe(self, instance: DBInstance) -> Response:
        result = check_tcp(instance.host, instance.port)
        result["instance"] = instance.name
        # 探测结果是运维信号，可达性直接影响是否还能把它当可用实例，故用 200/503 区分。
        status_code = 200 if result["reachable"] else 503
        return Response(result, status=status_code)

    @action(detail=True, methods=["get", "post"], url_path="probe")
    def probe(self, request, pk=None):
        """探测该实例 TCP 端口是否可达：GET/POST /api/instances/{id}/probe/"""
        return self._probe(self.get_object())

    @action(detail=True, methods=["post"], url_path="rotate-password")
    def rotate_password(self, request, pk=None):
        """立即轮换该实例密码。响应里的明文只出现这一次，之后只能解密密文取回。"""
        instance = self.get_object()
        credential = Credential.for_instance(instance)
        plaintext = credential.rotate()
        return Response(
            {
                "instance": instance.name,
                "username": credential.username,
                "password": plaintext,
                "rotated_at": credential.rotated_at,
            }
        )


class CredentialViewSet(viewsets.ModelViewSet):
    queryset = Credential.objects.select_related("instance").order_by("instance__name")
    serializer_class = CredentialSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["reveal"] = self.request.query_params.get("reveal") in ("1", "true", "yes")
        return context


class InstanceDailyStatViewSet(viewsets.ReadOnlyModelViewSet):
    """日统计是 Celery 任务写入的结果，接口只读，便于直接核对统计结果。"""

    serializer_class = InstanceDailyStatSerializer

    def get_queryset(self):
        queryset = InstanceDailyStat.objects.select_related("department", "cluster")
        stat_date = self.request.query_params.get("date")
        if stat_date:
            queryset = queryset.filter(stat_date=stat_date)
        return queryset
