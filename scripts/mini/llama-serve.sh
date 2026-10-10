#!/bin/zsh
# What launchd runs to serve a model on the LAN: llama-server in the foreground,
# so that launchd sees it exit and starts it again.
#
# The model comes from ~/models/serve.conf, the default. A file at
# /tmp/llama-serve.override, written by start-server.sh, takes its place until
# it is removed or the machine reboots. An override that does not stay up (a
# model llama-server cannot load) is dropped on its third start in two minutes,
# so the default comes back instead of a restart every few seconds.
#
# One slot and a 16K context: the 256K default would spend most of the 32 GB on
# KV cache.
MODELS="$HOME/models"
CONF="$MODELS/serve.conf"
OVERRIDE=/tmp/llama-serve.override
STARTS="$OVERRIDE.starts"

if [ -f "$OVERRIDE" ]; then
  now=$(date +%s)
  echo "$now" >> "$STARTS"
  recent=$(awk -v now="$now" 'now - $1 < 120' "$STARTS" | wc -l)
  if [ "$recent" -ge 3 ]; then
    echo "$(date '+%F %T') $OVERRIDE did not stay up ($(grep '^MODEL=' "$OVERRIDE")); back to the default"
    rm -f "$OVERRIDE" "$STARTS"
  else
    CONF="$OVERRIDE"
  fi
fi

EXTRA=()
source "$CONF"
[[ "$MODEL" = /* ]] || MODEL="$MODELS/gguf/$MODEL"

if [ ! -f "$MODEL" ] || [ -z "$ALIAS" ]; then
  # launchd would start this again at once; give whoever reads the log a minute.
  echo "$(date '+%F %T') $CONF names no usable model: MODEL=$MODEL ALIAS=$ALIAS"
  sleep 60
  exit 1
fi

echo "$(date '+%F %T') serving $ALIAS from $MODEL ($CONF) $EXTRA"
exec /opt/homebrew/bin/llama-server \
  --model "$MODEL" \
  --alias "$ALIAS" \
  --host 0.0.0.0 --port 8080 \
  --api-key-file "$MODELS/api-key.txt" \
  --ctx-size 16384 --parallel 1 \
  --n-gpu-layers 999 \
  --log-file "$MODELS/server.log" \
  "${EXTRA[@]}"
