"""DDL owned by the single workflow schema 2 -> 3 migration."""

SLOTS = ("user_avatar", "character_avatar", "pet_main", "pet_thinking", "pet_focused", "pet_pleased", "pet_alert")

ASSET_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE asset(id TEXT PRIMARY KEY,media_type TEXT NOT NULL CHECK(media_type='image/png'),bytes INTEGER NOT NULL CHECK(bytes>0),width INTEGER NOT NULL CHECK(width BETWEEN 1 AND 2048),height INTEGER NOT NULL CHECK(height BETWEEN 1 AND 2048),sha256 TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,provenance TEXT NOT NULL CHECK(provenance='user_import'),created_at TEXT NOT NULL)",
    "CREATE TABLE appearance_profile(slot TEXT PRIMARY KEY,asset_id TEXT REFERENCES asset(id),revision INTEGER NOT NULL DEFAULT 1,display_mode TEXT NOT NULL DEFAULT 'contain',crop_x REAL NOT NULL DEFAULT 0,crop_y REAL NOT NULL DEFAULT 0,crop_width REAL NOT NULL DEFAULT 1,crop_height REAL NOT NULL DEFAULT 1)",
    *("INSERT INTO appearance_profile(slot) VALUES('" + slot + "')" for slot in SLOTS),
)
