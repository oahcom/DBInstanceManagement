from django.contrib import admin

from .models import Cluster, Credential, DBInstance, Department, InstanceDailyStat


@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin):
    """密码明文不出现在后台：加密字段只读，需要查看明文走 API 的 ?reveal=1。"""

    list_display = ("instance", "username", "rotated_at")
    readonly_fields = ("password_encrypted", "rotated_at")


admin.site.register(Department)
admin.site.register(Cluster)
admin.site.register(DBInstance)
admin.site.register(InstanceDailyStat)
