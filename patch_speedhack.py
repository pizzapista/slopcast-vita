#!/usr/bin/env python3
"""
patch_speedhack.py — anchor-based surgical patcher for the slopcast-vita
speed-hack layer.

WHY NOT PLAIN .patch FILES FOR MODIFIED SOURCES
-----------------------------------------------
You are tracking a fork. A unified diff whose context lines were written
without reading your exact tree at the base commit fails to apply, and it
fails in the least useful way possible: a wall of rejected hunks with no
indication of which hook was actually important.

This applier instead locates each hook by a SYMBOL — a function definition or
a call site — and inserts the hack block next to it. That survives line drift,
comment changes, and reordering elsewhere in the file. Every insertion is
wrapped in BEGIN/END markers so it can be removed exactly.

Guarantees:
  * Idempotent. Running --apply twice inserts nothing the second time.
  * Revertible. --revert removes every marked block byte-for-byte.
  * Backed up. Every modified file is copied to <file>.speedhack.bak once.
  * Honest. Anchors that cannot be found are reported, not guessed at.

Usage:
  patch_speedhack.py --root /path/to/slopcast-vita --check
  patch_speedhack.py --root /path/to/slopcast-vita --apply
  patch_speedhack.py --root /path/to/slopcast-vita --revert

Exit codes:
  0  everything requested succeeded
  1  fatal (bad --root, unreadable tree)
  2  one or more anchors were not found (partial apply)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

# ---------------------------------------------------------------------------
# Marker format. Every inserted block is wrapped in these two lines so the
# reverter can find it without pattern-matching the block body.
#
# The marker says "speedhack" because that is the module name; see README.md,
# "A note on naming".
# ---------------------------------------------------------------------------
def _mark_begin(hid: str) -> str:
    return "// [speedhack] BEGIN " + hid

def _mark_end(hid: str) -> str:
    return "// [speedhack] END " + hid

# ---------------------------------------------------------------------------
# Inserted blocks.
#
# All of these are top-of-function or before-call-site insertions. They are
# written with tab indentation to match flycast's source style.
# ---------------------------------------------------------------------------

BLOCK_FPSCR_UPDATE = """\
	// [speedhack] BEGIN sh4-fpscr-update
	// Only PR (precision, bit 19), SZ (transfer size, bit 20) and FR (register
	// bank, bit 21) change host FP behaviour. The rounding-mode bits and the
	// cause/enable/flag bookkeeping are guest-visible but do not require a
	// host VFP state change. Cache the host-relevant subset and early-out.
	if (speedhack::fastFpscr())
	{
		extern unsigned int speedhackFpscrHostBits();
		static unsigned int s_lastHostBits = 0xFFFFFFFFu;
		const unsigned int hostBits = speedhackFpscrHostBits();
		if (hostBits == s_lastHostBits)
			return;
		s_lastHostBits = hostBits;
	}
	// [speedhack] END sh4-fpscr-update
"""

BLOCK_FPSCR_SET = """\
	// [speedhack] BEGIN sh4-fpscr-set
	// Force single precision + single bank. Any title that genuinely used
	// double maths will render slightly wrong but will not crash, because the
	// FR/DR aliasing bookkeeping is simply never entered. This is the highest
	// leverage dynarec hack for SA and VT.
	if (speedhack::singlePrecisionFPU())
		value &= ~(FPSCR_PR | FPSCR_FR | FPSCR_SZ);
	// [speedhack] END sh4-fpscr-set
"""

BLOCK_SMC = """\
	// [speedhack] BEGIN sh4-smc
	// RuntimeBlockCheck() runs at the head of every recompiled block and is
	// one of the hottest host-code paths in the emulator. Returning the
	// "unchanged" value keeps the block cache warm forever.
	//
	// Commercial Dreamcast titles almost never self-modify game code; the
	// technique is mostly homebrew and a few copy-protection stubs. This is
	// safe for the three titles in scope. It is NOT safe in general.
	//
	// POLARITY: this assumes false == "code unchanged, proceed". If your
	// revision inverted the sense, flip it — the emulator will crash loudly
	// within seconds either way, so you will know immediately.
	if (speedhack::skipSmcCheck())
		return false;
	// [speedhack] END sh4-smc
"""

BLOCK_AICA_DSP_STEP = """\
	// [speedhack] BEGIN aica-dsp-step
	if (!speedhack::aicaDSP())
	{
		// Skipping the DSP removes the whole FIR/effect pipeline. But the
		// guest polls the DSP's registers to sequence its audio driver, so
		// simply returning can hang the game at a loading screen.
		//
		// Keep the handshake alive, just do not compute: clear the request
		// flag and publish zeros into the effect output registers.
		ADST  = 0;
		MPRO  = 0;
		MADRS = 0;
		// The flag the guest waits on for "DSP finished this block":
		if (dsp.inputRequestPending) dsp.inputRequestPending = false;
		return;
	}
	// [speedhack] END aica-dsp-step
"""

BLOCK_AICA_DSP_WRITE = """\
	// [speedhack] BEGIN aica-dsp-write
	if (!speedhack::aicaDSP())
		return;   // swallow the program upload; nothing will consume it
	// [speedhack] END aica-dsp-write
"""

BLOCK_AICA_SAMPLE = """\
	// [speedhack] BEGIN aica-sample-div
	// Generate fewer samples and let the output stage's interpolation stretch
	// them. Halves or quarters both the ARM7 mixing cost and the ring-buffer
	// bandwidth into sceAudioOut.
	//
	// div 2 is essentially inaudible on the Vita's speakers and on earbuds
	// for these titles. div 4 is audible on cymbals and high-frequency synth
	// — use it only if you are desperate, and NEVER for Virtua Tennis, where
	// ball-hit transients become mushy and that is a gameplay feel regression.
	{
		const int div = speedhack::aicaSampleDiv();
		if (div > 1)
			samples /= (unsigned int)div;
	}
	// [speedhack] END aica-sample-div
"""

BLOCK_AICA_ARM7 = """\
	// [speedhack] BEGIN aica-arm7-alt
	// Run the ARM7 on alternating ticks and double its slice. The guest sees
	// a slightly jittery timer; sequencing stays in sync because the sample
	// counter is what actually drives playback.
	//
	// The doubling must be unconditional on the ticks that DO run, or the
	// sample count drifts and music desyncs from the level.
	if (speedhack::aicaArm7Alt())
	{
		static unsigned int s_tick = 0;
		if ((s_tick++ & 1u) == 0u)
			return;
		samples *= 2u;
	}
	// [speedhack] END aica-arm7-alt
"""

BLOCK_PVR_DRAW = """\
	// [speedhack] BEGIN pvr-draw
	// NOTE: this block references the local RenderType in scope. On upstream
	// @ 446f887 that local is named `rt`. If your fork renamed it, the
	// compiler will flag this line and the fix is a one-word rename.
	if (speedhack::skipModVolumes())
	{
		// Dropping modifier volumes removes an entire stencil pass. What you
		// lose depends on the title:
		//   Sonic Adventure  — character/robot shadows vanish, and geometry
		//                      that relied on modvol clipping pokes through
		//                      walls.
		//   Crazy Taxi       — the fake AO blobs under cars disappear.
		//   Virtua Tennis    — ball shadow gone (gameplay-relevant, which is
		//                      why its profile uses the Speed preset).
		//
		// What you gain: one full vertex-shader pass over every modvol-covered
		// triangle, plus the stencil clear and the pipeline state churn.
		rt.modifierVolumes.clear();
	}

	if (speedhack::skipShadowPass())
	{
		// Translucent geometry is often drawn twice: once depth-only to seed
		// the depth buffer, then again blended. The pre-pass exists purely to
		// fix sorting artifacts.
		//
		// On a tile-based SGX the depth pre-pass is cheap in bandwidth (tiles
		// stay resident) but expensive in draw calls. Measure before keeping
		// this on — for Crazy Taxi it is usually a wash, for SA it is a win.
		rt.opaque.insert(rt.opaque.end(),
		                 rt.punchThrough.begin(), rt.punchThrough.end());
		rt.punchThrough.clear();
	}
	// [speedhack] END pvr-draw
"""

# ---------------------------------------------------------------------------
# Patch specifications.
#
# kind:
#   "func"        — locate a function definition, insert at top of its body
#   "before"      — insert immediately before the first line matching `anchor`
#   "append"      — append to end of file
#   "copy"        — copy a file verbatim from files/ into the tree
#
# func_re / anchor are Python regexes. They are deliberately loose: they match
# the symbol, not the surrounding formatting.
# ---------------------------------------------------------------------------

SPECS = [
    # ---------------- SH4 dynarec ----------------------------------------
    dict(
        hid="sh4-fpscr-update",
        kind="func",
        path="core/hw/sh4/dynarec/sh4_fpu.cpp",
        func_re=r"^\s*(?:static\s+)?(?:void|bool|int|u32)\s+UpdateFPSCR\s*\(",
        block=BLOCK_FPSCR_UPDATE,
        include="hw/pvr/speedhack.h",
        required=True,
    ),
    dict(
        hid="sh4-fpscr-set",
        kind="func",
        path="core/hw/sh4/dynarec/sh4_fpu.cpp",
        func_re=r"^\s*(?:static\s+)?(?:void|bool|int|u32)\s+SetFPSCR\s*\(",
        block=BLOCK_FPSCR_SET,
        include="hw/pvr/speedhack.h",
        required=True,
    ),
    dict(
        hid="sh4-smc",
        kind="func",
        path="core/hw/sh4/dynarec/dynarec.cpp",
        func_re=r"^\s*(?:static\s+)?bool\s+RuntimeBlockCheck\s*\(",
        block=BLOCK_SMC,
        include="hw/pvr/speedhack.h",
        required=True,
    ),

    # ---------------- AICA audio ------------------------------------------
    dict(
        hid="aica-dsp-step",
        kind="func",
        path="core/hw/aica/dsp.cpp",
        func_re=r"^\s*(?:static\s+)?void\s+dsp_step\s*\(",
        block=BLOCK_AICA_DSP_STEP,
        include="hw/pvr/speedhack.h",
        required=True,
    ),
    dict(
        hid="aica-dsp-write",
        kind="func",
        path="core/hw/aica/dsp.cpp",
        func_re=r"^\s*(?:static\s+)?void\s+dsp_writenmem\s*\(",
        block=BLOCK_AICA_DSP_WRITE,
        include="hw/pvr/speedhack.h",
        required=False,
    ),
    dict(
        hid="aica-sample-div",
        kind="func",
        path="core/hw/aica/aica.cpp",
        func_re=r"^\s*(?:static\s+)?void\s+AICA_Sample\s*\(",
        block=BLOCK_AICA_SAMPLE,
        include="hw/pvr/speedhack.h",
        required=True,
    ),
    dict(
        hid="aica-arm7-alt",
        kind="func",
        path="core/hw/aica/aica.cpp",
        func_re=r"^\s*(?:static\s+)?void\s+UpdateArm7\s*\(",
        block=BLOCK_AICA_ARM7,
        include="hw/pvr/speedhack.h",
        required=False,
    ),

    # ---------------- PVR -------------------------------------------------
    dict(
        hid="pvr-draw",
        kind="before",
        path="core/hw/pvr/rend.cpp",
        anchor=r"renderer->draw\s*\(",
        block=BLOCK_PVR_DRAW,
        include="hw/pvr/speedhack.h",
        required=True,
    ),

    # ---------------- new files -------------------------------------------
    dict(hid="file-speedhack-h", kind="copy",
         src="core/hw/pvr/speedhack.h", dst="core/hw/pvr/speedhack.h"),
    dict(hid="file-speedhack-cpp", kind="copy",
         src="core/hw/pvr/speedhack.cpp", dst="core/hw/pvr/speedhack.cpp"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_lines(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.readlines()

def write_lines(path: str, lines):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(lines)

def backup_once(path: str, report):
    bak = path + ".speedhack.bak"
    if os.path.exists(bak):
        return
    shutil.copy2(path, bak)
    report.append("  backup: " + os.path.relpath(bak))

def already_has_marker(lines, hid: str) -> bool:
    needle = _mark_begin(hid)
    return any(needle in ln for ln in lines)

def file_has_marker(path: str, hid: str) -> bool:
    if not os.path.exists(path):
        return False
    return already_has_marker(read_lines(path), hid)

def find_func_body_insert_index(lines, func_re: str):
    """
    Return the line index at which to insert (i.e. the line AFTER the opening
    brace of the matching function body), or None.

    Handles both `void f() {` and:

        void f()
        {
    """
    rx = re.compile(func_re)
    for i, ln in enumerate(lines):
        if not rx.search(ln):
            continue
        # Scan forward a few lines for the opening brace. Six is generous:
        # flycast signatures sometimes wrap, and attributes may sit between
        # the signature and the brace.
        for j in range(i, min(i + 6, len(lines))):
            if "{" in lines[j]:
                return j + 1
        return None
    return None

def find_line_index(lines, anchor_re: str):
    rx = re.compile(anchor_re)
    for i, ln in enumerate(lines):
        if rx.search(ln):
            return i
    return None

def ensure_include(lines, include_path: str):
    """
    Insert `#include "include_path"` after the last existing #include near the
    top of the file, if not already present. Returns True if the file changed.
    """
    needle = '#include "' + include_path + '"'
    if any(needle in ln for ln in lines):
        return False

    last_inc = None
    for i, ln in enumerate(lines[:120]):
        if ln.lstrip().startswith("#include"):
            last_inc = i

    if last_inc is None:
        # No include block found in the first 120 lines — put it at the very
        # top after any leading comment block.
        insert_at = 0
        for i, ln in enumerate(lines[:40]):
            s = ln.strip()
            if s.startswith("/*") or s.startswith("*") or s.startswith("//") \
               or s == "" or s.startswith("#pragma"):
                insert_at = i + 1
            else:
                break
        lines.insert(insert_at, needle + "\n")
    else:
        lines.insert(last_inc + 1, needle + "\n")
    return True

# ---------------------------------------------------------------------------
# Apply / revert
# ---------------------------------------------------------------------------

def apply_spec(root: str, spec: dict, report, dry: bool) -> str:
    """
    Returns one of: 'applied', 'skipped-present', 'missing-file',
    'anchor-missing', 'copied', 'copy-exists'.
    """
    kind = spec["kind"]

    # ---- copy (new files) ------------------------------------------------
    if kind == "copy":
        src = os.path.join(SCRIPT_DIR, "files", spec["src"])
        dst = os.path.join(root, spec["dst"])
        if not os.path.exists(src):
            report.append("  MISSING SOURCE: " + src)
            return "missing-file"
        if os.path.exists(dst):
            report.append("  already present: " + spec["dst"])
            return "copy-exists"
        if dry:
            report.append("  would copy -> " + spec["dst"])
            return "copied"
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        report.append("  copied -> " + spec["dst"])
        return "copied"

    # ---- edits to existing files ----------------------------------------
    path = os.path.join(root, spec["path"])
    if not os.path.exists(path):
        report.append("  MISSING FILE: " + spec["path"])
        return "missing-file"

    lines = read_lines(path)

    if already_has_marker(lines, spec["hid"]):
        report.append("  already applied: " + spec["path"])
        return "skipped-present"

    insert_at = None

    if kind == "func":
        insert_at = find_func_body_insert_index(lines, spec["func_re"])
        if insert_at is None:
            report.append("  ANCHOR NOT FOUND: %s  (looking for %s)"
                          % (spec["path"], spec["func_re"]))
            return "anchor-missing"
    elif kind == "before":
        idx = find_line_index(lines, spec["anchor"])
        if idx is None:
            report.append("  ANCHOR NOT FOUND: %s  (looking for %s)"
                          % (spec["path"], spec["anchor"]))
            return "anchor-missing"
        insert_at = idx
    elif kind == "append":
        insert_at = len(lines)
    else:
        raise ValueError("unknown spec kind: " + kind)

    if dry:
        report.append("  would insert %s at %s:%d"
                      % (spec["hid"], spec["path"], insert_at + 1))
        return "applied"

    backup_once(path, report)

    block_lines = [ln + "\n" for ln in spec["block"].rstrip("\n").split("\n")]
    lines[insert_at:insert_at] = block_lines

    if spec.get("include"):
        if ensure_include(lines, spec["include"]):
            report.append("  added include: " + spec["include"])

    write_lines(path, lines)
    report.append("  applied %s -> %s:%d"
                  % (spec["hid"], spec["path"], insert_at + 1))
    return "applied"

def revert_all(root: str, report) -> int:
    removed = 0
    for spec in SPECS:
        if spec["kind"] == "copy":
            dst = os.path.join(root, spec["dst"])
            if os.path.exists(dst):
                os.remove(dst)
                report.append("  removed file: " + spec["dst"])
                removed += 1
            continue

        path = os.path.join(root, spec["path"])
        if not os.path.exists(path):
            continue

        lines = read_lines(path)
        b = _mark_begin(spec["hid"])
        e = _mark_end(spec["hid"])

        out = []
        skipping = False
        hit = False
        for ln in lines:
            if b in ln:
                skipping = True
                hit = True
                continue
            if e in ln:
                skipping = False
                continue
            if not skipping:
                out.append(ln)

        if hit:
            write_lines(path, out)
            report.append("  reverted %s in %s" % (spec["hid"], spec["path"]))
            removed += 1

    report.append("")
    report.append("  %d change(s) reverted. Backups (*.speedhack.bak) left in place."
                  % removed)
    return 0

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Anchor-based patcher for the slopcast-vita speed-hack layer.")
    ap.add_argument("--root", required=True,
                    help="Path to the root of your slopcast-vita checkout.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true",
                   help="Report insertion points without writing anything.")
    g.add_argument("--apply", action="store_true",
                   help="Apply the patch series.")
    g.add_argument("--revert", action="store_true",
                   help="Remove every marked block and the copied files.")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.stderr.write("ERROR: --root is not a directory: %s\n" % root)
        return 1

    # Sanity: are we actually looking at a flycast-derived tree?
    marker = os.path.join(root, "core", "hw", "pvr")
    if not os.path.isdir(marker):
        sys.stderr.write(
            "ERROR: %s does not look like a flycast-derived checkout.\n"
            "       Expected to find core/hw/pvr/ underneath it.\n" % root)
        return 1

    report = []

    if args.revert:
        report.append("Reverting from: " + root)
        report.append("")
        rc = revert_all(root, report)
        print("\n".join(report))
        return rc

    dry = args.check
    report.append(("Dry run (nothing will be written) from: " if dry
                   else "Applying to: ") + root)
    report.append("")

    problems = 0
    applied = 0
    skipped = 0

    for spec in SPECS:
        result = apply_spec(root, spec, report, dry)
        if result == "applied":
            applied += 1
        elif result in ("skipped-present", "copy-exists"):
            skipped += 1
        elif result in ("anchor-missing", "missing-file"):
            if spec.get("required", False):
                problems += 1
            else:
                # Optional hooks are common to miss — they are revision
                # dependent and the module degrades gracefully without them.
                report.append("  (optional hook, skipped)")

    report.append("")
    report.append("  applied=%d  skipped=%d  required-problems=%d"
                  % (applied, skipped, problems))

    if problems:
        report.append("")
        report.append("  One or more REQUIRED anchors were not found. Those hooks")
        report.append("  were NOT applied. Check the file paths and function names")
        report.append("  against your revision, or wire them by hand using the")
        report.append("  'Manual hooks' section of README.md.")

    if dry and applied:
        report.append("")
        report.append("  Re-run with --apply to write these changes.")

    print("\n".join(report))
    return 0 if problems == 0 else 2

if __name__ == "__main__":
    sys.exit(main())
