from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


class DeliveryMigrationTests(unittest.TestCase):
    def test_idempotency_upgrade_hashes_raw_ids_and_nulls_cross_conversation_duplicates(self) -> None:
        migration_path = (
            Path(__file__).parents[1] / "alembic" / "versions"
            / "c9d0e1f2a3b4_strengthen_message_idempotency.py"
        )
        spec = importlib.util.spec_from_file_location("migration_c9", migration_path)
        migration = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(migration)
        bind = MagicMock()
        bind.execute.return_value.mappings.return_value.all.return_value = [
            {"id": 1, "tenant_id": 7, "source_message_id": "raw-42",
             "channel": "douyin#shop_a", "contact_id": "wxid_a"},
            {"id": 2, "tenant_id": 7, "source_message_id": "raw-42",
             "channel": "douyin#shop_a", "contact_id": "wxid_a"},
            {"id": 3, "tenant_id": 7, "source_message_id": "raw-42",
             "channel": "douyin#shop_a", "contact_id": "wxid_b"},
        ]
        batch = MagicMock()

        with (
            patch.object(migration.op, "get_bind", return_value=bind),
            patch.object(migration.op, "batch_alter_table") as batch_context,
        ):
            batch_context.return_value.__enter__.return_value = batch
            migration.upgrade()

        updates = [call.args[1] for call in bind.execute.call_args_list[1:]]
        digest_a = hashlib.sha256(b"douyin#shop_a\0wxid_a\0raw-42").hexdigest()
        digest_b = hashlib.sha256(b"douyin#shop_a\0wxid_b\0raw-42").hexdigest()
        self.assertEqual(
            [
                {"value": digest_a, "id": 1},
                {"value": None, "id": 2},
                {"value": digest_b, "id": 3},
            ],
            updates,
        )
        batch.create_unique_constraint.assert_called_once_with(
            "uq_message_source_per_tenant", ["tenant_id", "source_message_id"],
        )


if __name__ == "__main__":
    unittest.main()
