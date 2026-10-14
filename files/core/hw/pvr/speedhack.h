/*
 * speedhack.h — Vita-first "gameplay over accuracy" layer for slopcast-vita.
 *
 * Base: Rinnegatamante/flycast-vita @ 446f887741ce38596224e6cea83148627d4263e6
 *
 * Design rules:
 *   1. No hack is compiled out. Every hack is a runtime branch on a profile
 *      field, so one binary serves both a 60fps Crazy Taxi session and an
 *      accuracy bisect.
 *   2. No dependency on flycast headers. This file includes only <cstdint>,
 *      so it cannot break when upstream renames a type.
 *   3. Configuration is resolved lazily on first query. There is no init()
 *      call to wire into the emulator startup path.
 *   4. On non-Vita builds the default preset is Accurate, so a shared tree
 *      behaves exactly as stock on PC.
 *
 * All queries are const, non-virtual, and defined in the same translation
 * unit as the profile, so the compiler can inline them at every call site.
 */
#pragma once

#include <cstdint>

namespace speedhack
{
	enum class Preset : int
	{
		Accurate = 0,   // stock flycast behaviour, every hack off
		Balanced = 1,   // safe wins only, no expected visual regression
		Speed    = 2,   // visible shortcuts, gameplay intact
		Fastest  = 3,   // target for SA / CT / VT — accuracy is sacrificed
	};

	struct Profile
	{
		Preset preset;
		// ---- PVR / GPU ----
		bool perStrip;           // flush non-opaque TA lists at end-of-list
		bool skipModVolumes;     // drop the modifier-volume stencil pass
		bool skipShadowPass;     // drop the translucent depth pre-pass
		bool skipRTT;            // no render-to-texture / framebuffer readback
		bool forceNearest;       // GL_NEAREST, no mip chain
		int  lodBias;            // negative = sharper and cheaper on SGX543
		// ---- SH4 dynarec ----
		bool fastFpscr;          // hoist redundant host FP rounding-mode updates
		bool singlePrecisionFPU; // force PR/FR/SZ off — single bank, single precision
		bool skipSmcCheck;       // never invalidate dynarec blocks
		bool tightSched;         // coarser sh4_sched granularity
		// ---- AICA ----
		bool aicaDSP;            // run the Yamaha DSP at all
		int  aicaSampleDiv;      // 1 = 44.1k, 2 = 22.05k, 4 = 11.025k
		bool aicaArm7Alt;        // step ARM7 every other audio tick
	};

	// Resolved profile. Safe to call from any thread; resolution happens once.
	const Profile& profile();

	// ---- PVR layer ---------------------------------------------------------
	bool perStrip();
	bool skipModVolumes();
	bool skipShadowPass();
	bool skipRTT();
	bool forceNearest();
	int  lodBias();

	// ---- SH4 layer ---------------------------------------------------------
	bool fastFpscr();
	bool singlePrecisionFPU();
	bool skipSmcCheck();
	bool tightSched();

	// ---- AICA layer --------------------------------------------------------
	bool aicaDSP();
	int  aicaSampleDiv();
	bool aicaArm7Alt();

	// Called from the SH4 reset path. Invalidates the cached host FPSCR bits
	// so a save-state load or hard reset cannot carry a stale rounding mode
	// into the next frame.
	void resetFpscrCache();

	// ---- Thread affinity (Cortex-A9 quad) ----------------------------------
	// Rule: never pin the emu thread and the GPU-submit thread to the same
	// core. Under vitaGL the GLES->GXM command-buffer build is CPU-side, so
	// both threads do real work and will thrash each other's L2 slice.
	void pinEmuThread();
	void pinAudioThread();
	void pinGpuThread();

	// ---- Threading helper --------------------------------------------------
	// Yields the calling core instead of burning it in a spin. Use in the
	// render-thread handshake.
	void yieldCore(int microseconds = 50);
}
