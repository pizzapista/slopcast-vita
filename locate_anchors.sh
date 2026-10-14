#!/usr/bin/env bash
#
# locate_anchors.sh — verify every anchor the patcher depends on exists in
# YOUR tree, and show the exact line number.
#
# Run this from the root of your slopcast-vita checkout, or pass the root:
#     ./locate_anchors.sh [/path/to/slopcast-vita]
#
# It writes nothing. It only reads.

set -u

ROOT="${1:-.}"
ROOT="$(cd "$ROOT" && pwd)"

if [ ! -d "$ROOT/core/hw/pvr" ]; then
    echo "ERROR: $ROOT does not look like a flycast-derived checkout."
    echo "       Expected core/hw/pvr/ underneath it."
    exit 1
fi

cd "$ROOT" || exit 1

FOUND=0
MISSING=0

# report <label> <file> <regex>
report() {
    local label="$1" file="$2" rx="$3"
    if [ ! -f "$file" ]; then
        printf '  %-28s MISSING FILE   %s\n' "$label" "$file"
        MISSING=$((MISSING + 1))
        return
    fi
    local hits
    hits=$(grep -nE "$rx" "$file" 2>/dev/null | head -3)
    if [ -z "$hits" ]; then
        printf '  %-28s NOT FOUND      %s\n' "$label" "$file"
        MISSING=$((MISSING + 1))
    else
        printf '  %-28s OK             %s\n' "$label" "$file"
        echo "$hits" | sed 's/^/        /'
        FOUND=$((FOUND + 1))
    fi
}

echo "Anchor survey for: $ROOT"
echo
echo "REQUIRED — the patcher will refuse to report success without these"
echo "-----------------------------------------------------------------"
report "UpdateFPSCR"    core/hw/sh4/dynarec/sh4_fpu.cpp \
       '^[[:space:]]*(static[[:space:]]+)?(void|bool|int|u32)[[:space:]]+UpdateFPSCR[[:space:]]*\('
report "SetFPSCR"       core/hw/sh4/dynarec/sh4_fpu.cpp \
       '^[[:space:]]*(static[[:space:]]+)?(void|bool|int|u32)[[:space:]]+SetFPSCR[[:space:]]*\('
report "RuntimeBlockCheck" core/hw/sh4/dynarec/dynarec.cpp \
       '^[[:space:]]*(static[[:space:]]+)?bool[[:space:]]+RuntimeBlockCheck[[:space:]]*\('
report "dsp_step"       core/hw/aica/dsp.cpp \
       '^[[:space:]]*(static[[:space:]]+)?void[[:space:]]+dsp_step[[:space:]]*\('
report "AICA_Sample"    core/hw/aica/aica.cpp \
       '^[[:space:]]*(static[[:space:]]+)?void[[:space:]]+AICA_Sample[[:space:]]*\('
report "renderer->draw" core/hw/pvr/rend.cpp \
       'renderer->draw[[:space:]]*\('

echo
echo "OPTIONAL — the module degrades gracefully without these"
echo "-------------------------------------------------------"
report "dsp_writenmem"   core/hw/aica/dsp.cpp \
       '^[[:space:]]*(static[[:space:]]+)?void[[:space:]]+dsp_writenmem[[:space:]]*\('
report "UpdateArm7"      core/hw/aica/aica.cpp \
       '^[[:space:]]*(static[[:space:]]+)?void[[:space:]]+UpdateArm7[[:space:]]*\('
report "pvr_WriteReg"    core/hw/pvr/pvr_regs.cpp \
       '^[[:space:]]*(static[[:space:]]+)?void[[:space:]]+pvr_WriteReg[[:space:]]*\('
report "PerStripRendering" core/hw/pvr/ta.cpp \
       'PerStripRendering'
report "EmulateFramebuffer" core/rend/gles/gles.cpp \
       'EmulateFramebuffer|renderFramebuffer'
report "renderBusy"      core/hw/pvr/rend.cpp \
       'renderBusy|renderDone'

echo
echo "MANUAL HOOKS — verify these symbols for the hand-wired snippets"
echo "---------------------------------------------------------------"
report "FPSCR_PR"         core/hw/sh4/sh4_if.h \
       'FPSCR_PR'
report "modifierVolumes"   core/rend/rendtypes.h \
       'modifierVolumes'
report "GD-ROM serial"     core/hw/gdrom/gdrom.cpp \
       'GameId|game_id|GetGameId'

echo
echo "GAME SERIAL — where your fork exposes it (define the weak hook there)"
echo "---------------------------------------------------------------------"
grep -rn "GameId\|game_id\|GetGameId" core/cfg/ core/hw/gdrom/ 2>/dev/null \
    | head -8 | sed 's/^/  /' || echo "  (no match — search the tree by hand)"

echo
echo "BUILD SYSTEM — does it glob, or must you list speedhack.cpp?"
echo "------------------------------------------------------------"
if [ -f CMakeLists.txt ]; then
    grep -nE 'GLOB|target_sources|add_executable' CMakeLists.txt \
        | head -10 | sed 's/^/  /'
else
    echo "  (no CMakeLists.txt at root)"
fi

echo
echo "Summary: $FOUND anchor(s) located, $MISSING missing."
if [ "$MISSING" -gt 0 ]; then
    echo "Missing REQUIRED anchors will be reported by the patcher. Missing"
    echo "OPTIONAL anchors are fine — the module handles their absence."
fi
exit 0
