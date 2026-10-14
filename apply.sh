#!/usr/bin/env bash
#
# apply.sh — thin wrapper around patch_speedhack.py.
#
# Usage:
#   ./apply.sh --check  [/path/to/flycast-vita]
#   ./apply.sh --apply  [/path/to/flycast-vita]
#   ./apply.sh --revert [/path/to/flycast-vita]
#
# If no root is given, the current directory is assumed.

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:---check}"
shift || true
ROOT="${1:-.}"

case "$MODE" in
    --check|--apply|--revert) ;;
    *)
        echo "Usage: $0 [--check|--apply|--revert] [root]" >&2
        exit 1
        ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required." >&2
    exit 1
fi

# Always survey anchors first on a check, so failures are easy to read.
if [ "$MODE" = "--check" ]; then
    "$HERE/locate_anchors.sh" "$ROOT" || true
    echo
fi

python3 "$HERE/patch_speedhack.py" --root "$ROOT" "$MODE"
RC=$?

if [ "$MODE" = "--apply" ] && [ "$RC" -eq 0 ]; then
    echo
    echo "Applied. Next steps:"
    echo "  1. Add core/hw/pvr/speedhack.cpp to your build if CMake does not glob."
    echo "  2. Wire the weak serial hook (see README, 'Wiring the game serial')."
    echo "  3. Apply the manual hooks (see README, 'Manual hooks')."
    echo "  4. Build, then:  FLYCAST_VITA_LOG=1 ./flycast"
    echo
    echo "Revert with: $0 --revert $ROOT"
fi

exit $RC
