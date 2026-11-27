CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;  -- version = '5'
CREATE TABLE files (
    key TEXT PRIMARY KEY, target TEXT NOT NULL, plugin TEXT NOT NULL,
    post_id TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '', path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0, mtime INTEGER, date TEXT,
    estimated INTEGER NOT NULL DEFAULT 0, kind TEXT, files INTEGER,
    state TEXT NOT NULL DEFAULT 'saved',            -- saved | gone | link
    unpacked TEXT NOT NULL DEFAULT '', folder TEXT, extracted TEXT,
    at TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
CREATE INDEX files_post ON files (target, post_id);
CREATE INDEX files_undated ON files (target) WHERE state = 'saved' AND (date IS NULL OR estimated);
CREATE VIEW posts AS
    SELECT target, post_id, min(category) AS category, max(date) AS date,
           max(count(*), coalesce(max(files), 1)) AS files, max(at) AS at
    FROM files WHERE state = 'saved' GROUP BY target, post_id;
CREATE TABLE targets (
    key TEXT PRIMARY KEY, plugin TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL,
    follow INTEGER NOT NULL DEFAULT 0, categories TEXT NOT NULL DEFAULT '[]', query TEXT,
    complete TEXT NOT NULL DEFAULT '{}', cursors TEXT NOT NULL DEFAULT '{}',
    last_sync TEXT NOT NULL DEFAULT '',
    files INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
    heads TEXT NOT NULL DEFAULT '{}', folder TEXT
) WITHOUT ROWID;
CREATE TABLE target_seen (
    target TEXT NOT NULL, item TEXT NOT NULL, ord INTEGER NOT NULL,
    PRIMARY KEY (target, item)
) WITHOUT ROWID;
CREATE INDEX target_seen_ord ON target_seen (target, ord);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY, command TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
    saved INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE failures (
    run INTEGER, key TEXT NOT NULL, target TEXT NOT NULL, post_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '', file TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '', class TEXT NOT NULL, message TEXT NOT NULL,
    hint TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 1, at TEXT NOT NULL,
    cursor TEXT, retryable INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX failures_target ON failures (target, key);
CREATE INDEX failures_run ON failures (run);
CREATE UNIQUE INDEX failures_key ON failures (key);
CREATE TABLE state (plugin TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY (plugin, key)) WITHOUT ROWID;

-- A version 1 home: flnd at e179bf1 wrote meta.version "5". Example values only.
INSERT INTO meta VALUES ('version', '5');

-- loi:post/101 and its twin loi:101 (to merge); loi:post/102 alone; a search found both.
INSERT INTO targets (key, plugin, value, kind, follow, categories, last_sync, files, bytes, heads, complete)
VALUES ('loi:post/101', 'loi', 'post/101', 'post', 1, '[]', '2026-11-01T00:00:00+00:00', 2, 20,
        '{"info": "loi:post/101"}', '{"info": "2026-11-01"}'),
       ('loi:101', 'loi', '101', 'post', 0, '["info"]', '2026-11-05T00:00:00+00:00', 1, 5, '{}', '{}'),
       ('loi:post/102', 'loi', 'post/102', 'post', 0, '[]', '2026-11-02T00:00:00+00:00', 1, 7, '{}', '{}'),
       ('loi:author/9', 'loi', 'author/9', 'author', 1, '[]', '2026-11-03T00:00:00+00:00', 0, 0, '{}', '{}');
INSERT INTO files (key, target, plugin, post_id, category, path, size, state)
VALUES ('loi:a_0', 'loi:post/101', 'loi', '101', 'info', 'media/loi/example_author/101 One/a.jpg', 10, 'saved'),
       ('loi:a_1', 'loi:post/101', 'loi', '101', 'info', 'media/loi/example_author/101 One/b.jpg', 10, 'saved'),
       ('loi:a_2', 'loi:101', 'loi', '101', 'info', 'media/loi/example_author/101 One/c.jpg', 5, 'saved'),
       ('loi:d_0', 'loi:post/102', 'loi', '102', 'info', 'media/loi/example_author/102 Two/d.jpg', 7, 'saved');
INSERT INTO target_seen (target, item, ord)
VALUES ('loi:author/9', 'loi:post/101', 0), ('loi:author/9', 'loi:post/102', 1),
       ('loi:post/101', 'x', 0), ('loi:101', 'y', 0);
INSERT INTO failures (run, key, target, post_id, class, message, at)
VALUES (1, 'loi:post/102', 'loi:post/102', '', 'NotFound', 'gone', '2026-11-02T00:00:00+00:00'),
       (1, 'loi:f_0', 'loi:post/101', '101', 'Blocked', 'slow', '2026-11-02T00:00:00+00:00');

-- A removed route's targets, and a search that finds posts.
INSERT INTO targets (key, plugin, value, kind, follow, last_sync, files, bytes)
VALUES ('tlg:examplechannel', 'tlg', 'examplechannel', 'channel', 1, '2026-11-01T00:00:00+00:00', 1, 3),
       ('tik:example words', 'tik', 'example words', 'search', 1, '2026-11-01T00:00:00+00:00', 0, 0),
       ('loi:example words', 'loi', 'example words', 'search', 1, '2026-11-01T00:00:00+00:00', 0, 0),
       ('twt:example_user', 'twt', 'example_user', 'user', 1, '2026-11-01T00:00:00+00:00', 1, 4);
INSERT INTO files (key, target, plugin, post_id, category, path, size, state)
VALUES ('tlg:m_0', 'tlg:examplechannel', 'tlg', '1', 'posts', 'media/tlg/examplechannel/m.jpg', 3, 'saved'),
       ('twt:t_0', 'twt:example_user', 'twt', '1', 'posts', 'media/twt/example_user/t.jpg', 4, 'saved');
INSERT INTO runs (id, command, started) VALUES (1, 'get', '2026-11-01T00:00:00+00:00');
