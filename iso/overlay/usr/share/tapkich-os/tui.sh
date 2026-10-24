# TapkicH-OS TUI helpers (sourced). One consistent interaction everywhere:
#   UP/DOWN  move      SPACE  select / unselect      ENTER  continue      ESC / LEFT  back (or cancel)
#   type a letter  jump to the next item starting with it (press again for the next one)
#
#   ui_select one  "Question" item...   exactly one item must be selected (SPACE picks it, replacing any other)
#   ui_select many "Question" item...   one or more; the order you pick them in is kept (shown as [1] [2] ...)
#   ui_confirm "Question" "Yes label" "No label"     SPACE to pick, ENTER to continue; returns 0 for the first
#
# Items listed (space separated indexes) in UI_RISKY are drawn as risky actions (red).
# Results: UI_PICKED (array of picked items, in pick order) and UI_PICKED_IDX (their indexes).
# Return codes: 0 = continued, 1 = cancelled.
#
# Text styles, set by the caller as SGR parameter strings (defaults are 16-colour console codes):
#   TUI_HEAD   question / subheader        TUI_BODY    normal items        TUI_MUTED  hints, scroll info
#   TUI_ACCENT item under the cursor       TUI_SEL     selected items      TUI_RISK   risky action

TUI_HEAD=${TUI_HEAD:-34}
TUI_BODY=${TUI_BODY:-37}
TUI_MUTED=${TUI_MUTED:-90}
TUI_ACCENT=${TUI_ACCENT:-1;35}
TUI_SEL=${TUI_SEL:-34}
TUI_RISK=${TUI_RISK:-1;91}
TUI_PAD=${TUI_PAD:-4}
UI_RISKY=${UI_RISKY:-}
UI_SEP=${UI_SEP:-}         # indexes drawn as an empty line (a gap) that the cursor skips, e.g. between a suggestion and the full list

ui_select() {
  local mode=$1 header=$2; shift 2
  local -a items=("$@") order=() sel=()
  local n=${#items[@]} cur=0 top=0 i key rest hint="" lines
  local h=$(( n < 10 ? n : 10 ))
  local pad; printf -v pad '%*s' "$TUI_PAD" ''
  local -a risky=() sep=(); for i in $UI_RISKY; do risky[i]=1; done; for i in $UI_SEP; do sep[i]=1; done
  for ((i = 0; i < n; i++)); do sel[i]=0; done
  UI_PICKED=(); UI_PICKED_IDX=()
  lines=$(( h + 3 ))

  _ui_draw() {
    local first=${1:-0} j idx mark style cursor
    ((first)) || printf '\e[%dA' "$lines"
    printf '\e[2K\r%s\e[%sm%s\e[0m\n' "$pad" "$TUI_HEAD" "$header"
    for ((j = 0; j < h; j++)); do
      idx=$((top + j))
      if ((${sep[idx]:-0})); then printf '\e[2K\r\n'; continue; fi
      if ((sel[idx])); then
        if [[ $mode == many ]]; then
          local pos=0 k; for k in "${!order[@]}"; do [[ ${order[k]} == "$idx" ]] && pos=$((k + 1)); done
          mark="[$pos]"
        else
          mark="[x]"
        fi
      else
        mark="[ ]"
      fi
      if ((idx == cur)); then cursor=">"; else cursor=" "; fi
      if ((${risky[idx]:-0})); then style=$TUI_RISK
      elif ((idx == cur)); then style=$TUI_ACCENT
      elif ((sel[idx])); then style=$TUI_SEL
      else style=$TUI_BODY; fi
      printf '\e[2K\r%s\e[%sm%s %s %s\e[0m\n' "$pad" "$style" "$cursor" "$mark" "${items[idx]}"
    done
    if ((n > h)); then
      printf '\e[2K\r%s\e[%sm  (%d-%d of %d)\e[0m\n' "$pad" "$TUI_MUTED" $((top + 1)) $((top + h)) "$n"
    else
      printf '\e[2K\r\n'
    fi
    printf '\e[2K\r%s\e[%sm%s\e[0m\n' "$pad" "$TUI_MUTED" "${hint:-SPACE select   ENTER continue   ESC back   UP/DOWN move$( ((n > h)) && echo '   type a letter to jump')}"
  }

  UI_EOF=0
  { : </dev/tty; } 2>/dev/null || { UI_EOF=1; return 1; }       # no terminal at all: nothing to ask
  exec 9>&1; exec >/dev/tty
  printf '\e[?25l'
  _ui_draw 1
  while true; do
    IFS= read -rsn1 key </dev/tty || { UI_EOF=1; printf '\e[?25h'; exec >&9 9>&-; return 1; }   # keyboard gone
    case $key in
      $'\e')
        rest=""; IFS= read -rsn2 -t 0.05 rest </dev/tty || true
        case $rest in
          '[A') ((cur > 0)) && ((cur--)); while ((${sep[cur]:-0} && cur > 0)); do ((cur--)); done ;;
          '[B') ((cur < n - 1)) && ((cur++)); while ((${sep[cur]:-0} && cur < n - 1)); do ((cur++)); done ;;
          '[D') printf '\e[?25h'; exec >&9 9>&-; return 1 ;;      # LEFT arrow: back
          '') printf '\e[?25h'; exec >&9 9>&-; return 1 ;;      # bare ESC: back
        esac ;;
      $'\x7f'|$'\b') printf '\e[?25h'; exec >&9 9>&-; return 1 ;;   # BACKSPACE: back
      [[:alpha:]])      # type a letter: jump to the next item that starts with it (handy in long lists)
        local lk=${key,,} start=$cur step it
        for ((step = 1; step <= n; step++)); do
          it=$(( (start + step) % n ))
          if [[ ${items[it],,} == "$lk"* ]]; then cur=$it; break; fi
        done ;;
      ' ')
        hint=""
        if [[ $mode == one ]]; then
          for ((i = 0; i < n; i++)); do sel[i]=0; done
          sel[cur]=1; order=("$cur")
        elif ((sel[cur])); then
          sel[cur]=0
          local -a keep=(); for i in "${order[@]}"; do [[ $i != "$cur" ]] && keep+=("$i"); done; order=("${keep[@]}")
        else
          sel[cur]=1; order+=("$cur")
        fi ;;
      '')   # ENTER
        if ((${#order[@]} == 0)); then hint="Press SPACE to select, then ENTER."; else break; fi ;;
    esac
    ((cur < top)) && top=$cur
    ((cur >= top + h)) && top=$((cur - h + 1))
    _ui_draw 0
  done
  printf '\e[?25h'
  exec >&9 9>&-
  for i in "${order[@]}"; do UI_PICKED+=("${items[i]}"); UI_PICKED_IDX+=("$i"); done
  return 0
}

ui_confirm() {
  local header=$1 yes=$2 no=$3
  ui_select one "$header" "$yes" "$no" || return 1
  [[ ${UI_PICKED_IDX[0]} == 0 ]]
}
