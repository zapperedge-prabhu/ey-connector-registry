-- ====================================================================
-- testfile File Connector - Bridge Schema Tables
-- Version: 1.0.0
-- Date: 2026-08-07
-- Description: Database table definitions for testfile file data
-- ====================================================================

-- ====================================================================
-- TABLE 1: testfile full_results data
-- ====================================================================
CREATE TABLE IF NOT EXISTS bridge.tbl_testfile_full_results (
    -- ===== MANDATORY METADATA COLUMNS (DO NOT MODIFY) =====
    tenant_id UUID NOT NULL,
    connector_instance_id UUID NOT NULL,
    created_on TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    updated_on TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    created_by VARCHAR(255),
    updated_by VARCHAR(255),
    is_deleted BOOLEAN DEFAULT FALSE,
    data_tags JSONB,
    remarks TEXT,
    execution_id UUID,

    -- ===== BUSINESS COLUMNS =====
    "true_page" INTEGER,
    "json_key_used" INTEGER,
    "remapped" BOOLEAN,
    "engine" TEXT,
    "cer" NUMERIC,
    "wer" NUMERIC,
    "word_recall" NUMERIC,
    "elapsed_s" NUMERIC,
    "char_count" INTEGER,
    "provided_word_hit_ratio" NUMERIC,
    "provided_word_hits" INTEGER,
    "classification_type" TEXT,
    "classification_confidence" TEXT,
    "gt_char_len" INTEGER,
    "hyp_char_len" INTEGER,
    "expected_class" TEXT,
    "class_correct" TEXT,

    -- Primary key
    PRIMARY KEY (tenant_id, connector_instance_id, execution_id)
);

-- Performance index (tenant + connector filtering)
CREATE INDEX IF NOT EXISTS idx_testfile_full_results_tenant
    ON bridge.tbl_testfile_full_results(tenant_id, connector_instance_id);

COMMENT ON TABLE bridge.tbl_testfile_full_results IS 'testfile full_results data';

-- ====================================================================
-- ERROR TABLE: Validation Failures (tbl_pre_process_testfile)
-- Records that fail pre-processing validation are logged here.
-- Schema: errors | Type: UNLOGGED (fast writes, not replicated)
-- ====================================================================
CREATE UNLOGGED TABLE IF NOT EXISTS errors.tbl_pre_process_testfile (
    -- ===== MANDATORY ERROR TABLE COLUMNS =====
    id UUID NOT NULL DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL,
    created_on TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    created_by TEXT,
    updated_on TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'UTC'),
    updated_by TEXT,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    data_tags JSONB,
    remarks TEXT,

    -- ===== ERROR TRACKING COLUMNS =====
    connector_instance_id UUID,
    execution_id TEXT,
    source_table TEXT,
    rule_id UUID,
    rule_description TEXT,
    row_id TEXT,
    failed_data JSONB,
    error_message TEXT,
    error_timestamp TIMESTAMP WITH TIME ZONE DEFAULT (NOW() AT TIME ZONE 'UTC'),

    CONSTRAINT pk_tbl_pre_process_testfile PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_error_testfile_tenant
    ON errors.tbl_pre_process_testfile(tenant_id);
CREATE INDEX IF NOT EXISTS idx_error_testfile_execution
    ON errors.tbl_pre_process_testfile(execution_id);
CREATE INDEX IF NOT EXISTS idx_error_testfile_instance
    ON errors.tbl_pre_process_testfile(connector_instance_id);
CREATE INDEX IF NOT EXISTS idx_error_testfile_rule
    ON errors.tbl_pre_process_testfile(rule_id);
CREATE INDEX IF NOT EXISTS idx_error_testfile_source
    ON errors.tbl_pre_process_testfile(source_table);

COMMENT ON TABLE errors.tbl_pre_process_testfile IS 'Error log for validation failures in testfile file connector';

-- ====================================================================
-- End of Model SQL
-- ====================================================================
-- Notes:
-- 1. Business tables are in the 'bridge' schema (intermediate staging area)
-- 2. Error table is in the 'errors' schema (keeps error logs separate)
-- 3. All business tables include 10 mandatory metadata columns at the top
-- 4. All business tables use CREATE TABLE IF NOT EXISTS (idempotent)
-- 5. Error table is UNLOGGED for performance (fast writes, not replicated)
-- 6. Performance indexes added for tenant filtering and business keys