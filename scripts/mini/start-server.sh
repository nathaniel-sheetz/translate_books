#!/bin/zsh
# Choose what llama-server serves on the LAN. launchd owns the process
# (~/Library/LaunchAgents/local.translate-books.llama-server.plist runs
# llama-serve.sh at login and again whenever it exits); this only says which
# model and restarts it.
#
#   start-server.sh <gguf> <alias> [flags]   serve this model until --default or a reboot
#   start-server.sh --default                go back to the default model
#   start-server.sh --set-default <gguf> <alias> [flags]
#                                            make this the default, and serve it
#   start-server.sh --stop                   stop serving and free the memory
#   start-server.sh --status                 what is configured and what is running
#   start-server.sh                          restart with what is configured
#
# <gguf> is a file in ~/models/gguf or a full path. Extra flags go to llama-server.
MODELS="$HOME/models"
LABEL=local.translate-books.llama-server
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
OVERRIDE=/tmp/llama-serve.override

conf_lines() {  # <gguf> <alias> [flags...]
  local model="$1" alias="$2"
  shift 2
  echo "MODEL=${(q)model}"
  echo "ALIAS=${(q)alias}"
  echo "EXTRA=(${(q)@})"
}

have_model() {  # <gguf>; llama-serve.sh would wait a minute on a missing one, over and over
  local model="$1"
  [[ "$model" = /* ]] || model="$MODELS/gguf/$model"
  [ -f "$model" ] || { echo "no such model: $model" >&2; exit 2; }
}

restart() {
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl kickstart -k "$DOMAIN/$LABEL"
  else
    launchctl bootstrap "$DOMAIN" "$PLIST"
  fi
}

status() {
  local conf="$MODELS/serve.conf"
  [ -f "$OVERRIDE" ] && conf="$OVERRIDE"
  echo "configured ($conf):"
  grep -v '^#' "$conf" | sed 's/^/  /'
  if pgrep -x llama-server >/dev/null; then
    echo "running: $(curl -s -m 5 -H "Authorization: Bearer $(cat "$MODELS/api-key.txt")" \
      http://127.0.0.1:8080/v1/models | sed -n 's/.*"data":\[{"id":"\([^"]*\)".*/\1/p')"
  else
    echo "running: nothing"
  fi
}

case "$1" in
  --status)
    status
    exit 0 ;;
  --stop)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
    echo "llama-server stopped; any other form of this command starts it again"
    exit 0 ;;
  --default)
    rm -f "$OVERRIDE" "$OVERRIDE.starts" ;;
  --set-default)
    [ $# -ge 3 ] || { echo "usage: start-server.sh --set-default <gguf> <alias> [flags]" >&2; exit 2; }
    shift
    have_model "$1"
    # The comments at the head of the file stay; the last default is kept beside it.
    header="$(grep '^#' "$MODELS/serve.conf" 2>/dev/null)"
    cp "$MODELS/serve.conf" "$MODELS/serve.conf.previous" 2>/dev/null
    { [ -n "$header" ] && print -r -- "$header"; conf_lines "$@"; } > "$MODELS/serve.conf"
    rm -f "$OVERRIDE" "$OVERRIDE.starts" ;;
  "")
    ;;
  -*)
    echo "unknown option $1" >&2
    exit 2 ;;
  *)
    [ $# -ge 2 ] || { echo "usage: start-server.sh <gguf> <alias> [flags]" >&2; exit 2; }
    have_model "$1"
    rm -f "$OVERRIDE.starts"
    conf_lines "$@" > "$OVERRIDE" ;;
esac

restart && status
