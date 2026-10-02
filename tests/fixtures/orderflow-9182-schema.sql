-- Synthetic fixture schema dumped from Store at OrderFlow commit
-- 9182d8fd56becfee07651b99c6ab09a9e13e1fa4; contains no user rows.
BEGIN TRANSACTION;
CREATE TABLE documents (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_key TEXT NOT NULL,
                  size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_ms INTEGER NOT NULL,
                  upload_ms INTEGER NOT NULL, steps TEXT NOT NULL, page_count INTEGER,
                  purpose TEXT NOT NULL DEFAULT 'diagnostic'
                    CHECK (purpose IN ('diagnostic','management')), document_kind TEXT CHECK (document_kind IN ('purchase_order','invoice')),
                  UNIQUE(session_id, request_key)
                );
CREATE TABLE jobs (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, document_id TEXT NOT NULL,
                  request_key TEXT NOT NULL, attempt INTEGER NOT NULL, scenario TEXT NOT NULL,
                  state TEXT NOT NULL, error_code TEXT, result TEXT, steps TEXT NOT NULL,
                  created_ms INTEGER NOT NULL, started_ms INTEGER, finished_ms INTEGER,
                  UNIQUE(session_id, document_id, request_key)
                );
CREATE TABLE management_drafts (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                document_id TEXT NOT NULL REFERENCES documents(id),
                source_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
                rows_json TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
                updated_ms INTEGER NOT NULL
            );
CREATE TABLE management_local_sources (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                document_id TEXT NOT NULL UNIQUE REFERENCES documents(id),
                request_key TEXT NOT NULL, parser_id TEXT NOT NULL,
                candidate_rows_json TEXT NOT NULL, created_ms INTEGER NOT NULL,
                UNIQUE(session_id, request_key)
            );
CREATE TABLE management_record_sets (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                document_id TEXT NOT NULL UNIQUE REFERENCES documents(id),
                source_job_id TEXT REFERENCES jobs(id),
                source_local_id TEXT REFERENCES management_local_sources(id),
                kind TEXT NOT NULL CHECK (kind IN ('purchase_order','invoice')),
                rows_json TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision > 0),
                created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
                CHECK ((source_job_id IS NOT NULL) != (source_local_id IS NOT NULL))
            );
CREATE TABLE management_upload_keys (
                session_id TEXT NOT NULL, request_key TEXT NOT NULL,
                document_id TEXT NOT NULL REFERENCES documents(id),
                sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                document_kind TEXT NOT NULL CHECK (document_kind IN ('purchase_order','invoice')),
                PRIMARY KEY (session_id, request_key)
            );
CREATE TABLE session_auth (
                  session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                  authenticated_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL
                );
CREATE TABLE sessions (
                  id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, created_ms INTEGER NOT NULL
                );
COMMIT;
