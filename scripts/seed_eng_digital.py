"""
§17.154 — Seed the engineering RAG corpus with curated digital
building-block references.

Companion script to §17.149's ``seed_eng_topologies.py``, which
covered analog filter topologies. This one covers the digital
surface §17.152's Verilator-in-the-loop sizing stage expects to be
asked to size: counters, FIFOs, RAMs, FSMs, arithmetic units,
decoders/encoders, clock-domain crossing primitives, and ECC.

Both scripts populate the same ``eng`` Milvus partition; the §17.146
topology-select stage's retrieval query carries ``design.kind`` in
the natural-language search string (``"design kind: digital_logic"``
vs ``"design kind: analog_circuit"``) so chunks tagged with the
matching kind rank higher for queries from that side.

Behavior:

  * ``--dry-run``  Print the ingest plan without writing.
  * ``--with-urls`` Also ingest a set of canonical reference URLs
                    via ``run_research`` (depth=shallow, domain=eng).
                    Slower (~30-60s per URL); off by default.
  * Idempotent — the §9.x dedup pipeline rejects exact content-hash
    matches, so re-running is safe.

Run from inside the orchestrator container:

    docker exec scaffold-orchestrator python scripts/seed_eng_digital.py
    docker exec scaffold-orchestrator python scripts/seed_eng_digital.py --dry-run
    docker exec scaffold-orchestrator python scripts/seed_eng_digital.py --with-urls

Exit codes:
  0 success
  1 bad CLI flags
  2 ingest path returned an error (e.g. Milvus unavailable)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger("scaffold.seed_eng_digital")

DOMAIN = "eng_design"  # §17.329 — circuit content lives in its own partition
SOURCE_TYPE = "curated"
CONFIDENCE = 0.90


# ---------------------------------------------------------------------------
# Hand-curated digital building-block references
# ---------------------------------------------------------------------------

SEEDS: list[dict[str, Any]] = [
    # --- Counters ---
    {
        "title": "Synchronous N-bit binary counter",
        "tags": ["digital", "digital_logic", "counter", "binary", "synchronous"],
        "source_url": "https://en.wikipedia.org/wiki/Counter_(digital)#Synchronous_counter",
        "content": (
            "A synchronous N-bit binary counter increments by 1 on each "
            "active clock edge, wrapping from 2^N - 1 back to 0. All "
            "flip-flops share the same clock, eliminating ripple "
            "propagation delay. The maximum operating frequency is set "
            "by the carry-chain depth (combinational path from LSB to "
            "MSB through the adder logic). SystemVerilog: "
            "`always_ff @(posedge clk or negedge rst_n) if (!rst_n) "
            "count <= '0; else count <= count + 1'b1;`. Wrap latency: "
            "exactly 2^N cycles from reset to first wrap. Power "
            "consumption scales with the average number of bits "
            "toggling per cycle (high for binary). Used as base for "
            "address generators, watchdog timers, and time-base "
            "dividers."
        ),
    },
    {
        "title": "Ring counter (one-hot rotation)",
        "tags": ["digital", "digital_logic", "counter", "ring", "one_hot"],
        "source_url": "https://en.wikipedia.org/wiki/Ring_counter",
        "content": (
            "A ring counter is a circular shift register where exactly "
            "one flip-flop holds a '1' and the rest hold '0'; on each "
            "clock the '1' rotates to the next position. For N stages, "
            "the cycle length is N. Decoded outputs require no "
            "combinational logic — each bit IS the decoded state. "
            "Trade-off vs binary counter: uses N flip-flops to encode "
            "N states (vs log2(N) for binary), but eliminates decoder "
            "delay and toggles only 2 flip-flops per cycle (low power). "
            "Common use: control sequencers, state-machine encoding "
            "where decoded outputs are needed every cycle. Initialise "
            "via reset to put the '1' in a known position; otherwise "
            "the counter can lock up in an all-zeros or multi-one state."
        ),
    },
    {
        "title": "Johnson counter (twisted-ring counter)",
        "tags": ["digital", "digital_logic", "counter", "johnson", "twisted_ring"],
        "source_url": "https://en.wikipedia.org/wiki/Ring_counter#Johnson_counter",
        "content": (
            "A Johnson counter is a ring counter where the inverted "
            "output of the last stage feeds back to the first stage's "
            "input. For N stages, the cycle length is 2N — twice as "
            "many states as a plain ring counter for the same flip-"
            "flop count. The state sequence is a Gray-code-like "
            "pattern (only one bit changes per transition), making it "
            "useful for low-EMI applications and for driving "
            "quadrature-output dividers. Common use: digital "
            "synthesizer phase generators, 3-phase motor drive logic, "
            "decoded clock dividers. SystemVerilog: like a ring "
            "counter but the input to the first stage is "
            "`~count[N-1]` instead of `count[N-1]`."
        ),
    },
    {
        "title": "BCD (binary-coded decimal) counter",
        "tags": ["digital", "digital_logic", "counter", "bcd", "decade"],
        "source_url": "https://en.wikipedia.org/wiki/Binary-coded_decimal",
        "content": (
            "A BCD counter increments through 0–9 then wraps to 0, "
            "encoded as 4 bits per decade. Cascaded BCD counters "
            "implement multi-digit decimal counters (seconds-of-day, "
            "frequency counters with decimal display). At count==9, "
            "the next clock resets that decade and pulses a "
            "carry-to-next-decade output. SystemVerilog: `if "
            "(count == 4'd9) begin count <= 0; carry <= 1; end else "
            "count <= count + 1;`. Slower (more decoder logic per "
            "cycle) than a binary counter for the same range, but "
            "trivially converts to seven-segment-display output via a "
            "BCD-to-7-segment decoder. Common in user-facing "
            "instruments where decimal display matters more than "
            "internal density."
        ),
    },
    {
        "title": "Gray-code counter (low-power, low-EMI)",
        "tags": ["digital", "digital_logic", "counter", "gray_code", "low_power"],
        "source_url": "https://en.wikipedia.org/wiki/Gray_code",
        "content": (
            "A Gray-code counter produces a sequence where only one "
            "bit changes between consecutive states. Cycle length is "
            "still 2^N for N bits, identical to binary, but with "
            "transition-toggle count fixed at 1 per cycle (vs up to N "
            "for binary at the rollover boundary). Power benefit: "
            "lower switching activity drives lower dynamic power and "
            "lower EMI emission. Common use: cross-clock-domain "
            "address generators (the single-bit-change property means "
            "a synchronizer reading the counter mid-transition sees "
            "either the old or new value, never an intermediate "
            "garbage state). Trade-off: arithmetic on Gray-coded "
            "values requires conversion to binary first, so the "
            "counter is preferred over the calculator role."
        ),
    },
    # --- Storage ---
    {
        "title": "Synchronous FIFO (single-clock-domain)",
        "tags": ["digital", "digital_logic", "fifo", "synchronous", "storage"],
        "source_url": "https://en.wikipedia.org/wiki/FIFO_(computing_and_electronics)",
        "content": (
            "A synchronous FIFO buffers data between two parties "
            "operating in the same clock domain. Standard topology: a "
            "circular memory of DEPTH entries, a write pointer and a "
            "read pointer (each one bit wider than log2(DEPTH) to "
            "distinguish full from empty via the upper bit), full and "
            "empty flags derived from pointer comparison. Push when "
            "wr_en && !full advances the write pointer; pop when "
            "rd_en && !empty advances the read pointer. SystemVerilog "
            "testbench discipline (per §17.141): drive stimulus at "
            "negedge, sample DUT outputs at posedge — driving on "
            "posedge races the DUT's always_ff sampling and produces "
            "off-by-one errors. Common use: pipeline-stage decoupling, "
            "burst absorption for non-uniform producers/consumers."
        ),
    },
    {
        "title": "Asynchronous FIFO (clock-domain crossing)",
        "tags": ["digital", "digital_logic", "fifo", "async", "cdc"],
        "source_url": "https://en.wikipedia.org/wiki/Asynchronous_circuit",
        "content": (
            "An asynchronous FIFO bridges two independent clock "
            "domains. The write logic uses the producer clock; the "
            "read logic uses the consumer clock. Pointer comparison "
            "for full/empty flags requires cross-domain pointer "
            "exchange — done by Gray-coding the pointers so a single "
            "sampling-window glitch yields either the old or new "
            "pointer (never an intermediate garbage value). Each "
            "domain sees the OTHER domain's pointer through a 2-FF "
            "synchronizer plus Gray-to-binary conversion. Full flag "
            "is generated in the write domain (compare local "
            "write_ptr to synced read_ptr); empty in the read domain "
            "(compare local read_ptr to synced write_ptr). Critical "
            "for cross-clock buses, ADC/DAC interfaces, "
            "asynchronous-message handling."
        ),
    },
    {
        "title": "Single-port synchronous RAM (SP-SRAM)",
        "tags": ["digital", "digital_logic", "ram", "memory", "single_port"],
        "source_url": "https://en.wikipedia.org/wiki/Random-access_memory",
        "content": (
            "Single-port synchronous RAM: one address bus, one data "
            "I/O, read-OR-write per cycle (mutually exclusive). FPGA "
            "BRAMs and ASIC compiled-SRAMs in this configuration "
            "deliver the highest density per bit. SystemVerilog "
            "inferred-RAM pattern: `logic [W-1:0] mem [DEPTH-1:0]; "
            "always_ff @(posedge clk) begin if (we) mem[addr] <= din; "
            "dout <= mem[addr]; end`. Read-during-write behavior is "
            "implementation-defined (read-first vs write-first vs "
            "no-change) — pick the policy explicitly in the RTL or "
            "the synth tool picks for you. Common use: data buffers, "
            "lookup tables, instruction memories. Width × depth × "
            "ports decide whether the inferred-RAM maps to BRAM (good) "
            "or flip-flop array (bad — wastes area)."
        ),
    },
    {
        "title": "True dual-port synchronous RAM",
        "tags": ["digital", "digital_logic", "ram", "memory", "dual_port"],
        "source_url": "https://en.wikipedia.org/wiki/Dual-ported_RAM",
        "content": (
            "Dual-port synchronous RAM: two independent address/data "
            "ports, each capable of read and write per cycle. Two "
            "modes: true dual-port (both ports symmetric, common in "
            "compiled SRAMs) vs simple dual-port (port A is write-"
            "only, port B is read-only — typical FPGA BRAM mode). "
            "Read-during-write across ports requires explicit "
            "conflict resolution: if both ports write the same "
            "address in the same cycle, the result is "
            "implementation-defined. Common use: producer-consumer "
            "buffers within the same clock domain (one port writes, "
            "the other reads), register files (one read port, one "
            "write port per pipeline stage), small CAMs built from "
            "RAM + comparator."
        ),
    },
    {
        "title": "Shift register (SISO / SIPO / PISO)",
        "tags": ["digital", "digital_logic", "shift_register", "serial"],
        "source_url": "https://en.wikipedia.org/wiki/Shift_register",
        "content": (
            "A shift register is a cascade of flip-flops where each "
            "stage feeds the next. Three common modes by IO shape: "
            "SISO (serial-in, serial-out — delay line), SIPO "
            "(serial-in, parallel-out — deserializer), PISO (parallel-"
            "in, serial-out — serializer). Bidirectional shift "
            "registers add an MSB-or-LSB direction select. Universal "
            "shift registers add load (parallel write) and clear "
            "(synchronous reset) modes per stage. SystemVerilog SIPO "
            "pattern: `always_ff @(posedge clk) shifter <= "
            "{shifter[N-2:0], serial_in};`. Common use: SPI/I2C bit-"
            "banging, frame deserialization, history buffers for FIR "
            "filters, debounce delay lines."
        ),
    },
    # --- State machines ---
    {
        "title": "Moore-style finite state machine (FSM)",
        "tags": ["digital", "digital_logic", "fsm", "moore", "state_machine"],
        "source_url": "https://en.wikipedia.org/wiki/Moore_machine",
        "content": (
            "Moore FSM: outputs depend ONLY on the current state, "
            "not on the current input. Outputs are latched at the "
            "state boundary, so they're glitch-free across clock "
            "edges. State transitions depend on (state, inputs). The "
            "canonical 3-process SystemVerilog template: (1) "
            "synchronous state register `always_ff @(posedge clk) "
            "state <= next_state;` (2) combinational next-state logic "
            "`always_comb case (state) ...`, (3) combinational output "
            "logic `always_comb case (state) ...`. One-hot, binary, "
            "and Gray encodings each have trade-offs: one-hot is "
            "fastest (no decoder), binary is densest, Gray is lowest "
            "switching. Common use: protocol decoders, control "
            "sequencers where output glitch immunity matters."
        ),
    },
    {
        "title": "Mealy-style finite state machine",
        "tags": ["digital", "digital_logic", "fsm", "mealy", "state_machine"],
        "source_url": "https://en.wikipedia.org/wiki/Mealy_machine",
        "content": (
            "Mealy FSM: outputs depend on (current_state, current_"
            "input). Outputs change asynchronously with the input "
            "within a cycle (combinational from input to output), so "
            "they can glitch if inputs aren't stable. Trade-off vs "
            "Moore: Mealy typically needs fewer states for the same "
            "behavior because output can fire on the input itself "
            "rather than the state after the input. SystemVerilog "
            "pattern same as Moore but the output `always_comb` "
            "block's case is on `(state, inputs)` not just `state`. "
            "Common use: arbiters where the grant fires same-cycle as "
            "the request, fast protocol responses, ALU control where "
            "one-cycle decode-and-execute is worth the glitch risk."
        ),
    },
    {
        "title": "One-hot encoded FSM",
        "tags": ["digital", "digital_logic", "fsm", "one_hot", "encoding"],
        "source_url": "https://en.wikipedia.org/wiki/State_encoding_for_low_power",
        "content": (
            "One-hot FSM encoding: N states use N flip-flops, exactly "
            "one is '1' at a time. Trades flip-flop count for decoder "
            "speed — each state's output is a single flip-flop's Q, "
            "no combinational decoder needed. Transitions are simple "
            "next-state shifts. Synth tools default to one-hot when "
            "speed-optimizing and to binary when area-optimizing; "
            "force the encoding with `(* fsm_encoding = \"one_hot\" "
            "*)` attribute when the default doesn't match design "
            "intent. Common use: high-speed protocol decoders where "
            "per-state output fanout matters, low-power FSMs where "
            "single-bit-change transitions reduce switching activity. "
            "Drawback: state count beyond ~32 makes the flip-flop "
            "count impractical; switch to binary or gray."
        ),
    },
    # --- Arithmetic ---
    {
        "title": "Ripple-carry adder",
        "tags": ["digital", "digital_logic", "adder", "ripple_carry", "arithmetic"],
        "source_url": "https://en.wikipedia.org/wiki/Adder_(electronics)#Ripple-carry_adder",
        "content": (
            "A ripple-carry adder is a cascade of N full-adders where "
            "each stage's carry-out feeds the next stage's carry-in. "
            "Total propagation delay scales linearly with N (the worst-"
            "case path is from LSB carry-in to MSB carry-out). For "
            "N=8 this is ~16 gate delays on the carry chain; for "
            "N=32 it's ~64 — the dominant timing limit for medium- to "
            "wide-width adders. SystemVerilog: most tools infer a "
            "ripple-carry adder from `assign sum = a + b;` unless "
            "told otherwise. Simplest topology, lowest area, slowest. "
            "Used for narrow widths (N≤8) where its simplicity wins; "
            "wider designs switch to carry-lookahead or carry-select."
        ),
    },
    {
        "title": "Carry-lookahead adder",
        "tags": ["digital", "digital_logic", "adder", "carry_lookahead", "arithmetic"],
        "source_url": "https://en.wikipedia.org/wiki/Carry-lookahead_adder",
        "content": (
            "A carry-lookahead adder (CLA) reduces the carry-chain "
            "delay from O(N) (ripple) to O(log N) by computing each "
            "bit's carry-in directly from the generate/propagate "
            "signals of all lower bits in parallel. Each bit i has "
            "generate G_i = a_i AND b_i and propagate P_i = a_i XOR "
            "b_i; the carry-in to bit i is C_i = G_(i-1) OR (P_(i-1) "
            "AND C_(i-1)), recursively expanded. Practical "
            "implementations group bits into 4-bit or 8-bit blocks "
            "with block-level lookahead. Trade-off vs ripple-carry: "
            "lower delay at the cost of higher area and routing "
            "complexity. Used in datapath designs needing single-"
            "cycle wide-adders (CPUs, DSPs, MAC units)."
        ),
    },
    {
        "title": "Booth's multiplication algorithm",
        "tags": ["digital", "digital_logic", "multiplier", "booth", "arithmetic"],
        "source_url": "https://en.wikipedia.org/wiki/Booth%27s_multiplication_algorithm",
        "content": (
            "Booth's algorithm multiplies two signed numbers in 2's "
            "complement directly, without sign-extension preprocessing. "
            "The radix-2 Booth recoder examines pairs of multiplier "
            "bits and emits one of {add multiplicand, subtract "
            "multiplicand, no-op} per partial product, reducing the "
            "number of partial products (and thus the adder-tree "
            "depth) when the multiplier has long runs of 1s. Radix-4 "
            "modified Booth examines 3-bit windows and halves the "
            "partial-product count. Pipelined implementations chain "
            "the partial-product generation with a Wallace tree of "
            "carry-save adders before a final ripple/CLA. Used in "
            "DSP MAC units, signed multiplication in CPU ALUs."
        ),
    },
    {
        "title": "Magnitude comparator",
        "tags": ["digital", "digital_logic", "comparator", "arithmetic"],
        "source_url": "https://en.wikipedia.org/wiki/Digital_comparator",
        "content": (
            "A magnitude comparator emits the three signals A>B, A=B, "
            "A<B from two N-bit inputs. Cascaded implementations "
            "ripple comparison from MSB to LSB: at each bit, if the "
            "bits differ, the result is fixed; otherwise the result "
            "is the cascaded carry from below. Parallel "
            "implementations compute equality (XNOR of each bit, "
            "ANDed) and inequality via subtractor + sign-bit. "
            "Synthesizers infer comparators from `a > b` / `a == b` "
            "/ `a < b` operators in SystemVerilog. Used in scheduler "
            "priority logic, watchdog threshold detectors, and "
            "address-range decoders where one bus address must "
            "satisfy A_low <= addr < A_high."
        ),
    },
    # --- Decoders / encoders ---
    {
        "title": "Binary decoder (N-to-2^N)",
        "tags": ["digital", "digital_logic", "decoder", "demultiplexer"],
        "source_url": "https://en.wikipedia.org/wiki/Binary_decoder",
        "content": (
            "A binary decoder converts an N-bit binary input into a "
            "one-hot 2^N-bit output. Bit i of the output is '1' iff "
            "the input value equals i. Add an enable input and the "
            "decoder becomes a 1-to-2^N demultiplexer (the enable is "
            "the data, the address selects which output it lands on). "
            "Hierarchical decoders (e.g. 6-to-64 built from 2-to-4 "
            "and 4-to-16 stages) trade depth for routing area. "
            "SystemVerilog: `assign onehot = 1'b1 << addr;`. Used "
            "for chip-select generation in memory-mapped buses, RAM "
            "address decoders, instruction-format decoding in CPUs."
        ),
    },
    {
        "title": "Priority encoder",
        "tags": ["digital", "digital_logic", "encoder", "priority"],
        "source_url": "https://en.wikipedia.org/wiki/Priority_encoder",
        "content": (
            "A priority encoder is the inverse of a decoder: from a "
            "multi-bit input it outputs the index of the highest-"
            "priority (typically lowest-numbered or highest-numbered) "
            "active bit. An N-to-log2(N) encoder accepts 2^N input "
            "lines and emits the N-bit index. A 'valid' output flag "
            "distinguishes 'index 0 because no bits set' from 'index "
            "0 because bit 0 is set'. SystemVerilog: `casez` with "
            "wildcards for priority, or explicit `for` loop. Used in "
            "interrupt controllers (decode the highest-priority "
            "pending IRQ), arbiters (grant to the highest-priority "
            "requester), find-first-set instructions in CPUs."
        ),
    },
    {
        "title": "Multiplexer (mux)",
        "tags": ["digital", "digital_logic", "multiplexer", "mux"],
        "source_url": "https://en.wikipedia.org/wiki/Multiplexer",
        "content": (
            "An N-to-1 multiplexer selects one of N data inputs and "
            "drives it to the output based on log2(N) select lines. "
            "The most common datapath building block — every "
            "register file write port, every ALU operand source, "
            "every conditional assignment in RTL compiles to a mux. "
            "SystemVerilog `assign out = sel ? a : b;` for a 2-to-1; "
            "wider muxes via `case` statements or array indexing. "
            "Synth tools build wide muxes from 2-to-1 trees by "
            "default; explicit pipelining helps when the input "
            "fanin tree exceeds the cycle's timing budget. Bus "
            "muxing across clock domains requires CDC handling — "
            "the §17.141 timing-race lesson applies."
        ),
    },
    # --- Clock / CDC ---
    {
        "title": "2-FF synchronizer (clock-domain crossing primitive)",
        "tags": ["digital", "digital_logic", "synchronizer", "cdc", "metastability"],
        "source_url": "https://en.wikipedia.org/wiki/Clock_domain_crossing",
        "content": (
            "A 2-FF synchronizer is the canonical clock-domain "
            "crossing primitive for single-bit signals. Two flip-"
            "flops in series sample the asynchronous input on the "
            "destination clock; the first stage may go metastable, "
            "but by the second stage's sampling time the probability "
            "of metastable propagation has decayed exponentially. "
            "MTBF improves dramatically with each added stage. "
            "Multi-bit buses CANNOT use 2-FF sync directly — the bits "
            "could synchronize at different cycles and produce "
            "transient garbage values; multi-bit CDC needs handshake, "
            "Gray-coding, or an async FIFO. Synth tools recognize "
            "the pattern via `(* async_reg = \"true\" *)` attributes "
            "and place the FFs adjacently to maximize MTBF."
        ),
    },
    {
        "title": "Edge detector (rising / falling / both)",
        "tags": ["digital", "digital_logic", "edge_detector"],
        "source_url": "https://en.wikipedia.org/wiki/Edge_detection",
        "content": (
            "An edge detector emits a 1-cycle pulse when its input "
            "transitions in the selected direction (rising, falling, "
            "or both). Standard topology: one flip-flop delays the "
            "input by one cycle; the output is "
            "`(current_input AND NOT delayed_input)` for rising, "
            "`(NOT current_input AND delayed_input)` for falling, or "
            "the XOR of the two for both-edges. SystemVerilog: "
            "`always_ff @(posedge clk) prev <= sig; assign rising = "
            "sig & ~prev;`. Common use: converting level signals to "
            "pulse signals for FIFO push/pop control, debounced-"
            "button event detection, packet-start detection in "
            "serial protocols."
        ),
    },
    {
        "title": "Integer clock divider",
        "tags": ["digital", "digital_logic", "clock_divider", "clock"],
        "source_url": "https://en.wikipedia.org/wiki/Clock_divider",
        "content": (
            "An integer clock divider generates a slower clock from "
            "a faster one by counting input clock edges. For even "
            "division by 2N: a counter triggers a toggle every N "
            "cycles, producing a 50% duty-cycle output at "
            "frequency_in / (2N). Odd division requires both edges of "
            "the input clock to produce a 50% duty cycle, or accepts "
            "an asymmetric duty cycle. Glitch-free output requires "
            "the divided clock to be brought out through a flip-flop "
            "rather than the counter's MSB directly. For "
            "fractional/PLL-based division, the divider becomes a "
            "DDS (direct digital synthesis) accumulator or a "
            "phase-locked-loop primitive — different topology, "
            "different §17.146 candidate."
        ),
    },
    # --- ECC ---
    {
        "title": "Parity checker / generator (single-bit error detect)",
        "tags": ["digital", "digital_logic", "parity", "ecc", "error_detection"],
        "source_url": "https://en.wikipedia.org/wiki/Parity_bit",
        "content": (
            "A parity bit is the XOR of all data bits (even parity) "
            "or its inverse (odd parity). On transmission, the parity "
            "bit is appended; on reception, the receiver re-computes "
            "the XOR and compares. Single-bit errors flip the "
            "computed parity vs the received parity, surfacing a "
            "parity_error flag. Double-bit errors are NOT detected — "
            "they cancel in the XOR. SystemVerilog: `assign parity = "
            "^data_bus;`. Common use: low-cost integrity check on "
            "memory reads (DRAM ECC's poor cousin), UART error "
            "detection, low-overhead bus-protocol error guards. "
            "When two-bit errors are plausible, upgrade to Hamming "
            "(single-error-correct, double-error-detect)."
        ),
    },
    {
        "title": "Hamming code SECDED (single-error-correct, double-error-detect)",
        "tags": ["digital", "digital_logic", "hamming", "ecc", "secded"],
        "source_url": "https://en.wikipedia.org/wiki/Hamming_code",
        "content": (
            "Hamming code with SECDED extension adds K parity bits to "
            "M data bits where 2^K >= M + K + 1, plus one overall "
            "parity bit. On read, the K syndrome bits identify the "
            "erroneous bit position (if any single bit flipped), "
            "letting the receiver correct it in place. The extra "
            "overall parity distinguishes single-bit errors "
            "(correctable) from double-bit errors (detectable, "
            "uncorrectable). For 64-bit data words, SECDED needs 7 + "
            "1 = 8 ECC bits, yielding 72-bit memory rows — the "
            "canonical DRAM-DIMM-with-ECC layout. SystemVerilog: "
            "encoder and decoder are both wide XOR trees of selected "
            "bit positions; tool-generated from a parity-check "
            "matrix is standard practice. Used in server memory, "
            "satellite electronics, anywhere transient bit-flips "
            "would otherwise corrupt critical data."
        ),
    },
]


URLS_FOR_RESEARCH: list[str] = [
    "https://en.wikipedia.org/wiki/Counter_(digital)",
    "https://en.wikipedia.org/wiki/FIFO_(computing_and_electronics)",
    "https://en.wikipedia.org/wiki/Random-access_memory",
    "https://en.wikipedia.org/wiki/Shift_register",
    "https://en.wikipedia.org/wiki/Finite-state_machine",
    "https://en.wikipedia.org/wiki/Adder_(electronics)",
    "https://en.wikipedia.org/wiki/Multiplexer",
    "https://en.wikipedia.org/wiki/Clock_domain_crossing",
    "https://en.wikipedia.org/wiki/Hamming_code",
]


# ---------------------------------------------------------------------------
# Public helpers — exported so unit tests can call them directly.
# ---------------------------------------------------------------------------

def build_entries() -> list[dict[str, Any]]:
    """Convert ``SEEDS`` into the shape ``ingest_entries`` expects."""
    return [
        {
            "title": s["title"],
            "content": s["content"].strip(),
            "domain_tags": list(s["tags"]),
            "source_url": s["source_url"],
            "source_type": SOURCE_TYPE,
            "confidence": CONFIDENCE,
        }
        for s in SEEDS
    ]


async def _with_http_clients(coro):
    """Eager-init the shared httpx client registry, run ``coro``,
    then close. The orchestrator does this in its lifespan handler;
    a standalone CLI script has to do it itself or the embedder
    (via ``model_router.embed``) hits the registered Ollama client
    before ``init_clients`` runs (see §17.149)."""
    from app.utils import http_clients
    http_clients.init_clients()
    try:
        return await coro
    finally:
        await http_clients.close_clients()


async def ingest_curated() -> dict:
    """Run the curated batch through the existing ingest pipeline."""
    from app.modules.rag_pipeline import ingest_entries
    return await _with_http_clients(
        ingest_entries(build_entries(), domain=DOMAIN)
    )


async def ingest_urls(urls: list[str]) -> dict:
    """Run each URL through ``run_research`` sequentially. Returns a
    summary dict {url: status}. Failures per-url are logged but do
    not abort the batch."""
    from app.modules.research_agent import run_research

    async def _run() -> dict[str, str]:
        summary: dict[str, str] = {}
        for url in urls:
            logger.info("url_ingest_start: url=%s", url)
            events: list[str] = []
            try:
                async for ev in run_research(
                    url, depth="shallow", domain=DOMAIN,
                ):
                    events.append(ev)
                summary[url] = "ok"
                logger.info(
                    "url_ingest_ok: url=%s events=%d", url, len(events),
                )
            except Exception as exc:
                summary[url] = f"error: {exc}"
                logger.error(
                    "url_ingest_failed: url=%s error=%s", url, exc,
                )
        return summary

    return await _with_http_clients(_run())


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _print_plan(with_urls: bool) -> None:
    entries = build_entries()
    print(f"DRY RUN — would ingest {len(entries)} curated entries into domain={DOMAIN!r}:")
    for e in entries:
        print(
            f"  - {e['title']!r:60s} "
            f"({len(e['content'])} chars, source={e['source_url']})"
        )
    if with_urls:
        print(f"\n  + {len(URLS_FOR_RESEARCH)} URLs via run_research(depth='shallow'):")
        for u in URLS_FOR_RESEARCH:
            print(f"    - {u}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without touching the corpus.",
    )
    parser.add_argument(
        "--with-urls",
        action="store_true",
        help="Also ingest canonical URLs via run_research (slow, network).",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.dry_run:
        _print_plan(with_urls=args.with_urls)
        return 0

    try:
        stats = asyncio.run(ingest_curated())
    except Exception as exc:
        logger.error("curated_ingest_failed: %s", exc)
        return 2
    logger.info("curated_ingest_done: stats=%s", stats)

    if args.with_urls:
        try:
            url_summary = asyncio.run(ingest_urls(URLS_FOR_RESEARCH))
        except Exception as exc:
            logger.error("url_ingest_batch_failed: %s", exc)
            return 2
        oks = sum(1 for v in url_summary.values() if v == "ok")
        logger.info(
            "url_ingest_done: oks=%d/%d summary=%s",
            oks, len(URLS_FOR_RESEARCH), url_summary,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
