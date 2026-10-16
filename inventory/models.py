import secrets
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Department(TimeStampedModel):
    name = models.CharField("部门名称", max_length=64, unique=True)
    code = models.CharField("部门编码", max_length=32, unique=True)

    class Meta:
        verbose_name = verbose_name_plural = "部门"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class Cluster(TimeStampedModel):
    class Engine(models.TextChoices):
        MYSQL = "mysql", "MySQL"
        POSTGRESQL = "postgresql", "PostgreSQL"
        REDIS = "redis", "Redis"
        MONGODB = "mongodb", "MongoDB"
        OTHER = "other", "其他"

    name = models.CharField("集群名称", max_length=64)
    engine = models.CharField(
        "数据库类型", max_length=16, choices=Engine.choices, default=Engine.MYSQL
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="clusters",
        verbose_name="所属部门",
    )

    class Meta:
        verbose_name = verbose_name_plural = "集群"
        ordering = ("department__name", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("department", "name"), name="uniq_cluster_name_per_department"
            )
        ]

    def __str__(self) -> str:
        return f"{self.department.name}/{self.name}"


class DBInstance(TimeStampedModel):
    class Role(models.TextChoices):
        PRIMARY = "primary", "主库"
        REPLICA = "replica", "从库"

    name = models.CharField("实例名称", max_length=64, unique=True)
    cluster = models.ForeignKey(
        Cluster, on_delete=models.PROTECT, related_name="instances", verbose_name="所属集群"
    )
    host = models.GenericIPAddressField("主机地址")
    port = models.PositiveIntegerField(
        "端口",
        default=3306,
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
    )
    role = models.CharField("角色", max_length=16, choices=Role.choices, default=Role.PRIMARY)
    is_active = models.BooleanField("是否启用", default=True)

    class Meta:
        verbose_name = verbose_name_plural = "数据库实例"
        ordering = ("name",)

    def __str__(self) -> str:
        return f"{self.name} ({self.host}:{self.port})"


class Credential(TimeStampedModel):
    """实例账号。明文密码只在轮换瞬间存在于内存，落库的只有 Fernet 密文。"""

    PASSWORD_LENGTH = 24

    instance = models.OneToOneField(
        DBInstance,
        on_delete=models.CASCADE,
        related_name="credential",
        verbose_name="实例",
    )
    username = models.CharField("账号", max_length=64, default="dbadmin")
    password_encrypted = models.TextField("加密密码", blank=True)
    rotated_at = models.DateTimeField("上次轮换时间", null=True, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = "实例凭据"

    def __str__(self) -> str:
        return f"{self.instance.name}:{self.username}"

    @classmethod
    def for_instance(cls, instance: DBInstance) -> "Credential":
        """取实例凭据；没有就立即生成一个——系统不允许存在无密码的实例。"""
        credential, created = cls.objects.get_or_create(instance=instance)
        if created:
            credential.rotate()
        return credential

    @staticmethod
    def _fernet() -> Fernet:
        return Fernet(settings.DBI_MASTER_KEY.encode())

    def set_password(self, plaintext: str) -> None:
        self.password_encrypted = self._fernet().encrypt(plaintext.encode()).decode()

    def get_password(self) -> str:
        """解密返回明文。密钥不匹配或密文损坏时抛 ValueError，不静默降级为空串。"""
        try:
            return self._fernet().decrypt(self.password_encrypted.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("凭据解密失败：主密钥不匹配或密文已损坏") from exc

    def rotate(self) -> str:
        """生成新密码、加密落库，返回明文供调用方（例如真实下发到数据库）使用。"""
        plaintext = secrets.token_urlsafe(self.PASSWORD_LENGTH)
        self.set_password(plaintext)
        self.rotated_at = timezone.now()
        self.save(update_fields=["password_encrypted", "rotated_at", "updated_at"])
        return plaintext

    @property
    def is_due(self) -> bool:
        interval = timedelta(hours=settings.CREDENTIAL_ROTATE_INTERVAL_HOURS)
        return self.rotated_at is None or timezone.now() - self.rotated_at >= interval


@receiver(post_save, sender=DBInstance)
def create_credential_for_new_instance(sender, instance: DBInstance, created: bool, **kwargs) -> None:
    """实例一旦建立就立即拥有账密。

    放在信号里而不是视图里：admin、shell、数据迁移创建的实例同样受约束，
    否则会出现"绕过 API 建的实例永远没有密码"的空洞。
    """
    if created:
        Credential.for_instance(instance)


class InstanceDailyStat(models.Model):
    """按 (日期, 部门, 集群) 维度落地实例数量快照。"""

    stat_date = models.DateField("统计日期")
    department = models.ForeignKey(
        Department, on_delete=models.CASCADE, related_name="daily_stats", verbose_name="部门"
    )
    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, related_name="daily_stats", verbose_name="集群"
    )
    instance_count = models.PositiveIntegerField("实例数量", default=0)
    created_at = models.DateTimeField("写入时间", auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = "实例数量日统计"
        ordering = ("-stat_date", "department__name", "cluster__name")
        constraints = [
            models.UniqueConstraint(
                fields=("stat_date", "department", "cluster"), name="uniq_daily_stat_scope"
            )
        ]

    def __str__(self) -> str:
        return f"{self.stat_date} {self.department.name}/{self.cluster.name}={self.instance_count}"
