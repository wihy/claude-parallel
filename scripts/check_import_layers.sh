#!/usr/bin/env bash
# 分层 import 方向静态守护 — CI 用。
#
# 规则 (单向依赖):
#   app → application → domain ← infrastructure
#                  ↓
#   perf → domain + infrastructure (不可反向依赖 application/app)
#
# 任一规则违反则退出码 1, block PR。

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

fail=0

check() {
  local name="$1"
  local pattern="$2"
  local path="$3"
  local hits
  hits=$(grep -rn "$pattern" "$path" 2>/dev/null | grep -v __pycache__ || true)
  if [ -n "$hits" ]; then
    echo -e "${RED}✗ 违反规则: $name${NC}"
    echo "$hits"
    fail=1
  else
    echo -e "${GREEN}✓ $name${NC}"
  fi
}

echo "=== 分层 import 方向检查 ==="
echo ""

check "1. domain/ 不 import app/application/infrastructure/perf" \
  "^from src\.\(app\|application\|infrastructure\|perf\)\|^from \.\.\(app\|application\|infrastructure\|perf\)" \
  "src/domain/"

check "2. infrastructure/ 不 import application" \
  "^from src\.application\|^from \.\.application" \
  "src/infrastructure/"

check "3. infrastructure/ 不 import app" \
  "^from src\.app\.\|^from \.\.app\." \
  "src/infrastructure/"

check "4. perf/ 不 import application" \
  "^from src\.application\|^from \.\.application" \
  "src/perf/"

check "5. perf/ 不 import app" \
  "^from src\.app\.\|^from \.\.app\." \
  "src/perf/"

check "6. application/ 不 import app" \
  "^from src\.app\.\|^from \.\.app\." \
  "src/application/"

echo ""
if [ $fail -eq 1 ]; then
  echo -e "${RED}✗ 分层规则检查失败${NC}"
  exit 1
else
  echo -e "${GREEN}✓ 所有分层规则通过${NC}"
fi
