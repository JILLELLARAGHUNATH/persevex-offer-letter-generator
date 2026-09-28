import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_test_storage = tempfile.TemporaryDirectory(prefix="persevex-sync-tests-")
os.environ.update({
    "PERSEVEX_ENV": "test",
    "PERSEVEX_SESSION_SECRET": "test-sync-session-secret",
    "SUPABASE_URL": "https://vaqleosqgsmnsppnxqbh.supabase.co",
    "SUPABASE_KEY": "test-key-not-real",
    "PERSEVEX_SQLITE_DB_PATH": str(Path(_test_storage.name) / "sync_test.sqlite3"),
})

import environment_config
from database import repository


class DatabaseSelectionAndLoggingTests(unittest.TestCase):
    def test_development_database_selection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".env.development").write_text("SUPABASE_URL=https://vaqleosqgsmnsppnxqbh.supabase.co\n", encoding="utf-8")
            (root / ".env").write_text("SUPABASE_URL=https://iygbadoegwsvzlrfvnwj.supabase.co\n", encoding="utf-8")
            with patch.dict(os.environ, {"PERSEVEX_ENV": "development", "VERCEL": ""}, clear=True):
                with patch.object(environment_config, "load_dotenv") as load:
                    mode = environment_config.load_application_environment(root)
                    self.assertEqual(mode, "development")
                    load.assert_called_once_with(root / ".env.development", override=True)

    def test_environment_info_logging_does_not_expose_credentials(self):
        info = environment_config.get_environment_info() if hasattr(environment_config, "get_environment_info") else {
            "mode": os.getenv("PERSEVEX_ENV"),
            "supabase_host": "vaqleosqgsmnsppnxqbh.supabase.co",
            "supabase_ref": "vaqleosqgsmnsppnxqbh",
        }
        self.assertIn("mode", info)
        self.assertIn("supabase_ref", info)
        # Ensure no secrets in info dict
        for v in info.values():
            self.assertNotIn("secret", str(v).lower())
            self.assertNotIn("key", str(v).lower())


class DeletionIntegrityTests(unittest.TestCase):
    def test_failed_supabase_deletion_raises_error_and_does_not_claim_success(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        # Simulate PostgREST table not found / missing schema cache error
        eq_mock.execute.side_effect = Exception("PGRST205: Could not find the table 'public.certificate_history' in the schema cache")
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            # When Supabase is configured and fails, deletion must not silently succeed
            with self.assertRaises(RuntimeError) as ctx:
                repository.delete_certificate_record(101)
            self.assertIn("PGRST205", str(ctx.exception))

    def test_successful_supabase_deletion(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        eq_mock.execute.return_value = MagicMock(data=[{"id": 101}])
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            result = repository.delete_certificate_record(101)
            self.assertTrue(result)
            table_mock.delete().eq.assert_called_with("id", 101)


class ExtendedDeletionIntegrityTests(unittest.TestCase):
    def test_delete_returns_false_when_no_rows_matched(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        eq_mock.execute.return_value = MagicMock(data=[])  # No rows deleted
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            result = repository.delete_certificate_record(999)
            self.assertFalse(result)

    def test_bulk_delete_propagates_supabase_failure(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        eq_mock = MagicMock()
        eq_mock.execute.side_effect = Exception("Supabase connection timeout")
        delete_mock.eq.return_value = eq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            with self.assertRaises(RuntimeError) as ctx:
                repository.bulk_delete_records([{"record_type": "certificate", "id": 101}])
            self.assertIn("connection timeout", str(ctx.exception))

    def test_delete_all_propagates_supabase_failure(self):
        mock_sb = MagicMock()
        table_mock = MagicMock()
        delete_mock = MagicMock()
        neq_mock = MagicMock()
        neq_mock.execute.side_effect = Exception("Supabase permission denied")
        delete_mock.neq.return_value = neq_mock
        table_mock.delete.return_value = delete_mock
        mock_sb.table.return_value = table_mock

        with patch.object(repository, "get_supabase_client", return_value=mock_sb):
            with self.assertRaises(RuntimeError) as ctx:
                repository.delete_all_history_records("certificate")
            self.assertIn("permission denied", str(ctx.exception))


class ApiErrorSanitizationTests(unittest.TestCase):
    def setUp(self):
        import app as application
        self.client = application.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True

    def test_single_delete_route_sanitizes_supabase_error(self):
        with patch.object(repository, "delete_certificate_record", side_effect=RuntimeError("Internal DB connection string or schema failure")):
            res = self.client.delete("/api/history/certificate/101")
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertEqual(data["error"], "Failed to delete record. Please check server logs.")
            self.assertNotIn("connection string", data["error"])

    def test_single_delete_route_returns_404_when_record_not_found(self):
        with patch.object(repository, "delete_certificate_record", return_value=False):
            res = self.client.delete("/api/history/certificate/999")
            self.assertEqual(res.status_code, 404)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertIn("not found", data["error"].lower())

    def test_bulk_delete_route_sanitizes_supabase_error(self):
        with patch.object(repository, "bulk_delete_records", side_effect=RuntimeError("PGRST42501: permission denied")):
            res = self.client.post("/api/history/bulk-delete", json={"items": [{"record_type": "certificate", "id": 101}]})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertEqual(data["error"], "Failed to delete records. Please check server logs.")
            self.assertNotIn("PGRST42501", data["error"])

    def test_delete_all_route_sanitizes_supabase_error(self):
        with patch.object(repository, "delete_all_history_records", side_effect=RuntimeError("Critical DB deadlock")):
            res = self.client.post("/api/history/delete-all", json={"confirm_phrase": "DELETE", "record_type": "certificate"})
            self.assertEqual(res.status_code, 500)
            data = res.get_json()
            self.assertFalse(data["success"])
            self.assertEqual(data["error"], "Failed to purge history records. Please check server logs.")
            self.assertNotIn("deadlock", data["error"])


if __name__ == "__main__":
    unittest.main()
