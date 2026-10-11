CREATE TABLE IF NOT EXISTS pr_meta (
    pr_key TEXT PRIMARY KEY,
    data TEXT,
    head_sha TEXT,
    comment_count INTEGER DEFAULT 0,
    review_count INTEGER DEFAULT 0,
    commit_count INTEGER DEFAULT 0,
    fetched_at REAL
);
CREATE TABLE IF NOT EXISTS pr_files (
    pr_key TEXT, head_sha TEXT, data TEXT,
    PRIMARY KEY (pr_key, head_sha)
);
CREATE TABLE IF NOT EXISTS pr_commits (
    pr_key TEXT, sha TEXT, data TEXT,
    PRIMARY KEY (pr_key, sha)
);
CREATE TABLE IF NOT EXISTS pr_comments (
    pr_key TEXT, id INTEGER, data TEXT,
    PRIMARY KEY (pr_key, id)
);
CREATE TABLE IF NOT EXISTS pr_reviews (
    pr_key TEXT, id INTEGER, data TEXT,
    PRIMARY KEY (pr_key, id)
);
CREATE TABLE IF NOT EXISTS user_profiles (
    username TEXT PRIMARY KEY,
    data TEXT,
    fetched_at REAL
);
