-- memory.db v2: the city each lead was first found in (from the run plan). migrate() backfills
-- existing rows from runs.db after this file runs.

ALTER TABLE leads ADD COLUMN city TEXT;
