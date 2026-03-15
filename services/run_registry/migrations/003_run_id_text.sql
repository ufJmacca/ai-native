ALTER TABLE runs
    ALTER COLUMN run_id TYPE TEXT USING run_id::text;
