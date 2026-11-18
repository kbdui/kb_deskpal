"""一次性为当前角色的待确认理解补期限；SQLite在线备份、整批事务、不可变修订。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


def apply_deadline(data_dir: Path, deadline: datetime) -> dict:
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError('Deadline must include a timezone')
    now = datetime.now(timezone.utc)
    if deadline <= now:
        raise ValueError('Deadline must be in the future')
    registry = data_dir / 'profiles.json'
    selected = json.loads(registry.read_text(encoding='utf-8'))['selected_id']
    if not isinstance(selected, str) or len(selected) != 32 or any(c not in '0123456789abcdef' for c in selected):
        raise ValueError('Invalid selected profile')
    profile = (data_dir / 'profiles' / selected).resolve(strict=True)
    if not profile.is_relative_to(data_dir.resolve() / 'profiles'):
        raise ValueError('Profile escapes data directory')
    database = profile / 'memory.sqlite3'
    backup_dir = data_dir / 'repair-backups' / ('pending-archive-' + now.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8])
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / 'memory.sqlite3'
    stamp = deadline.astimezone(timezone.utc).isoformat(timespec='microseconds')
    conn = sqlite3.connect(database.resolve(strict=True).as_uri() + '?mode=rw', uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        # 保持写入预留锁，备份读连接仍可读取提交快照，其他写者不能穿插修改本批。
        conn.execute('BEGIN IMMEDIATE')
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(backup_path)) as backup:
                source.backup(backup)
                if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Backup validation failed')
        if json.loads(registry.read_text(encoding='utf-8'))['selected_id'] != selected:
            raise RuntimeError('Active profile changed before update')
        rows = conn.execute("SELECT r.* FROM memory_heads h JOIN memory_revisions r "
            "ON r.id=h.id AND r.revision=h.revision WHERE r.status IN ('candidate','disputed')").fetchall()
        changed = []
        for row in rows:
            draft = json.loads(row['draft'])
            if draft.get('archive_after') == stamp:
                continue
            draft['archive_after'] = stamp
            conn.execute('INSERT INTO memory_revisions(id,revision,status,source_id,draft,recorded_at,accepted_at,supersedes,reason,default_expiry) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)', (row['id'], row['revision']+1, row['status'], row['source_id'],
                json.dumps(draft, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')),
                now.isoformat(timespec='microseconds'), row['accepted_at'], row['supersedes'],
                'user_pending_archive_deadline', row['default_expiry']))
            cursor = conn.execute('UPDATE memory_heads SET revision=? WHERE id=? AND revision=?',
                (row['revision']+1, row['id'], row['revision']))
            if cursor.rowcount != 1:
                raise RuntimeError('Memory revision changed')
            conn.execute('DELETE FROM memory_embeddings WHERE memory_id=?', (row['id'],))
            changed.append(row['id'])
        if changed:
            conn.execute("UPDATE memory_meta SET value=value+1 WHERE name='epoch'")
        remaining = conn.execute("SELECT COUNT(*) FROM memory_heads h JOIN memory_revisions r "
            "ON r.id=h.id AND r.revision=h.revision WHERE r.status IN ('candidate','disputed') "
            "AND (json_extract(r.draft,'$.archive_after') IS NULL OR json_extract(r.draft,'$.archive_after')!=?)", (stamp,)).fetchone()[0]
        if remaining:
            raise RuntimeError('Pending deadline verification failed')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    receipt = dict(profile=selected, deadline=stamp, changed_count=len(changed), ids=changed,
        backup=str(backup_path.resolve()), committed_at=now.isoformat())
    (backup_dir / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--deadline', type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    print(json.dumps(apply_deadline(args.data_dir, args.deadline)))
