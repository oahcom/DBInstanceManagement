from rest_framework import serializers

from .models import Cluster, Credential, DBInstance, Department, InstanceDailyStat


class DepartmentSerializer(serializers.ModelSerializer):
    # 由 queryset 的 annotate(cluster_count=...) 提供，读注解字段而不是 related manager，
    # 否则列表页每行一次 COUNT 查询。
    cluster_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Department
        fields = ("id", "name", "code", "cluster_count", "created_at", "updated_at")


class ClusterSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    instance_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Cluster
        fields = (
            "id",
            "name",
            "engine",
            "department",
            "department_name",
            "instance_count",
            "created_at",
            "updated_at",
        )


class DBInstanceSerializer(serializers.ModelSerializer):
    cluster_name = serializers.CharField(source="cluster.name", read_only=True)
    department_name = serializers.CharField(source="cluster.department.name", read_only=True)

    class Meta:
        model = DBInstance
        fields = (
            "id",
            "name",
            "cluster",
            "cluster_name",
            "department_name",
            "host",
            "port",
            "role",
            "is_active",
            "created_at",
            "updated_at",
        )


class CredentialSerializer(serializers.ModelSerializer):
    instance_name = serializers.CharField(source="instance.name", read_only=True)
    password = serializers.SerializerMethodField()

    class Meta:
        model = Credential
        fields = (
            "id",
            "instance",
            "instance_name",
            "username",
            "password",
            "rotated_at",
            "updated_at",
        )
        read_only_fields = ("rotated_at", "password")

    def get_password(self, obj: Credential) -> str:
        """默认返回掩码；带 ?reveal=1 才回明文，避免密码随列表接口被批量拖走。"""
        if not self.context.get("reveal"):
            return "********"
        return obj.get_password()


class InstanceDailyStatSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    cluster_name = serializers.CharField(source="cluster.name", read_only=True)

    class Meta:
        model = InstanceDailyStat
        fields = (
            "id",
            "stat_date",
            "department",
            "department_name",
            "cluster",
            "cluster_name",
            "instance_count",
            "created_at",
        )
