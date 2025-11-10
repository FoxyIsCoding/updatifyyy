#!/usr/bin/env bash
#
# illogical-updots simple installer
#
# Clones (or updates) the repository into:  ~/.cache/illogical-updots
# Installs a user-level launcher (.desktop + icon) under ~/.local/share
# Then optionally launches the application.
#
#
# Usage:
#   ./install.sh                # install/update + launcher + run
#   ./install.sh --no-run       # install/update + launcher only
#   ./install.sh --force-clone  # discard existing clone and re-clone
#
#
# Exit codes:
#   0 success
#   1 argument error
#   2 missing dependency
#   3 clone/update failure
#   4 launcher install failure
#   5 run failure
#

set -euo pipefail

REPO_URL="https://github.com/FoxyIsCoding/illogical-updots.git"
CACHE_DIR="${HOME}/.cache/illogical-updots"
PYTHON="${PYTHON:-python3}"
NO_RUN="false"
FORCE_CLONE="false"

while (( $# )); do
  case "$1" in
    --no-run) NO_RUN="true" ;;
    --force-clone) FORCE_CLONE="true" ;;
    -h|--help)
      cat <<EOF
illogical-updots installer

Installs to: ${CACHE_DIR}
Repository: ${REPO_URL}

Options:
  --no-run       Do not launch the app after installation.
  --force-clone  Remove existing clone and re-clone fresh.
  -h, --help     Show this help.

Environment:
  PYTHON         Python interpreter (default: python3)

EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
  shift
done

command -v git >/dev/null 2>&1 || { echo "git not found" >&2; exit 2; }
command -v "${PYTHON}" >/dev/null 2>&1 || { echo "Python '${PYTHON}' not found" >&2; exit 2; }

echo "==> Target directory: ${CACHE_DIR}"
echo "==> Repository: ${REPO_URL}"

if [ -d "${CACHE_DIR}" ] && [ -d "${CACHE_DIR}/.git" ]; then
  if [ "${FORCE_CLONE}" = "true" ]; then
    echo "==> Removing existing clone (force)..."
    rm -rf "${CACHE_DIR}"
  else
    echo "==> Updating existing clone..."
    (
      cd "${CACHE_DIR}"
      git fetch --all --prune || { echo "git fetch failed" >&2; exit 3; }
      git pull --rebase || { echo "git pull failed" >&2; exit 3; }
    )
  fi
fi

if [ ! -d "${CACHE_DIR}/.git" ]; then
  echo "==> Cloning repository..."
  git clone --depth=1 "${REPO_URL}" "${CACHE_DIR}" || { echo "git clone failed" >&2; exit 3; }
fi

# Install icon & .desktop
APPS_DIR="${HOME}/.local/share/applications"
ICONS_DIR="${HOME}/.local/share/icons/hicolor/256x256/apps"
mkdir -p "${APPS_DIR}" "${ICONS_DIR}"

ICON_SRC="${CACHE_DIR}/.github/assets/logo.png"
ICON_DEST="${ICONS_DIR}/illogical-updots.png"
if [ -f "${ICON_SRC}" ]; then
  cp -f "${ICON_SRC}" "${ICON_DEST}" || echo "Warning: failed to copy icon"
else
  echo "Warning: icon not found at ${ICON_SRC}"
fi

DESKTOP_FILE="${APPS_DIR}/illogical-updots.desktop"
cat > "${DESKTOP_FILE}" <<EOF
[Desktop Entry]
Type=Application
Name=illogical-updots
Comment=Git updates & console installer for your dotfiles
Exec=${PYTHON} ${CACHE_DIR}/app.py
Icon=illogical-updots
Terminal=false
Categories=Utility;System;
StartupWMClass=illogical-updots
X-GNOME-UsesNotifications=true
EOF

if [ ! -f "${DESKTOP_FILE}" ]; then
  echo "Failed to write desktop file" >&2
  exit 4
fi

# Best-effort cache updates (non-fatal)
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -q "${HOME}/.local/share/icons" || true
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "${APPS_DIR}" || true

echo "==> Launcher installed: ${DESKTOP_FILE}"
echo "==> Icon installed: ${ICON_DEST}"

if [ "${NO_RUN}" = "true" ]; then
  echo "==> Installation complete. Launch skipped (--no-run)."
  echo "To run later: ${PYTHON} ${CACHE_DIR}/app.py"
  exit 0
fi

echo "==> Launching illogical-updots..."
set +e
"${PYTHON}" "${CACHE_DIR}/app.py"
RC=$?
set -e
if [ $RC -ne 0 ]; then
  echo "Application failed with exit code ${RC}" >&2
  exit 5
fi

exit 0
