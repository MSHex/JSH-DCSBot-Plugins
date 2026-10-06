CREATE TABLE IF NOT EXISTS jsh_airport_levels (
    server_name TEXT NOT NULL,
    airbase TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    updated_by TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    PRIMARY KEY (server_name, airbase),
    FOREIGN KEY (server_name) REFERENCES servers (server_name) ON UPDATE CASCADE ON DELETE CASCADE
);
