#!/bin/bash
# Sentinel v2 log 轮转 · 防 logs/*.log 无限增长。
#
# 策略：
#   - 扫 logs/*.log 大小，超过 MAX_SIZE_MB（默认 10MB）的 mv → .1.gz
#   - 保留最近 ROTATE_KEEP 份（默认 5 份），更老的删除
#   - 同时清理 .err.log（合并处理）
#
# 触发：每周日 23:50 由 launchd（合并进 backup.plist 同时段），或人工跑。

set -e

SENTINEL_HOME="${SENTINEL_HOME:-$HOME/sentinel-v2}"
LOGS_DIR="$SENTINEL_HOME/logs"
MAX_SIZE_MB="${MAX_SIZE_MB:-10}"
ROTATE_KEEP="${ROTATE_KEEP:-5}"

[ -d "$LOGS_DIR" ] || { echo "[rotate-logs] no logs dir"; exit 0; }

cd "$LOGS_DIR"

for f in *.log; do
    [ -f "$f" ] || continue
    size_mb=$(du -m "$f" | awk '{print $1}')
    if [ "$size_mb" -lt "$MAX_SIZE_MB" ]; then
        continue
    fi
    ts=$(date +%Y%m%d-%H%M%S)
    rotated="${f%.log}.${ts}.log.gz"
    gzip -c "$f" > "$rotated"
    : > "$f"  # truncate in-place（launchd 句柄不丢）
    echo "[rotate-logs] $(date '+%Y-%m-%d %H:%M:%S') rotated $f → $rotated ($size_mb MB)"
done

# 保留最近 N 份每个 base name 的 .gz（macOS bash 3.2 兼容 · 无 declare -A）
bases=$(ls -1 *.log.gz 2>/dev/null \
        | sed -E 's/\.[0-9]+-[0-9]+\.log\.gz$//' \
        | sort -u)
for base in $bases; do
    # 按 mtime 降序，取超出 ROTATE_KEEP 部分删除
    ls -t "$base".*.log.gz 2>/dev/null \
        | tail -n +$((ROTATE_KEEP + 1)) \
        | while read old; do
            rm -f "$old"
            echo "[rotate-logs] purged old: $old"
          done
done
