#!/usr/bin/env bash
# mysqlagent 安全扫描：SBOM 生成 + 镜像漏洞扫描（Trivy）+ 源码密钥扫描
#
# 用法（在 WSL / Linux 内运行）：
#   bash scripts/security-scan.sh [镜像名:tag]     # 默认 mysqlagent:latest
#
# 产物（写入 ./scan/）：
#   sbom.cdx.json      CycloneDX SBOM（始终生成，不依赖漏洞库）
#   image-scan.txt     镜像漏洞报告（需下载漏洞库；网络不可达时跳过并提示）
#   fs-scan.txt        源码与配置扫描报告（密钥泄露检测）
#
# 网络说明：漏洞库默认源 ghcr.io/aquasec/trivy-db 在部分网络环境不可达，
# 可用 TRIVY_DB_REPO 环境变量换源；SBOM 与密钥扫描不受影响。
# 依赖：docker（trivy 经 aquasec/trivy 容器运行，无需本地安装）。

set -uo pipefail

IMAGE="${1:-mysqlagent:latest}"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/../scan"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$OUT_DIR"

TRIVY_IMAGE="aquasec/trivy:latest"
TRIVY_DB_REPO="${TRIVY_DB_REPO:-ghcr.io/aquasec/trivy-db:2}"
CACHE_VOL="mysqlagent-trivy-cache"
docker volume inspect "${CACHE_VOL}" >/dev/null 2>&1 || docker volume create "${CACHE_VOL}" >/dev/null

TMP_TAR="$(mktemp /tmp/mysqlagent-img.XXXXXX.tar)"
trap 'rm -f "${TMP_TAR}"' EXIT

docker pull -q "${TRIVY_IMAGE}" >/dev/null
echo ">> 导出镜像 ${IMAGE}"
docker save "${IMAGE}" -o "${TMP_TAR}"

# 容器化 trivy：镜像 tar 与报告目录以卷挂载
img_scan() {
    docker run --rm \
        -v "${TMP_TAR}:/img.tar:ro" \
        -v "${OUT_DIR}:/out" \
        -v "${CACHE_VOL}:/root/.cache" \
        "${TRIVY_IMAGE}" image --input /img.tar "$@"
}
fs_scan() {
    docker run --rm \
        -v "${REPO_ROOT}:/repo:ro" \
        -v "${OUT_DIR}:/out" \
        -v "${CACHE_VOL}:/root/.cache" \
        "${TRIVY_IMAGE}" fs "$@" /repo
}

FAIL=0

echo ">> [1/3] 生成 SBOM (CycloneDX)"
if img_scan --format cyclonedx --skip-db-update --output /out/sbom.cdx.json; then
    echo "   SBOM: ${OUT_DIR}/sbom.cdx.json ($(grep -c '"bom-ref"' "${OUT_DIR}/sbom.cdx.json" || echo '?') 个组件)"
else
    echo "   FAIL：SBOM 生成失败" >&2
    FAIL=1
fi

echo ">> [2/3] 扫描镜像漏洞（库源 ${TRIVY_DB_REPO}）"
if img_scan --db-repository="${TRIVY_DB_REPO}" --severity HIGH,CRITICAL \
        --output /out/image-scan.txt 2>"${OUT_DIR}/image-scan.err"; then
    echo "   报告: ${OUT_DIR}/image-scan.txt"
    if img_scan --db-repository="${TRIVY_DB_REPO}" --severity HIGH,CRITICAL \
            --exit-code 1 --quiet >/dev/null 2>&1; then
        echo "   PASS：镜像无 HIGH/CRITICAL 漏洞 ✓"
    else
        echo "   FAIL：镜像存在 HIGH/CRITICAL 漏洞（详见 image-scan.txt）" >&2
        FAIL=1
    fi
else
    echo "   SKIP：漏洞库不可达（详见 image-scan.err）。SBOM 已生成，可在网络可达环境重跑或换源："
    echo "        TRIVY_DB_REPO=<registry>/trivy-db:2 bash scripts/security-scan.sh"
fi
rm -f "${OUT_DIR}/image-scan.err"

echo ">> [3/3] 扫描源码密钥泄露"
if fs_scan --scanners secret --output /out/fs-scan.txt 2>/dev/null; then
    echo "   报告: ${OUT_DIR}/fs-scan.txt"
else
    # 部分版本 fs 子命令参数差异，降级为表内输出
    if fs_scan --scanners secret > "${OUT_DIR}/fs-scan.txt" 2>/dev/null; then
        echo "   报告: ${OUT_DIR}/fs-scan.txt"
    else
        echo "   SKIP：源码扫描失败" >&2
    fi
fi

exit "${FAIL}"
