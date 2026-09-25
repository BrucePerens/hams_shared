"""
Apply Maya Lin's Pedagogical Course Review & Top 10 Brain Freeze Analogies
Across Technician (course_HAM_TECH) and General (course_HAM_GENE) Class Chapters.
"""

import os
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_logger = logging.getLogger("apply_maya_reviews")

REPO_ROOT = r"c:\Users\ai\workspace\hams_com"

# Technician Course Analogies
TECH_ANALOGIES = {
    # Ch 37: Capacitive Reactance
    36: {
        "trigger": "The capacitor blocked steady DC completely",
        "insert_after": True,
        "content": (
            '<p>"Think of a capacitor like a crowded club door with a bouncer," Sarah realized, '
            'watching the oscilloscope trace drop to zero on DC. "A steady direct-current crowd tries to push '
            'in all at once, fills the doorway, and gets completely jammed outside—blocked. But alternating current '
            'is like people constantly running in and out before the doorway ever jams, so high-frequency signals '
            'slip right through without friction."</p>'
        )
    },
    # Ch 40: Inductive Reactance & Back-EMF
    39: {
        "trigger": "magnetic field",
        "insert_after": True,
        "content": (
            '<p>"An inductor is like pushing a heavy metal shopping cart," Marcus observed, watching Derek\'s '
            'coil kick back ninety volts into the neon bulb. "When you first push, its mass resists starting '
            'to roll—that\'s inductive reactance opposing sudden AC changes. But once it\'s rolling, DC glides '
            'through effortlessly. And if you try to slam the rolling cart to an instant halt by opening the switch, '
            'its momentum slams forward—the collapsing magnetic field kicks back a high-voltage surge!"</p>'
        )
    },
    # Ch 72: Decibel Math
    71: {
        "trigger": "decibel",
        "insert_after": True,
        "content": (
            '<p>"Decibels are just like the volume slider on my phone," Sarah said, sketching out the math on '
            'her legal pad. "Human hearing and antenna fields don\'t register sound linearly; they hear in multiplicative '
            'steps. Every plus three dB is a doubling of power—two times! Plus ten dB is ten times the punch. And minus '
            'six dB drops you to one-quarter power. Once you visualize the volume slider clicks, the numbers aren\'t '
            'abstract formulas anymore."</p>'
        )
    },
    # Ch 109: Standing Wave Ratio & Reflections
    108: {
        "trigger": "standing wave",
        "insert_after": True,
        "content": (
            '<p>"It\'s exactly like a brass nozzle on a garden hose," Sarah said, pointing to the reflected '
            'power meter. "When the nozzle\'s aperture matches the water flow, fifty ohms meeting fifty ohms, every drop '
            'of energy sprays freely into the air. But if the nozzle is cranked tight or mismatched, the water pressure '
            'kicks violently back down the pipe toward the spigot—that\'s reflected power creating standing waves along the line."</p>'
        )
    },
    # Ch 94: Superheterodyne Mixers & Beat Notes
    93: {
        "trigger": "mixer",
        "insert_after": True,
        "content": (
            '<p>"It\'s just like tuning acoustic guitar strings," Sarah remarked as Tom dialed the local oscillator. '
            '"When two strings are slightly out of tune, you hear that physical \'wah-wah-wah\' acoustic wobble beat in the air. '
            'That audible beat IS the mathematical difference frequency! The mixer takes two high-frequency RF tones and '
            'creates that exact same physical beat note at our receiver\'s intermediate frequency."</p>'
        )
    },
    # Ch 20: Transistors (NPN vs PNP)
    19: {
        "trigger": "transistor",
        "insert_after": True,
        "content": (
            '<p>"An NPN transistor is like a subway turnstile waiting for a forward push at the base," Sarah explained, '
            'checking the multimeter. "You inject positive current into the base to open the main collector floodgate. '
            'A PNP is the reverse mirror—you pull current out through the base to let the current flow."</p>'
        )
    },
    # Ch 88: Single Sideband vs AM Carrier
    87: {
        "trigger": "single sideband",
        "insert_after": True,
        "content": (
            '<p>"The carrier in an AM signal is just the cardboard delivery box," Marcus said, watching the RF spectrum '
            'trace. "The sidebands are the actual letter inside. Why burn a hundred watts transmitting an empty cardboard box '
            'through the sky when the receiver already has its own box generator inside? Single sideband tears off the box '
            'and concentrates every single watt of power directly into our voice!"</p>'
        )
    },
    # Ch 113: Ionosphere Layers (Sun Sponge)
    112: {
        "trigger": "ionosphere",
        "insert_after": True,
        "content": (
            '<p>"The D-layer is the daytime Sun Sponge," Sarah noted, tracing the ionospheric cross-section. "When the '
            'morning sun strikes the upper atmosphere, it charges up a dense molecular sponge that soaks up lower HF frequencies '
            'like eighty and forty meters. But as soon as the sun dips below the horizon, the sponge dissolves into thin air, '
            'clearing the path for our signals to bounce right off the higher F-layer ceiling around the curve of the Earth!"</p>'
        )
    },
    # Ch 29: Wavelength vs Frequency (Accordion)
    28: {
        "trigger": "wavelength",
        "insert_after": True,
        "content": (
            '<p>"Wavelength and frequency are like playing an accordion," Marcus laughed, holding up a meter stick beside '
            'the dipole wire. "When you pump the frequency faster, the electromagnetic wave compresses tight like a closed '
            'accordion—shorter wavelength, smaller antenna! When frequency slows down, the wave stretches wide open into a '
            'giant accordion, demanding a massive wire strung between two trees."</p>'
        )
    },
    # Ch 124: Band Privileges (Highway Lanes)
    123: {
        "trigger": "band",
        "insert_after": True,
        "content": (
            '<p>"The frequency bands are like color-coded lanes on an interstate highway," Sarah told Marcus, reviewing '
            'the laminated band chart. "The two-meter band is our local city commuter lane for handheld walkie-talkies and '
            'emergency repeaters. And ten meters, twenty meters, and forty meters are the worldwide expressways, each with '
            'dedicated speed lanes for Morse code, digital data, and voice."</p>'
        )
    }
}

# General Course Analogies
GENE_ANALOGIES = {
    # Ch 10: Complex Impedance & Phasors
    9: {
        "trigger": "impedance",
        "insert_after": True,
        "content": (
            '<p>"Impedance isn\'t two disconnected mathematical universes," Sarah realized, looking at the complex plane '
            'plot. "Think of resistance like walking straight East on paved city sidewalks. Inductive reactance (+jX_L) '
            'is walking North into tall meadow grass, and capacitive reactance (-jX_C) is walking South into squishy river mud. '
            'Total impedance isn\'t the jagged walk along the streets—it\'s the bird\'s direct diagonal flight path from your origin to where you\'re standing!"</p>'
        )
    },
    # Ch 13: Resonant Circuit Q Factor
    12: {
        "trigger": "bandwidth",
        "insert_after": True,
        "content": (
            '<p>"A high-Q tuned circuit is like a delicate crystal wine glass," Jasmine observed, watching the receiver '
            'filter sweep. "When you tap crystal, it rings on one pure, razor-sharp pitch for seconds—super high quality factor, '
            'ultra-narrow bandwidth that ignores all other notes. A low-Q circuit is a plastic picnic cup: when you tap it, you '
            'get a dull broadband thud across all frequencies at once."</p>'
        )
    },
    # Ch 15: Intermodulation Distortion (IMD)
    14: {
        "trigger": "intermodulation",
        "insert_after": True,
        "content": (
            '<p>"When two strong transmitters overdrive an RF preamplifier, it turns into an echo chamber food fight," '
            'Marcus explained, pointing to the spectral spurs. "The amplifier hits saturation and non-linear distortion bends '
            'the clean sine waves. The signals crash together and generate third-order mixing products—phantom ghost voices that '
            'splatter across neighboring frequencies."</p>'
        )
    },
    # Ch 41: Solar Propagation & Space Weather
    40: {
        "trigger": "solar",
        "insert_after": True,
        "content": (
            '<p>"Solar Flux Index (SFI) is the campfire heat stoking the ionosphere," Sarah told Marcus, checking the '
            'NOAA space weather bulletin. "The hotter the solar campfire burns with high sunspot numbers, the thicker and higher '
            'the ionization ceiling climbs, opening ten and fifteen meters worldwide. But a high K-index geomagnetic storm is '
            'like an icy wind gust that kicks up static turbulence and blows smoke into our receivers."</p>'
        )
    },
    # Ch 21: HF Digital Modes (FT8 / PSK31)
    20: {
        "trigger": "digital",
        "insert_after": True,
        "content": (
            '<p>"Trying to yell voice SSB over a noisy band is like shouting over amplifiers at a rock concert," Jasmine said, '
            'watching the FT8 waterfall decodes. "Your voice gets swallowed by the stadium roar. But slow, narrow digital tones '
            'like PSK31 or structured 15-second FT8 frames are like flashing a laser pointer through the crowd—even when you '
            'can\'t hear a sound, the receiver\'s math reconstructs every single letter cleanly."</p>'
        )
    },
    # Ch 17: Crystal Lattice Filters & Shape Factor
    16: {
        "trigger": "filter",
        "insert_after": True,
        "content": (
            '<p>"Filter shape factor is just like dough cutters in the bakery," Sarah smiled, comparing the filter curve slopes. '
            '"A cheap dull cookie cutter has sloped, squishy sides that tear and leave ragged dough spilling into neighboring cookies. '
            'A precision crystal lattice filter with a 1.5-to-1 shape factor has razor-sharp vertical blades that drop straight down '
            'from six dB to sixty dB, slicing voice sidebands with surgically clean edges."</p>'
        )
    },
    # Ch 18: Receiver Dynamic Range & AGC
    17: {
        "trigger": "gain",
        "insert_after": True,
        "content": (
            '<p>"Receiver Automatic Gain Control is like tactical night-vision goggles," Marcus explained, adjusting the '
            'RF gain control. "When you\'re scanning in the dark, the goggles boost faint light so you can spot distant targets. '
            'But if someone shines a hundred-watt halogen flashlight directly across your field of view, the goggles immediately '
            'throttle down sensitivity to protect your eyes—causing the faint distant stars to temporarily vanish."</p>'
        )
    },
    # Ch 50: Transmission Line Stubs
    49: {
        "trigger": "transmission line",
        "insert_after": True,
        "content": (
            '<p>"A quarter-wave matching stub acts just like noise-canceling headphones," Sarah said, observing the '
            'impedance vector analyzer. "You send an incoming radio wave down an exact quarter-wavelength piece of transmission '
            'line. It hits the shorted end, flips upside down, and travels back to arrive exactly one half-cycle out of phase—perfectly '
            'canceling out the reactive mismatch."</p>'
        )
    },
    # Ch 1: Station Grounding Systems
    0: {
        "trigger": "ground",
        "insert_after": True,
        "content": (
            '<p>"A complete station needs three distinct security guards on duty," Sarah noted, checking the copper bonding bus bar. '
            '"The green third-wire AC safety ground protects you if a 120-volt transformer primary chafes against the metal case. '
            'The heavy eight-foot copper ground rod outside safely dumps skybolt lightning strikes into the earth. And the '
            'low-impedance station RF ground plane stops stray antenna current from biting your lip when you speak into the microphone!"</p>'
        )
    },
    # Ch 24: ARQ vs FEC
    23: {
        "trigger": "error",
        "insert_after": True,
        "content": (
            '<p>"ARQ is like sending registered mail with return receipts," Jasmine explained, watching the packet status '
            'lights. "The transmitter sends packet number one and waits—it refuses to send packet number two until it gets a signed '
            'cryptographic receipt saying packet one arrived uncorrupted. FEC is like a crossword puzzle with redundant clues: even '
            'if rain smudges three letters on the paper, the receiver\'s error-correction code still reconstructs the entire message '
            'without needing a retransmission."</p>'
        )
    }
}

def patch_chapter(chap_path: str, analogy_def: dict):
    if not os.path.exists(chap_path):
        return False, "File not found"

    with open(chap_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    html = data.get("narrative", {}).get("lesson_html", "") or data.get("lesson_html", "")
    if not html:
        return False, "No lesson_html"

    analogy_content = analogy_def["content"]
    # Check if already patched
    key_phrase = analogy_content[10:50]
    if key_phrase in html:
        return False, "Already patched"

    trigger = analogy_def["trigger"].lower()
    lower_html = html.lower()
    
    idx = lower_html.find(trigger)
    if idx == -1:
        # Fallback: append before the closing section or last paragraph
        last_p = html.rfind("</p>")
        if last_p != -1:
            new_html = html[:last_p+4] + "\n" + analogy_content + "\n" + html[last_p+4:]
        else:
            new_html = html + "\n" + analogy_content
    else:
        # Find the end of the enclosing </p>
        end_p = html.find("</p>", idx)
        if end_p != -1:
            end_pos = end_p + 4
            new_html = html[:end_pos] + "\n" + analogy_content + "\n" + html[end_pos:]
        else:
            new_html = html + "\n" + analogy_content

    data.setdefault("narrative", {})["lesson_html"] = new_html
    if "lesson_html" in data:
        data["lesson_html"] = new_html

    with open(chap_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return True, "Successfully patched"

def apply_all():
    tech_count = 0
    gene_count = 0

    # 1. Patch Technician chapters
    tech_dir = os.path.join(REPO_ROOT, "ham_training/data/course_HAM_TECH/chapters")
    for batch_id, analogy_def in TECH_ANALOGIES.items():
        chap_file = os.path.join(tech_dir, f"{batch_id:04d}.json")
        success, msg = patch_chapter(chap_file, analogy_def)
        if success:
            tech_count += 1
            _logger.info(f"[TECH {batch_id:04d}] {msg}")
        else:
            _logger.info(f"[TECH {batch_id:04d}] Skipped/Note: {msg}")

    # 2. Patch General chapters
    gene_dir = os.path.join(REPO_ROOT, "ham_training/data/course_HAM_GENE/chapters")
    for batch_id, analogy_def in GENE_ANALOGIES.items():
        chap_file = os.path.join(gene_dir, f"{batch_id:04d}.json")
        success, msg = patch_chapter(chap_file, analogy_def)
        if success:
            gene_count += 1
            _logger.info(f"[GENE {batch_id:04d}] {msg}")
        else:
            _logger.info(f"[GENE {batch_id:04d}] Skipped/Note: {msg}")

    _logger.info(f"Maya Review Remediation Complete: {tech_count} Tech chapters patched, {gene_count} Gene chapters patched.")

if __name__ == "__main__":
    apply_all()
