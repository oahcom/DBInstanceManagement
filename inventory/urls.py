from rest_framework.routers import DefaultRouter

from .views import (
    ClusterViewSet,
    CredentialViewSet,
    DBInstanceViewSet,
    DepartmentViewSet,
    InstanceDailyStatViewSet,
)

router = DefaultRouter()
router.register("departments", DepartmentViewSet)
router.register("clusters", ClusterViewSet)
router.register("instances", DBInstanceViewSet)
router.register("credentials", CredentialViewSet)
router.register("daily-stats", InstanceDailyStatViewSet, basename="daily-stat")

urlpatterns = router.urls
