-- 0004: indexes on the foreign-key columns that the delete + join paths hit.
--
-- update_tasks/provisioning_tasks reference servers(id) and firmware(id) with
-- ON DELETE SET NULL (migrations 0002/0003). Without an index on the child
-- column, every server/firmware DELETE makes SQLite full-scan the child table
-- to find the rows to NULL, and jobs_repo.list_tasks_with_server's LEFT JOINs
-- scan as job history grows. These keep deletes and the jobs page O(log n).

CREATE INDEX IF NOT EXISTS idx_tasks_server      ON update_tasks(server_id);
CREATE INDEX IF NOT EXISTS idx_tasks_fw          ON update_tasks(firmware_id);
CREATE INDEX IF NOT EXISTS idx_prov_tasks_server ON provisioning_tasks(server_id);
