"""覆盖题目 5 个考点的端到端测试：模型/CRUD/探测/密码轮换/日统计/计时中间件。

跑法：.venv/bin/python manage.py test
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Cluster, Credential, DBInstance, Department, InstanceDailyStat
from .probe import check_tcp
from .tasks import collect_daily_stats, rotate_instance_passwords


class APITestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user("ops", password="ops-secret-2026")
        self.client.force_authenticate(self.user)
        self.department = Department.objects.create(name="基础架构部", code="INFRA")
        self.cluster = Cluster.objects.create(
            name="订单库主集群", engine=Cluster.Engine.MYSQL, department=self.department
        )


class AuthTests(APITestCase):
    def test_anonymous_cannot_list_instances(self):
        self.client.force_authenticate(None)
        response = self.client.get("/api/instances/")
        self.assertIn(response.status_code, (401, 403))

    def test_anonymous_cannot_reveal_passwords(self):
        Credential.for_instance(
            DBInstance.objects.create(
                name="order-db-auth", cluster=self.cluster, host="10.0.0.9", port=3306
            )
        )
        self.client.force_authenticate(None)
        response = self.client.get("/api/credentials/?reveal=1")
        self.assertIn(response.status_code, (401, 403))


class DepartmentCRUDTests(APITestCase):
    def test_create_department_returns_201_and_persists(self):
        response = self.client.post(
            "/api/departments/", {"name": "数据平台部", "code": "DATA"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(Department.objects.filter(code="DATA").exists())

    def test_duplicate_department_code_returns_400(self):
        response = self.client.post(
            "/api/departments/", {"name": "另一个名字", "code": "INFRA"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("code", response.json())

    def test_update_department_name(self):
        response = self.client.patch(
            f"/api/departments/{self.department.id}/", {"name": "基础架构一组"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.department.refresh_from_db()
        self.assertEqual(self.department.name, "基础架构一组")

    def test_delete_empty_department_returns_204(self):
        empty = Department.objects.create(name="临时部门", code="TMP")
        response = self.client.delete(f"/api/departments/{empty.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Department.objects.filter(id=empty.id).exists())


class ClusterCRUDTests(APITestCase):
    def test_create_cluster_with_department(self):
        response = self.client.post(
            "/api/clusters/",
            {"name": "用户库集群", "engine": "postgresql", "department": self.department.id},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["department_name"], "基础架构部")

    def test_delete_department_with_clusters_returns_409(self):
        response = self.client.delete(f"/api/departments/{self.department.id}/")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Department.objects.filter(id=self.department.id).exists())

    def test_duplicate_cluster_name_in_department_returns_400(self):
        response = self.client.post(
            "/api/clusters/",
            {"name": "订单库主集群", "engine": "mysql", "department": self.department.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class InstanceProbeTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.instance = DBInstance.objects.create(
            name="order-db-01", cluster=self.cluster, host="127.0.0.1", port=3306
        )

    def test_probe_unreachable_port_returns_503(self):
        response = self.client.get(f"/api/instances/{self.instance.id}/probe/")
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body["reachable"])
        self.assertEqual(body["port"], 3306)
        self.assertTrue(body["error"])

    def test_check_tcp_reports_listening_port_reachable(self):
        import socket

        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            result = check_tcp("127.0.0.1", port, timeout=1)
        self.assertTrue(result["reachable"])
        self.assertEqual(result["error"], "")
        self.assertGreaterEqual(result["latency_ms"], 0)


class CredentialRotationTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.instance = DBInstance.objects.create(
            name="order-db-02", cluster=self.cluster, host="10.0.0.1", port=3306
        )

    def test_new_instance_gets_credential_automatically(self):
        credential = Credential.objects.get(instance=self.instance)
        self.assertIsNotNone(credential.rotated_at)
        self.assertGreaterEqual(len(credential.get_password()), 24)

    def test_password_stored_encrypted_and_decryptable(self):
        credential = Credential.for_instance(self.instance)
        plaintext = credential.get_password()
        self.assertNotIn(plaintext, credential.password_encrypted)
        self.assertTrue(credential.password_encrypted.startswith("gAAAAA"))
        self.assertGreaterEqual(len(plaintext), 24)

    def test_rotate_changes_plaintext_and_refreshes_timestamp(self):
        credential = Credential.for_instance(self.instance)
        credential.rotated_at = timezone.now() - timedelta(hours=13)
        credential.save(update_fields=["rotated_at"])
        old = credential.get_password()

        rotated = rotate_instance_passwords()
        credential.refresh_from_db()

        self.assertEqual(rotated["rotated"], 1)
        self.assertNotEqual(old, credential.get_password())
        self.assertLess(timezone.now() - credential.rotated_at, timedelta(minutes=1))

    def test_recently_rotated_instance_is_skipped(self):
        credential = Credential.for_instance(self.instance)
        before = credential.password_encrypted
        result = rotate_instance_passwords()
        credential.refresh_from_db()
        self.assertEqual(result["rotated"], 0)
        self.assertEqual(credential.password_encrypted, before)

    def test_credential_api_masks_password_unless_revealed(self):
        Credential.for_instance(self.instance)
        masked = self.client.get("/api/credentials/").json()["results"][0]["password"]
        self.assertEqual(masked, "********")

        revealed = self.client.get("/api/credentials/?reveal=1").json()["results"][0]["password"]
        self.assertNotEqual(revealed, "********")
        self.assertGreaterEqual(len(revealed), 24)

    def test_credential_cannot_be_created_via_api(self):
        """密码只能由服务端生成，否则可以 POST 出空密码凭据。"""
        response = self.client.post(
            "/api/credentials/", {"instance": self.instance.id, "username": "hacked"}, format="json"
        )
        self.assertEqual(response.status_code, 405)
        self.assertFalse(Credential.objects.filter(username="hacked").exists())

    def test_credential_cannot_be_deleted_or_edited(self):
        Credential.for_instance(self.instance)
        credential = Credential.objects.get(instance=self.instance)
        self.assertEqual(self.client.delete(f"/api/credentials/{credential.id}/").status_code, 405)
        self.assertEqual(
            self.client.patch(
                f"/api/credentials/{credential.id}/", {"username": "x"}, format="json"
            ).status_code,
            405,
        )

    def test_rotate_password_endpoint_returns_one_time_plaintext(self):
        response = self.client.post(f"/api/instances/{self.instance.id}/rotate-password/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["instance"], "order-db-02")
        credential = Credential.objects.get(instance=self.instance)
        self.assertEqual(credential.get_password(), response.json()["password"])


class DailyStatTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.other_cluster = Cluster.objects.create(
            name="用户库集群", engine=Cluster.Engine.POSTGRESQL, department=self.department
        )
        for index in range(3):
            DBInstance.objects.create(
                name=f"order-db-{index}", cluster=self.cluster, host="10.0.0.1", port=3306
            )
        DBInstance.objects.create(
            name="user-db-1", cluster=self.other_cluster, host="10.0.0.2", port=5432
        )

    def test_count_instances_by_department_and_cluster(self):
        result = collect_daily_stats(stat_date="2026-10-15")
        self.assertEqual(result["rows"], 2)

        stats = {
            row.cluster.name: row.instance_count
            for row in InstanceDailyStat.objects.filter(stat_date="2026-10-15")
        }
        self.assertEqual(stats, {"订单库主集群": 3, "用户库集群": 1})

    def test_rerun_same_date_does_not_duplicate_rows(self):
        collect_daily_stats(stat_date="2026-10-15")
        collect_daily_stats(stat_date="2026-10-15")
        self.assertEqual(InstanceDailyStat.objects.filter(stat_date="2026-10-15").count(), 2)

    def test_default_date_is_yesterday(self):
        result = collect_daily_stats()
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertEqual(result["stat_date"], yesterday)

    def test_stat_api_filters_by_date(self):
        collect_daily_stats(stat_date="2026-10-15")
        collect_daily_stats(stat_date="2026-10-14")

        response = self.client.get("/api/daily-stats/?date=2026-10-15")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["results"][0]["department_name"], "基础架构部")


class RequestTimingMiddlewareTests(APITestCase):
    def test_response_header_contains_numeric_duration(self):
        response = self.client.get("/api/departments/")
        self.assertIn("X-Request-Duration-Ms", response)
        self.assertGreaterEqual(float(response["X-Request-Duration-Ms"]), 0)

    def test_duration_stays_within_plausible_range(self):
        response = self.client.get("/api/instances/")
        self.assertLessEqual(float(response["X-Request-Duration-Ms"]), 1000)
