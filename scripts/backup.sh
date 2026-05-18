#!/bin/bash
# Sentinel v2 SQLite 备份 · gzip 到 KB _archive/backups/，14 天滚动。

set -e

SENTINEL_HOME="${SENTINEL_HOME:-$HOME/sentinel-v2}"
DB="$SENTINEL_HOME/db/messages.db"
BACKUP_DIR="$HOME/.kb/02-Work/1-Pixl/Projects/Sentinel/_archive/backups"

mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] DB 不存在: $DB"
    exit 0
fi

DATE=$(date '+%Y-%m-%d')
OUT="$BACKUP_DIR/messages-$DATE.db.gz"

# sqlite3 .backup 保证一致性（不阻塞活跃连接）
TMP=$(mktemp -t sentinel-backup)
sqlite3 "$DB" ".backup '$TMP'"
gzip -c "$TMP" > "$OUT"
rm -f "$TMP"

SIZE=$(ls -lh "$OUT" | awk '{print $5}')
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✓ 备份完成: $OUT ($SIZE)"

# 滚动：保留 14 天
find "$BACKUP_DIR" -name "messages-*.db.gz" -mtime +14 -delete -print | while read f; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 删除老备份: $f"
done

# 顺便跑 log 轮转（>10MB 截断到 .gz，保留 5 份）
"$SENTINEL_HOME/scripts/rotate-logs.sh" || true
