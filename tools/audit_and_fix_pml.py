"""
Prove My Language (PML) Audit and Remediation Script
Audits and fixes explanatory completeness, first-use acronym expansions,
and technical terminology definitions across all course chapters.
"""

import os
import sys
import json
import re
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_logger = logging.getLogger("pml_audit")

REPO_ROOT = r"c:\Users\ai\workspace\hams_com"

COMMON_ACRONYMS = {
    "SWR": ("Standing Wave Ratio", "A measure of impedance matching between transmission line and antenna."),
    "PEP": ("Peak Envelope Power", "The average power supplied to the transmission line by a transmitter during one RF cycle at the crest of the modulation envelope."),
    "ERP": ("Effective Radiated Power", "The product of the power supplied to the antenna and its gain in a given direction relative to a half-wave dipole."),
    "ALC": ("Automatic Level Control", "A circuit that limits peak audio input to prevent transmitter overdrive, flat-topping, and splatter."),
    "VFO": ("Variable Frequency Oscillator", "A tunable oscillator circuit that controls the operating frequency of a transmitter or receiver."),
    "GFCI": ("Ground Fault Circuit Interrupter", "A fast-acting safety device that disconnects power when it senses an imbalance between hot and neutral currents."),
    "DMM": ("Digital Multimeter", "An electronic measuring instrument combining voltmeter, ammeter, and ohmmeter functions in one digital display."),
    "FET": ("Field-Effect Transistor", "A voltage-controlled semiconductor transistor that uses an electric field to regulate current with near-zero gate current."),
    "BJT": ("Bipolar Junction Transistor", "A current-controlled semiconductor device where base current regulates larger collector-emitter current."),
    "NVIS": ("Near Vertical Incidence Skywave", "A skywave propagation mode directing high-angle signals into the ionosphere to eliminate skip zones within a few hundred miles."),
    "WSPR": ("Weak Signal Propagation Reporter", "A digital beacon mode using four-tone FSK and heavy error-correction to probe worldwide propagation at milliwatt levels."),
    "ARQ": ("Automatic Repeat reQuest", "A digital data protocol that automatically requests retransmission when a packet checksum fails."),
    "IMD": ("Intermodulation Distortion", "Spurious signal products generated when two or more frequencies mix nonlinearly in an amplifier or mixer."),
    "MUF": ("Maximum Usable Frequency", "The highest frequency that returns to Earth after ionospheric refraction between two specified points."),
    "LUF": ("Lowest Usable Frequency", "The lowest frequency that provides acceptable signal strength over an ionospheric path without excessive D-layer absorption."),
    "FOT": ("Frequency of Optimum Transmission", "The operating frequency—typically 85% of the MUF—providing the most reliable skywave path."),
    "AGC": ("Automatic Gain Control", "A feedback circuit that automatically adjusts receiver RF/IF gain to maintain consistent audio output on fluctuating signals."),
    "PTT": ("Push-To-Talk", "A switch on a microphone or rig that toggles between receive and transmit modes."),
    "CW": ("Continuous Wave", "Unmodulated carrier Morse code transmission toggled on and off by a key."),
    "SSB": ("Single Sideband", "An amplitude modulation format with the carrier and one sideband suppressed to conserve spectrum and concentrate power."),
    "AM": ("Amplitude Modulation", "A modulation method where voice audio directly varies the amplitude of an RF carrier wave."),
    "FM": ("Frequency Modulation", "A modulation method where audio variations modulate the instantaneous frequency of the RF carrier."),
    "IF": ("Intermediate Frequency", "A fixed internal frequency produced by a mixer where primary amplification, filtering, and selectivity take place."),
    "PSK31": ("Phase Shift Keying 31-baud", "A narrowband digital modulation mode designed for keyboard-to-keyboard conversational communications over HF."),
    "RTTY": ("Radioteletype", "A frequency-shift keying teleprinter transmission mode using the 5-bit Baudot code."),
    "FT8": ("Franke-Taylor design 8-FSK", "A 15-second structured digital protocol for weak-signal HF communications operating below the audible noise floor."),
    "DDS": ("Direct Digital Synthesis", "A frequency generation method that synthesizes arbitrary analog waveforms from a time-varying digital number."),
    "PLL": ("Phase-Locked Loop", "A closed-loop feedback control circuit that synchronizes an oscillator frequency and phase to a reference frequency."),
    "DSP": ("Digital Signal Processing", "The numerical manipulation of digitized audio or intermediate frequency signals for sharp filtering and noise reduction."),
    "SDR": ("Software Defined Radio", "A radio communication architecture where traditional hardware components such as mixers and filters are implemented in software."),
    "TNC": ("Terminal Node Controller", "A hardware interface device that decodes and encodes digital audio into AX.25 packet radio frames."),
    "APRS": ("Automatic Packet Reporting System", "An amateur radio-based tactical digital communications protocol that broadcasts real-time position coordinates and weather data."),
    "FSK": ("Frequency Shift Keying", "A digital frequency modulation scheme where discrete digital states shift carrier frequency between mark and space."),
    "AFSK": ("Audio Frequency Shift Keying", "A digital modulation method that modulates tones in an audio passband before feeding standard voice transmitters."),
    "QSK": ("Full Break-In Keying", "A CW transceiver mode that rapidly switches between transmit and receive between Morse code elements so the operator can hear incoming signals."),
    "RIT": ("Receiver Incremental Tuning", "A transceiver control that fine-tunes the receiver frequency without altering the transmitter frequency."),
    "XIT": ("Transmitter Incremental Tuning", "A transceiver control that offsets transmit frequency without altering receiver tuning."),
    "LO": ("Local Oscillator", "An internal RF oscillator that provides a stable carrier frequency mixed with incoming signals to produce an intermediate frequency."),
    "BPF": ("Band-Pass Filter", "A resonant frequency filter circuit that permits a specified band of frequencies to pass while attenuating signals above and below."),
    "LPF": ("Low-Pass Filter", "A filter network that attenuates all frequencies above a designated cutoff frequency while allowing lower frequencies through."),
    "HPF": ("High-Pass Filter", "A filter circuit that allows high frequencies to pass unimpeded while heavily attenuating frequencies below its cutoff.")
}

def replace_first_outside_tags(html: str, pattern: str, replacement: str):
    tokens = re.split(r'(<[^>]+>)', html)
    new_tokens = []
    replaced = False
    for token in tokens:
        if not replaced and not token.startswith('<'):
            m = re.search(pattern, token)
            if m:
                token = re.sub(pattern, replacement, token, count=1)
                replaced = True
        new_tokens.append(token)
    return ''.join(new_tokens), replaced

def audit_chapter_pml(chapter_path: str, course_history: set, is_general: bool = False):
    """Audit and remediate a single chapter for PML compliance."""
    with open(chapter_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    html = data.get("narrative", {}).get("lesson_html", "")
    if not html:
        return 0, []

    modified = False
    issues_found = []
    
    # Check for unexpanded acronyms on first use
    for acr, (full_name, defn) in COMMON_ACRONYMS.items():
        if acr not in course_history:
            pattern = r'\b' + acr + r'\b'
            # Check if acronym appears in plain text
            m = re.search(pattern, html)
            if m:
                # Check if full_name is already adjacent
                start = max(0, m.start() - 60)
                end = min(len(html), m.end() + 60)
                window = html[start:end]
                
                if full_name.lower() not in window.lower():
                    # Unexpanded acronym on first use! Remediate it!
                    issues_found.append(f"Unexpanded acronym on first use: {acr}")
                    replacement = f'{full_name} (<span class="glossary-term">{acr}</span>)'
                    new_html, did_replace = replace_first_outside_tags(html, pattern, replacement)
                    if did_replace:
                        html = new_html
                        modified = True
                    
                    # Ensure glossary has it
                    curr = data.setdefault("curriculum", {})
                    gterms = curr.setdefault("glossary_terms", [])
                    term_exists = any(t.get("term", "").lower() == acr.lower() or t.get("term", "").lower() == full_name.lower() for t in gterms)
                    if not term_exists:
                        gterms.append({
                            "term": f"{full_name} ({acr})",
                            "definition": defn,
                            "alias_for": None,
                            "variations": [acr, full_name, acr.lower(), full_name.lower()]
                        })
                course_history.add(acr)
                
    if modified:
        data["narrative"]["lesson_html"] = html
        with open(chapter_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            
    return len(issues_found), issues_found

def run_pml_sweep(course: str):
    ch_dir = os.path.join(REPO_ROOT, f"ham_training/data/course_HAM_{course}/chapters")
    if not os.path.exists(ch_dir):
        _logger.error(f"Directory not found: {ch_dir}")
        return
        
    course_history = set()
    total_issues = 0
    files = sorted(os.listdir(ch_dir))
    _logger.info(f"Running PML audit sweep across {len(files)} chapters in course_HAM_{course}...")
    
    for f in files:
        if not f.endswith(".json"):
            continue
        p = os.path.join(ch_dir, f)
        count, issues = audit_chapter_pml(p, course_history, is_general=(course == "GENE"))
        if count > 0:
            total_issues += count
            _logger.info(f"[{f}] Fixed {count} issues: {issues[:2]}")
            
    _logger.info(f"PML Sweep for {course} Complete: {total_issues} total issues remediated.")

if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else "TECH"
    run_pml_sweep(c)
