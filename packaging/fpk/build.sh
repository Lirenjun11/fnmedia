#!/usr/bin/env bash
set -euo pipefail

FPK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${FPK_DIR}/../.." && pwd)"
VERSION="$(head -n1 "${REPO_ROOT}/VERSION" | tr -d '[:space:]')"
[ -n "${VERSION}" ] || { echo "ERROR: 无法读取 VERSION" >&2; exit 1; }

FNPACK_BIN="${FNPACK:-}"
STAGE="${FPK_DIR}/.build/fpk"
STAGE_ONLY=0
OUT_DIR="${REPO_ROOT}/dist"
SLIM=0

while [ $# -gt 0 ]; do
    case "$1" in
        --slim) SLIM=1; shift ;;
        --stage-only) STAGE_ONLY=1; STAGE="$2"; shift 2 ;;
        --fnpack) FNPACK_BIN="$2"; shift 2 ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

command -v rsync >/dev/null 2>&1 || { echo "ERROR: 需要 rsync" >&2; exit 1; }
[ -f "${REPO_ROOT}/proxy/static/index.html" ] || {
    echo "ERROR: 缺少前端产物，请先在 web/ 执行 npm install && npm run build" >&2; exit 1; }

rm -rf "${STAGE}"
mkdir -p "${STAGE}/app/repo" "${STAGE}/app/ui"

EXCLUDES=(
    --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc'
    --exclude='.git/' --exclude='.tools/' --exclude='dist/'
    --exclude='state/' --exclude='packaging/fpk/.build/'
    --exclude='node_modules/' --exclude='web/src/' --exclude='web/dist/'
    --exclude='web/.vite/' --exclude='tests/' --exclude='*.fpk' --exclude='.DS_Store'
)
[ "${SLIM}" -eq 1 ] && EXCLUDES+=(--exclude='wheelhouse/')

rsync -a --delete "${EXCLUDES[@]}" "${REPO_ROOT}/" "${STAGE}/app/repo/"

cp -a "${FPK_DIR}/app/ui/." "${STAGE}/app/ui/"
cp -a "${FPK_DIR}/cmd" "${STAGE}/cmd"
cp -a "${FPK_DIR}/config" "${STAGE}/config"
cp -a "${FPK_DIR}/wizard" "${STAGE}/wizard"
cp "${FPK_DIR}/ICON.PNG" "${FPK_DIR}/ICON_256.PNG" "${STAGE}/"
sed "s/@VERSION@/${VERSION}/" "${FPK_DIR}/manifest.in" > "${STAGE}/manifest"
chmod +x "${STAGE}"/cmd/*

echo "[fpk] 骨架组装完成: ${STAGE} (version=${VERSION}, slim=${SLIM})"
[ "${STAGE_ONLY}" -eq 1 ] && exit 0

if [ -z "${FNPACK_BIN}" ]; then
    if command -v fnpack >/dev/null 2>&1; then
        FNPACK_BIN="$(command -v fnpack)"
    elif [ -x "${REPO_ROOT}/.tools/fnpack" ]; then
        FNPACK_BIN="${REPO_ROOT}/.tools/fnpack"
    else
        echo "ERROR: 未找到 fnpack，请用 --fnpack 指定路径" >&2
        exit 1
    fi
fi
[ -x "${FNPACK_BIN}" ] || { echo "ERROR: fnpack 不可执行: ${FNPACK_BIN}" >&2; exit 1; }
FNPACK_BIN="$(cd "$(dirname "${FNPACK_BIN}")" && pwd)/$(basename "${FNPACK_BIN}")"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
( cd "${WORK}" && "${FNPACK_BIN}" build --directory "${STAGE}" )
BUILT="$(find "${WORK}" "${STAGE}" -maxdepth 2 -name '*.fpk' -print -quit 2>/dev/null || true)"
[ -n "${BUILT}" ] || { echo "ERROR: fnpack 未产出 .fpk" >&2; exit 1; }

mkdir -p "${OUT_DIR}"
SUFFIX=""
[ "${SLIM}" -eq 1 ] && SUFFIX="-slim"
FINAL="${OUT_DIR}/fnmedia-ext-${VERSION}${SUFFIX}.fpk"
mv "${BUILT}" "${FINAL}"
python3 - "${FINAL}" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
(p.parent / (p.name + ".sha256")).write_text(
    hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n", encoding="utf-8")
PY
echo "[fpk] 打包完成: ${FINAL}"
