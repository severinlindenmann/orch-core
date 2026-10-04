#!/bin/sh
# Status line that also records Claude's usage-limit percentages for orch's ticket-usage addon.
# Claude Code passes JSON on stdin; only rate_limits is recorded, and only when a value changed.
# Output: one short line, e.g. "Opus 5.5 · 5h 38% · week 61%".
dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/orch-usage"
log="$dir/limits.jsonl"
input=$(cat)
row=$(printf '%s' "$input" | jq -c '{
  five: .rate_limits.five_hour.used_percentage, five_reset: .rate_limits.five_hour.resets_at,
  week: .rate_limits.seven_day.used_percentage, week_reset: .rate_limits.seven_day.resets_at,
  session: .session_id, model: .model.id }' 2>/dev/null)
if [ -n "$row" ] && [ "$(printf '%s' "$row" | jq -r '.five // .week // empty')" != "" ]; then
  key=$(printf '%s' "$row" | jq -c '[.five, .week, .session]')
  if [ "$key" != "$(cat "$dir/.last" 2>/dev/null)" ]; then
    mkdir -p "$dir"
    printf '%s' "$row" | jq -c --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{at: $at} + .' >> "$log"
    printf '%s' "$key" > "$dir/.last"
  fi
fi
printf '%s' "$input" | jq -r '[.model.display_name // empty,
  (if .rate_limits.five_hour.used_percentage then "5h \(.rate_limits.five_hour.used_percentage | round)%" else empty end),
  (if .rate_limits.seven_day.used_percentage then "week \(.rate_limits.seven_day.used_percentage | round)%" else empty end)] | join(" · ")' 2>/dev/null
