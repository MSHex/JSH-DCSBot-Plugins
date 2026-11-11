CREATE TABLE IF NOT EXISTS jsh_awards_boards (
    month DATE PRIMARY KEY,
    posted_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    message_id BIGINT,
    note TEXT
);
