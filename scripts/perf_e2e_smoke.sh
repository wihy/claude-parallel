#!/bin/bash
#
# perf_e2e_smoke.sh — iOS 真机 Sampling Profiler 端到端冒烟验证
#
# 用法:
#   ./scripts/perf_e2e_smoke.sh [round] [--device UDID] [--process NAME]
#
# round:
#   1  旁路基础可用性 (只开 sampling, 不开主链路)
#   2  双 xctrace 互斥验证 (Power 主链路 + sampling 旁路)
#   2b 主链路 Time Profiler + sampling (预期: 旁路被跳过)
#   3  业务符号可见性 + 延迟 (交互式, 需手动操作 App)
#   all 依次跑 1 → 2 → 2b (Round 3 需手动)
#
# 示例:
#   ./scripts/perf_e2e_smoke.sh all
#   ./scripts/perf_e2e_smoke.sh 3 --device 00008120-XXXX --process Soul_New

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROCESS="Soul_New"
DEVICE=""
ROUND="${1:-all}"
WAIT_CYCLES=35  # 3 cycles * 10s + export overhead

# Parse args
shift || true
while [[ $# -gt 0 ]]; do
    case $1 in
        --device) DEVICE="$2"; shift 2 ;;
        --process) PROCESS="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Auto-detect device UDID if not provided
if [[ -z "$DEVICE" ]]; then
    DEVICE=$(xcrun xctrace list devices 2>/dev/null \
        | grep -v "^==" | grep -v "^$" | grep -v "Simulator" \
        | head -1 | sed 's/.*(\([^)]*\)).*/\1/' || true)
    if [[ -z "$DEVICE" ]]; then
        echo "ERROR: 未检测到 iOS 真机。请用 --device UDID 指定。"
        exit 1
    fi
    echo "Auto-detected device: $DEVICE"
fi

CPAR="python -m src.cli"
PASS=0
FAIL=0
SKIP=0

# ── Helpers ──

check() {
    local desc="$1"
    local result="$2"
    if [[ "$result" == "PASS" ]]; then
        echo "  [PASS] $desc"
        ((PASS++))
    elif [[ "$result" == "FAIL" ]]; then
        echo "  [FAIL] $desc"
        ((FAIL++))
    else
        echo "  [SKIP] $desc"
        ((SKIP++))
    fi
}

wait_cycles() {
    local secs="${1:-$WAIT_CYCLES}"
    echo "  等待 ${secs}s (约 3 个 cycle)..."
    sleep "$secs"
}

cleanup_tag() {
    local tag="$1"
    $CPAR perf stop --repo "$REPO_DIR" --tag "$tag" 2>/dev/null || true
    # rm -rf "$REPO_DIR/.claude-parallel/perf/$tag"  # 保留供检查
}

# ── Round 1: 旁路基础可用性 ──

round1() {
    local TAG="smoke_r1"
    echo ""
    echo "========================================"
    echo " Round 1: 旁路基础可用性"
    echo "========================================"
    echo ""

    cleanup_tag "$TAG"

    echo "  启动 sampling-only (无主 xctrace)..."
    META=$($CPAR perf start --repo "$REPO_DIR" --tag "$TAG" \
        --device "$DEVICE" --attach "$PROCESS" \
        --templates power \
        --sampling --sampling-interval 10 2>&1)
    echo "$META" | head -5

    # Check 1: sampling.enabled
    SAMPLING_ENABLED=$(echo "$META" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('sampling',{}).get('enabled', False))" 2>/dev/null || echo "False")
    check "sampling.enabled == True" "$([ "$SAMPLING_ENABLED" = "True" ] && echo PASS || echo FAIL)"

    wait_cycles

    # Check 2: hotspots 输出
    HOTSPOTS=$($CPAR perf hotspots --repo "$REPO_DIR" --tag "$TAG" --last 3 --json 2>&1)
    SNAP_COUNT=$(echo "$HOTSPOTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "0")
    check "hotspots 至少 1 个 cycle" "$([ "$SNAP_COUNT" -ge 1 ] 2>/dev/null && echo PASS || echo FAIL)"

    # Check 3: 有符号名 (非纯地址)
    HAS_SYMBOLS=$(echo "$HOTSPOTS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
syms = [e['symbol'] for snap in d for e in snap.get('top',[])]
has = any(not s.startswith('0x') for s in syms)
print('yes' if has else 'no')
" 2>/dev/null || echo "no")
    check "热点列表包含符号名 (非纯地址)" "$([ "$HAS_SYMBOLS" = "yes" ] && echo PASS || echo FAIL)"

    # Check 4: sampling.stderr 无冲突错误
    STDERR_FILE="$REPO_DIR/.claude-parallel/perf/$TAG/logs/sampling.stderr"
    if [[ -f "$STDERR_FILE" ]]; then
        HAS_CONFLICT=$(grep -c "already recording" "$STDERR_FILE" 2>/dev/null || echo "0")
        check "无 'already recording' 错误" "$([ "$HAS_CONFLICT" = "0" ] && echo PASS || echo FAIL)"
    else
        check "无 sampling.stderr (无错误)" "PASS"
    fi

    cleanup_tag "$TAG"

    # Check 5: stop 后无僵尸进程
    ZOMBIE=$(ps aux | grep "xctrace.*$TAG" | grep -v grep | wc -l | tr -d ' ')
    check "stop 后无残留 xctrace 进程" "$([ "$ZOMBIE" = "0" ] && echo PASS || echo FAIL)"

    echo ""
    echo "  Round 1 完成"
}

# ── Round 2: 双 xctrace 互斥验证 ──

round2() {
    local TAG="smoke_r2"
    echo ""
    echo "========================================"
    echo " Round 2: Power 主链路 + sampling 旁路"
    echo "========================================"
    echo ""

    cleanup_tag "$TAG"

    echo "  启动 Power + sampling..."
    META=$($CPAR perf start --repo "$REPO_DIR" --tag "$TAG" \
        --device "$DEVICE" --attach "$PROCESS" \
        --templates power \
        --sampling --sampling-interval 10 2>&1)

    SAMPLING_ENABLED=$(echo "$META" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('sampling',{}).get('enabled', False))" 2>/dev/null || echo "False")
    check "sampling.enabled == True (Power 不冲突)" "$([ "$SAMPLING_ENABLED" = "True" ] && echo PASS || echo FAIL)"

    wait_cycles

    # 主链路 trace 存在
    TRACE_DIR="$REPO_DIR/.claude-parallel/perf/$TAG/traces"
    POWER_TRACE=$(find "$TRACE_DIR" -name "*power*" -type d 2>/dev/null | head -1)
    check "Power trace 文件已生成" "$([ -n "$POWER_TRACE" ] && echo PASS || echo FAIL)"

    # hotspots 正常
    HOTSPOTS=$($CPAR perf hotspots --repo "$REPO_DIR" --tag "$TAG" --last 1 --json 2>&1)
    SNAP_COUNT=$(echo "$HOTSPOTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "0")
    check "sampling hotspots 正常输出" "$([ "$SNAP_COUNT" -ge 1 ] 2>/dev/null && echo PASS || echo FAIL)"

    cleanup_tag "$TAG"
    echo ""
    echo "  Round 2 完成"
}

round2b() {
    local TAG="smoke_r2b"
    echo ""
    echo "========================================"
    echo " Round 2b: Time Profiler 主链路 + sampling (预期跳过)"
    echo "========================================"
    echo ""

    cleanup_tag "$TAG"

    echo "  启动 Time Profiler 主链路 + sampling..."
    META=$($CPAR perf start --repo "$REPO_DIR" --tag "$TAG" \
        --device "$DEVICE" --attach "$PROCESS" \
        --templates time \
        --sampling 2>&1)

    SAMPLING_ENABLED=$(echo "$META" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('sampling',{}).get('enabled', False))" 2>/dev/null || echo "True")
    check "sampling.enabled == False (冲突自动跳过)" "$([ "$SAMPLING_ENABLED" = "False" ] && echo PASS || echo FAIL)"

    SAMPLING_REASON=$(echo "$META" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('sampling',{}).get('reason',''))" 2>/dev/null || echo "")
    check "reason 包含 timeprofiler_conflict" "$(echo "$SAMPLING_REASON" | grep -q "timeprofiler_conflict" && echo PASS || echo FAIL)"

    cleanup_tag "$TAG"
    echo ""
    echo "  Round 2b 完成"
}

# ── Round 3: 业务符号可见性 + 延迟 (交互式) ──

round3() {
    local TAG="smoke_r3"
    echo ""
    echo "========================================"
    echo " Round 3: 业务符号可见性 + 延迟 (交互式)"
    echo "========================================"
    echo ""

    cleanup_tag "$TAG"

    echo "  启动采集..."
    $CPAR perf start --repo "$REPO_DIR" --tag "$TAG" \
        --device "$DEVICE" --attach "$PROCESS" \
        --templates power \
        --sampling --sampling-interval 10 > /dev/null 2>&1

    echo ""
    echo "  采集已启动。请在另一个终端运行:"
    echo ""
    echo "    python -m src.cli perf hotspots --repo $REPO_DIR --tag $TAG --follow"
    echo ""
    echo "  然后在 SoulApp 中依次执行:"
    echo "    A. Kill App → 重新打开 (等 ~20s)"
    echo "    B. 进入群聊/滑动消息列表 (等 ~20s)"
    echo "    C. 停在首页不操作 (等 ~20s)"
    echo ""
    read -r -p "  完成以上操作后按 Enter 继续..."

    echo ""
    echo "  查看全会话聚合..."
    $CPAR perf hotspots --repo "$REPO_DIR" --tag "$TAG" --aggregate
    echo ""

    echo "  最后 3 个 cycle (应能区分忙/闲)..."
    $CPAR perf hotspots --repo "$REPO_DIR" --tag "$TAG" --last 3
    echo ""

    # 检查业务符号
    HOTSPOTS=$($CPAR perf hotspots --repo "$REPO_DIR" --tag "$TAG" --aggregate --json 2>&1)
    HAS_BUSINESS=$(echo "$HOTSPOTS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
syms = [e['symbol'] for snap in d for e in snap.get('top',[])]
has = any('SO' in s or 'Soul' in s or 'soul' in s.lower() for s in syms)
print('yes' if has else 'no')
" 2>/dev/null || echo "no")
    check "聚合 Top 中包含业务符号 (SO/Soul 前缀)" "$([ "$HAS_BUSINESS" = "yes" ] && echo PASS || echo FAIL)"

    echo ""
    read -r -p "  请回答: --follow 是否每 ~12s 刷新? (y/n) " FOLLOW_OK
    check "--follow 每 ~12s 刷新" "$([ "$FOLLOW_OK" = "y" ] && echo PASS || echo FAIL)"

    read -r -p "  请回答: 操作 App 后 ~15-20s 内看到热点变化? (y/n) " LATENCY_OK
    check "延迟 ~15-20s 内可见" "$([ "$LATENCY_OK" = "y" ] && echo PASS || echo FAIL)"

    read -r -p "  请回答: 场景 C (空闲) 采样数明显低于场景 B (忙)? (y/n) " BUSY_IDLE_OK
    check "忙/闲可区分" "$([ "$BUSY_IDLE_OK" = "y" ] && echo PASS || echo FAIL)"

    cleanup_tag "$TAG"
    echo ""
    echo "  Round 3 完成"
}

# ── Summary ──

summary() {
    echo ""
    echo "========================================"
    echo " 验证结果汇总"
    echo "========================================"
    echo "  PASS: $PASS"
    echo "  FAIL: $FAIL"
    echo "  SKIP: $SKIP"
    echo "========================================"
    if [[ $FAIL -gt 0 ]]; then
        echo "  结果: 有失败项, 请检查上方 [FAIL] 条目"
        exit 1
    else
        echo "  结果: 全部通过"
        exit 0
    fi
}

# ── Main ──

cd "$REPO_DIR"

case "$ROUND" in
    1)   round1; summary ;;
    2)   round2; summary ;;
    2b)  round2b; summary ;;
    3)   round3; summary ;;
    all) round1; round2; round2b; summary ;;
    *)   echo "用法: $0 [1|2|2b|3|all] [--device UDID] [--process NAME]"; exit 1 ;;
esac
