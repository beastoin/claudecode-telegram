#!/bin/bash
# host_metrics.sh — collect host metrics as JSON
# Called by bridge.py watchdog; replaces per-metric Python parsers.
# Usage: host_metrics.sh <metric> where metric is:
#   disk | mem | io | cpu_hogs | worktree | tailscale | all
set -euo pipefail

is_mac() { [[ "$(uname)" == "Darwin" ]]; }

metric_disk() {
    if is_mac; then
        line=$(df -g / | tail -1)
        set -- $line
        total=$2; free=$4; pct=${5%\%}
    else
        line=$(df -BG --output=size,used,avail,pcent / | tail -1)
        set -- $line
        total=${1%G}; free=${3%G}; pct=${4%\%}
    fi
    printf '{"pct":%d,"free_gb":%s,"total_gb":%s}\n' "$pct" "$free" "$total"
}

metric_mem() {
    if is_mac; then
        total=$(sysctl -n hw.memsize)
        eval "$(vm_stat | awk -F: '
            /page size/  { gsub(/[^0-9]/,"",$2); ps=$2 }
            /Pages free/ { gsub(/[^0-9]/,"",$2); free=$2 }
            /Pages inactive/ { gsub(/[^0-9]/,"",$2); inactive=$2 }
            /Pages speculative/ { gsub(/[^0-9]/,"",$2); spec=$2 }
            END { printf "avail=%d\n", (free+inactive+spec)*(ps?ps:16384) }')"
        used=$((total - avail))
    else
        read -r _ total used _ _ _ avail < <(free -b | grep '^Mem:')
    fi
    pct=$((used * 100 / total))
    total_gb=$(awk "BEGIN{printf \"%.1f\", $total/1073741824}")
    used_gb=$(awk "BEGIN{printf \"%.1f\", $used/1073741824}")
    avail_gb=$(awk "BEGIN{printf \"%.1f\", $avail/1073741824}")
    # top 5 memory procs
    if is_mac; then
        procs=$(ps -eo pid=,rss=,comm= -r 2>/dev/null | head -5 | awk '{printf "{\"pid\":\"%s\",\"rss_gb\":%.1f,\"cmd\":\"%s\"},", $1, $2/1048576, $3}')
    else
        procs=$(ps aux --sort=-rss 2>/dev/null | awk 'NR>1 && NR<=6{printf "{\"pid\":\"%s\",\"rss_gb\":%.1f,\"pct\":\"%s\",\"cmd\":\"%s\"},", $2, $6/1048576, $4, substr($11,1,80)}')
    fi
    procs="[${procs%,}]"
    printf '{"pct":%d,"used_gb":%s,"total_gb":%s,"avail_gb":%s,"top_procs":%s}\n' \
        "$pct" "$used_gb" "$total_gb" "$avail_gb" "$procs"
}

metric_io() {
    if is_mac; then
        line=$(iostat -c 2 -w 1 2>/dev/null | tail -1)
        set -- $line
        printf '{"iowait_pct":0,"read_iops":%d,"write_iops":%d,"util_pct":0}\n' \
            "${1%%.*}" "${2%%.*}" 2>/dev/null || echo 'null'
    else
        # Try iostat JSON first
        if out=$(iostat -x -d 1 2 -o JSON 2>/dev/null) && [ -n "$out" ]; then
            result=$(echo "$out" | python3 -c "
import sys, json
d = json.load(sys.stdin)
stats = d.get('sysstat',{}).get('hosts',[{}])[0].get('statistics',[])
if len(stats) >= 2:
    disks = stats[-1].get('disk',[])
    r = sum(x.get('r/s',0) for x in disks)
    w = sum(x.get('w/s',0) for x in disks)
    u = max((x.get('util',x.get('%util',0)) for x in disks), default=0)
    print(json.dumps({'iowait_pct':0,'read_iops':round(r),'write_iops':round(w),'util_pct':round(u,1)}))
else:
    print('null')
" 2>/dev/null)
            if [ -n "$result" ] && [ "$result" != "null" ]; then
                # Get iowait from vmstat
                iowait=$(vmstat 1 2 2>/dev/null | tail -1 | awk '{print $16}')
                echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); d['iowait_pct']=float('${iowait:-0}'); print(json.dumps(d))"
                return
            fi
        fi
        # Fallback: vmstat only
        line=$(vmstat 1 2 2>/dev/null | tail -1)
        set -- $line
        if [ $# -ge 16 ]; then
            printf '{"iowait_pct":%s,"read_iops":%s,"write_iops":%s,"util_pct":0}\n' \
                "${16}" "${9}" "${10}"
        else
            echo 'null'
        fi
    fi
}

metric_cpu_hogs() {
    threshold=${CPU_HOG_PCT:-90}
    if is_mac; then
        cmd="ps -eo pid,pcpu,etime,comm -r"
    else
        cmd="ps -eo pid,pcpu,etime,comm --sort=-pcpu"
    fi
    $cmd 2>/dev/null | awk -v thresh="$threshold" '
    NR>1 {
        pid=$1; cpu=$2+0; etime=$3; cmd=$4
        if (cpu < thresh) exit
        # Parse etime: [[dd-]hh:]mm:ss
        days=0; hours=0; mins=0
        n=split(etime, a, "[-:]")
        if (n==4) { days=a[1]; hours=a[2]; mins=a[3] }
        else if (n==3) { hours=a[1]; mins=a[2] }
        else if (n==2) { mins=a[1] }
        total_min = days*1440 + hours*60 + mins
        printf "{\"pid\":%d,\"cpu\":%.1f,\"etime_min\":%d,\"cmd\":\"%s\"},", pid, cpu, total_min, substr(cmd,1,80)
    }' | sed 's/,$//' | awk '{print "["$0"]"}'
}

metric_worktree() {
    if is_mac; then flag="-sk"; div=1024; else flag="-sb"; div=1; fi
    total=0
    items="["
    while IFS=$'\t' read -r size path; do
        bytes=$((size * (is_mac && echo 1024 || echo 1)))
        gb=$(awk "BEGIN{printf \"%.1f\", $size*$div/1073741824}")
        total=$((total + size * div))
        items="${items}{\"path\":\"$path\",\"size_gb\":$gb},"
    done < <(find "$HOME" -maxdepth 4 -type d -name worktrees 2>/dev/null | while read d; do du $flag "$d" 2>/dev/null; done)
    items="${items%,}]"
    total_gb=$(awk "BEGIN{printf \"%.1f\", $total/1073741824}")
    printf '{"total_gb":%s,"items":%s}\n' "$total_gb" "$items"
}

metric_tailscale() {
    if tailscale status --json 2>/dev/null | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('BackendState')=='Running' else 1)" 2>/dev/null; then
        echo '{"up":true}'
    else
        echo '{"up":false}'
    fi
}

case "${1:-all}" in
    disk)       metric_disk ;;
    mem)        metric_mem ;;
    io)         metric_io ;;
    cpu_hogs)   metric_cpu_hogs ;;
    worktree)   metric_worktree ;;
    tailscale)  metric_tailscale ;;
    all)
        echo '{'
        echo '"disk":'$(metric_disk 2>/dev/null || echo null)','
        echo '"mem":'$(metric_mem 2>/dev/null || echo null)','
        echo '"io":'$(metric_io 2>/dev/null || echo null)','
        echo '"cpu_hogs":'$(metric_cpu_hogs 2>/dev/null || echo '[]')','
        echo '"worktree":'$(metric_worktree 2>/dev/null || echo null)','
        echo '"tailscale":'$(metric_tailscale 2>/dev/null || echo null)
        echo '}'
        ;;
    *) echo "Unknown metric: $1" >&2; exit 1 ;;
esac
