#!/usr/bin/env bash
# ══════════════════════════════════════════════════
# ACRE Quick Start Script
# Run: chmod +x scripts/setup.sh && ./scripts/setup.sh
# ══════════════════════════════════════════════════
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo ""
echo "  █████╗  ██████╗██████╗ ███████╗"
echo " ██╔══██╗██╔════╝██╔══██╗██╔════╝"
echo " ███████║██║     ██████╔╝█████╗  "
echo " ██╔══██║██║     ██╔══██╗██╔══╝  "
echo " ██║  ██║╚██████╗██║  ██║███████╗"
echo " ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝"
echo "  Autonomous Codebase Reliability Engineer"
echo ""

# ── Check prerequisites ───────────────────────────────────────────────────────
info "Checking prerequisites..."

command -v docker     >/dev/null 2>&1 || error "Docker not found. Install Docker Desktop."
command -v python3    >/dev/null 2>&1 || error "Python 3 not found."
command -v node       >/dev/null 2>&1 || error "Node.js not found."
command -v git        >/dev/null 2>&1 || error "Git not found."

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
NODE_VERSION=$(node --version | cut -c2-)
info "Python $PYTHON_VERSION, Node $NODE_VERSION"

success "Prerequisites satisfied"

# ── Environment ───────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  cp .env.example .env
  warn ".env created from template. Fill in OPENAI_API_KEY and GITHUB_TOKEN before running!"
  echo ""
  echo "  Required minimum:"
  echo "    OPENAI_API_KEY=sk-..."
  echo "    GITHUB_TOKEN=ghp_..."
  echo ""
  read -p "Press Enter after editing .env to continue (Ctrl+C to abort)..." _
fi

source .env

if [ -z "$OPENAI_API_KEY" ]; then
  error "OPENAI_API_KEY is not set in .env"
fi
success "Environment loaded"

# ── Python virtual environments ───────────────────────────────────────────────
info "Setting up Python environments..."

for service in ingestion agents api finetuning; do
  if [ ! -d "services/$service/.venv" ]; then
    python3 -m venv "services/$service/.venv"
    "services/$service/.venv/bin/pip" install --quiet -r "services/$service/requirements.txt" \
      || warn "Some dependencies for $service may have failed"
    success "  $service venv ready"
  else
    info "  $service venv already exists, skipping"
  fi
done

# ── Node / Dashboard ──────────────────────────────────────────────────────────
info "Installing dashboard dependencies..."
(cd dashboard && npm ci --silent)
success "Dashboard dependencies installed"

# ── Docker Compose ────────────────────────────────────────────────────────────
info "Starting infrastructure (Postgres, Redis, ChromaDB, MLflow)..."
docker compose up -d postgres redis chromadb mlflow

info "Waiting for Postgres to be ready..."
for i in {1..30}; do
  docker compose exec -T postgres pg_isready -U acre >/dev/null 2>&1 && break
  sleep 1
done
success "Postgres ready"

# ── Seed fine-tuning data ─────────────────────────────────────────────────────
if [ ! -d "services/finetuning/training_data" ]; then
  info "Setting up fine-tuning training data directory..."
  mkdir -p services/finetuning/training_data
  cat > services/finetuning/training_data/sample.jsonl << 'SEED'
{"bug_title":"Null pointer dereference","severity":"HIGH","bug_type":"logic","vulnerable_code":"def process(data):\n    return data['key'].strip()","fixed_code":"def process(data):\n    if data and 'key' in data and data['key']:\n        return data['key'].strip()\n    return ''"}
{"bug_title":"SQL injection","severity":"CRITICAL","bug_type":"security","vulnerable_code":"query = f\"SELECT * FROM users WHERE name = '{name}'\"","fixed_code":"query = \"SELECT * FROM users WHERE name = %s\"\ncursor.execute(query, (name,))"}
{"bug_title":"Uncaught exception swallowed","severity":"MEDIUM","bug_type":"reliability","vulnerable_code":"try:\n    result = risky_op()\nexcept:\n    pass","fixed_code":"try:\n    result = risky_op()\nexcept Exception as e:\n    logger.error(f'risky_op failed: {e}')\n    raise"}
SEED
  success "Seed training data created"
fi

# ── Start all services ────────────────────────────────────────────────────────
info "Starting all ACRE services..."
docker compose up -d

sleep 5

# ── Health checks ─────────────────────────────────────────────────────────────
info "Running health checks..."

check_service() {
  local name=$1; local url=$2
  if curl -sf "$url" >/dev/null 2>&1; then
    success "  $name: $url"
  else
    warn "  $name: $url — not ready yet (may still be starting)"
  fi
}

check_service "API"      "http://localhost:8000/health"
check_service "GraphQL"  "http://localhost:8000/graphql"
check_service "MLflow"   "http://localhost:5000"
check_service "Dashboard" "http://localhost:3000"

echo ""
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}  ACRE is running!${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""
echo "  API:       http://localhost:8000"
echo "  API Docs:  http://localhost:8000/docs"
echo "  GraphQL:   http://localhost:8000/graphql"
echo "  Dashboard: http://localhost:3000"
echo "  MLflow:    http://localhost:5000"
echo "  Grafana:   http://localhost:3001  (admin / admin)"
echo ""
echo "  Quick test:"
echo "  curl -X POST http://localhost:8000/api/v1/analyses \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"repo_url\": \"https://github.com/psf/requests\"}'"
echo ""
