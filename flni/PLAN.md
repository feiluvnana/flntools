# PLAN: flni, an incremental game of light, the universe and time

Written 2026-11-18. flni is a desktop incremental ("idle") game in Python, for Windows, macOS
and Linux. You are the first light of a young universe, and you play its whole history: the
first photons, the first stars and the elements they forge, the galaxies and the cosmic web,
the mastery of time itself, and the long fight against entropy at the end of time. The first
click to the finale takes about 260 hours of game time, and an endless mode follows.

The user asked for a game as deep as Antimatter Dimensions, without copying it. So flni
matches that depth (five prestige layers, each with its own currencies, resets, challenges,
a structure to grow, and automation, then seven themed late-game eras), but its mechanics are
its own, drawn from the theme: the spectrum redshifting down, stars living and dying, a
periodic table filled by nucleosynthesis, a map of galaxies, constellations drawn on a random
night sky, parallel timelines, recorded loops, borrowed futures, Maxwell's demons and black
holes. Section 2.1 says, for each kind of depth the genre has, what flni does instead. No
name, text, art, layout or rule is taken from that game or any other.

Paths are relative to this folder, `flni/`, a project of its own in the monorepo beside
`flnd/`. Run every command here.

This page is the specification. A builder makes no design choice that isn't written here. When
this page is silent on a point, the builder picks the plainest option and says so in the
commit message. Numbers marked *tuned* are starting values: the balance step may change them,
through the simulator, and writes the new values back into this page.

## 1. Decisions

| id | decision | why |
| --- | --- | --- |
| G1 | **GUI: PySide6 (Qt 6) Widgets**, through the `PySide6-Essentials` wheel | native windows on all three systems, mature, fast text and painting, a canvas (`QGraphicsView`) for the web, the sky and the periodic table; Essentials leaves out the Qt add-ons we don't use. pygame needs a UI toolkit written by hand; Kivy and Dear PyGui look foreign; Flet downloads a Flutter runtime; Textual is a terminal |
| G2 | **Numbers: our own `Big`**, a float mantissa in [1, 10) and a Python `int` exponent | floats stop at 1.8e308, and the game goes to 1e(1e9). Python ints get slow past a few thousand digits. A pair stays O(1) per operation. No tetration: the finale sits at e1e9, well inside a float `log10` |
| G3 | **The model is pure Python with no Qt**, and deterministic: no clock, no I/O, and randomness only from a seeded generator kept in the save | it can be tested, simulated at 1,000× speed, balanced by a bot, and a night sky can't be re-rolled by reloading |
| G4 | **One fixed tick of 50 ms** (20 per second) for the model; the screen refreshes at 20 Hz and only redraws text that changed | the genre's usual feel; ≤ 1 ms of model time per tick leaves the CPU idle |
| G5 | **Balance through a simulator.** `flni sim` plays the game with a scripted policy and prints the time to each milestone. The pacing table (section 5) is a test | the only honest way to tune 260 hours of content |
| G6 | **Content is data.** Every band, upgrade, star class, element, galaxy, sky rule, study, anomaly, paradox, constant, era stage and achievement is a row in `content/`. The rules read the rows | adding content is adding a row; the UI lists come from the same rows |
| G7 | **Every multiplier is named.** Each source of a multiplier is one entry in `model/effects.py`, and the breakdown panel lists them | players want to see where every × comes from, and it makes balance bugs visible |
| G8 | **Saves are JSON**, versioned, with migrations; written atomically every 30 s and on quit, with 3 rolling backups; export and import as a text string | a player never loses a save, and can move one between machines |
| G9 | **Offline progress** is simulated on load: the time away, in at most 10,000 steps, with the automation running; or, by a setting, banked as Moments (2.7) | the game keeps going when closed |
| G10 | **The automator is a small language of our own**, parsed and run by the model, one statement per tick; never Python, never `eval` | a player's script can't do anything but play the game, and it runs the same in the simulator |
| G11 | **Packaging: PyInstaller `--onedir --windowed`**, built on each OS (a `.app` on macOS, a zipped folder on Windows and Linux) | the same tool `flnd` uses; a one-file Qt binary unpacks 80 MB on every start |
| G12 | **Python ≥ 3.11, ruff as the one checker, `make check` green at every commit** | the repository's rules (`../AGENTS.md`) |
| G13 | **No sign, no negatives:** `Big` is ≥ 0, and a subtraction below 0 raises | currencies never go negative; a negative is always a bug. A Paradox's debt (2.7) is its own field, not a negative amount |
| G14 | **Docs for whoever takes the project over**, in `docs/`, kept in step with the code by `tests/test_docs.py` (section 8), as `flnd/docs` is for flnd | a new owner, person or agent, can read why each part exists and how to change it without reading every module |

## 2. The game

### 2.1 The genre's kinds of depth, and what flni does instead

| the genre's kind of depth | flni's own mechanic | what makes it different |
| --- | --- | --- |
| a chain of producers | the **Spectrum**: 8 bands of light, each redshifting into the band below | the chain is physical: bands also *resonate* with their neighbours when their amounts are close, so you balance the chain, not only buy its top |
| a global speed you buy | **Expansion**: a dial you set, not a purchase | faster light, but every band drains into the one below: a trade-off you tune per run |
| small resets that unlock tiers | **Recombination** | — (the genre's common shape, kept plain) |
| the first prestige | **Ignite**, earning Stellar Mass | no forced cap: you ignite when the gain is worth it |
| a second producer system | **Stars**: you form stars of five classes; each lives, shines and dies | timing and portfolio: long-lived dwarfs or short, bright giants whose deaths give elements and remnants |
| permanent upgrades from a big set | **the Periodic Table**: 92 elements, made by fusion, supernovae and mergers | the "upgrade list" is the table itself; families (columns) share effects; 12 keystone elements open systems |
| challenges | **Strange Skies** (10): runs under changed laws of physics | each changes one physical constant |
| automation | **Probes**, made from silicon, then scripts and recorded **Loops** | automation is an element you make; later you record yourself and replay it |
| a skill tree | **the Cosmic Web**: a fixed map of 84 galaxies, linked by filaments that cost by distance | a spatial tree: which way you grow on the map is the build; clusters give set bonuses |
| random equipment | **Constellations**: each Coalesce shows a new random night sky; you draw a constellation by linking its stars | its effects come from its *shape* (lines, loops, branches, brightness, colour), so you design your own gear |
| formula mini-games | **the Observatory**: 6 Studies of orbits, accretion, decay, tides, pulsar timing, expansion | each asks a different choice of order and timing |
| harder, tiered challenges | **Anomalies** (8, 3 tiers each) | each bends a rule of the earlier layers, such as the Spectrum running upward |
| a time-based prestige | **Rewind**, earning Chronons | time is the layer's whole theme |
| playing several runs at once | **Timelines**: up to 4 parallel copies of the universe below, each with its own choices | no other kind fits: you compare strategies live and keep the best |
| challenges of another kind | **Paradoxes** (8, 3 tiers): start rich with resources borrowed from your future, and repay them | a debt you must clear before the run may end |
| a per-run strategic choice | **the Epoch dial**: the universe's age, favouring early or late layers | — |
| stored and spent time | **Moments**: banked time, spent as warps | — (common in idle games) |
| a pressure that grows | **Entropy**: rises through every late run and weakens everything | you fight it with Demons and Black holes, not with a bigger number |
| an active mini-game | **Maxwell's Demons** sort hot from cold particles to make Negentropy | an animated box, idle once upgraded |
| bursts of speed | **Black holes**: their Hawking radiation speeds time near them | small ones burst bright and vanish, large ones glow faintly for long |
| rule-changing modifiers | **Constants**: 12 sliders (light's speed, gravity, …) on a shared budget | trade-offs on sliders, not toggles |
| themed late-game realms | **the Seven Eras** of the far future, each a run under that era's physics, each opening a mechanic | drawn from cosmology: Inflation, the Dark Age, Reionization, the Degenerate era, the Black-hole era, the Dark era, the Last Light |
| an endless mode | **the Big Bounce**: a new universe with rolled constants, scored | each bounce is a different universe |
| achievements, stats, help, news | 160 achievements, statistics, records, the Codex, the news line, the **cosmic clock** | the top bar shows the universe's age, from its first second to trillions of years |

### 2.2 The layers at a glance

| # | layer | main currency | reset that earns it | opens at |
| --- | --- | --- | --- | --- |
| 1 | Dawn | Light (ℓ) | — | the start |
| 2 | Stars | Stellar Mass (M☉) | Ignite | 1e60 ℓ |
| 3 | Galaxies | Dark Matter (DM) | Coalesce | 1e600 ℓ in one run, and iron made |
| 4 | Chrono | Chronons (⧗) | Rewind | 1e15 DM, and uranium made |
| 5 | Entropy | Negentropy (N) | Crunch | 1e25 ⧗ |
| — | the Eras | — | — | each opened with Negentropy |
| — | the Big Bounce | Echoes | Bounce | the finale |

A reset of layer n resets every layer below it, except what that layer's milestones keep.
Each layer's tab has sub-tabs; the sidebar shows only what is unlocked.

**The cosmic clock.** The top bar shows the universe's age, mapped from total game time: the
first minute is the first second after the beginning; the first Ignite falls near 380,000
years; the first Coalesce near a billion; the first Rewind near 13.8 billion ("now"); the
Entropy layer runs into trillions of years and the Eras far beyond. The map is a fixed,
increasing table in `content/clock.py`. It is flavour only and changes no number.

### 2.3 Layer 1: Dawn

**The Spectrum.** Eight bands. Band 1 (Radio) makes Light; band k makes band k−1 as its
photons redshift. Each band has `bought` (drives cost and Harmonics) and `amount` (drives
production).

| k | band | base cost | cost growth per purchase | unlocked |
| ---: | --- | ---: | ---: | --- |
| 1 | Radio | 10 | ×1.15 | start |
| 2 | Microwave | 100 | ×1.20 | start |
| 3 | Infrared | 1e4 | ×1.25 | start |
| 4 | Visible | 1e6 | ×1.30 | start |
| 5 | Ultraviolet | 1e9 | ×1.35 | Recombination 1 |
| 6 | X-ray | 1e13 | ×1.40 | Recombination 2 |
| 7 | Gamma | 1e18 | ×1.45 | Recombination 3 |
| 8 | Planck | 1e24 | ×1.50 | Recombination 4 |

- **Cost:** `base · growth^bought`; past 150 bought, a further `growth^((bought−150)²/50)`
  (*tuned*). "Buy max" solves the cost in closed form, with no loop.
- **Production:** each unit of band k makes `mult_k` per second of the band below (band 1
  makes Light).
- **Harmonics:** every 25 bought of a band raises its harmonic number h by 1; the band's
  multiplier is ×(1 + h)^1.5. Growth by addition, then a power: the curve is softer early
  and keeps paying late.
- **Resonance:** two neighbouring bands whose amounts are within a factor of 10 of each other
  resonate: both ×2 (*tuned*; upgrades raise it). The Spectrum tab draws a resonance line
  between them. Buying only the top band breaks resonance; balancing keeps it.
- **Expansion:** a dial from 0 to 10 notches; a notch is unlocked by buying it (1e3 ℓ, ×100
  each, *tuned*). At notch e, Light is ×(1.5)^e, and every band loses `2e %` of its amount
  per second to the band below (on top of production). The dial can be turned at any time;
  the best notch depends on the run.
- **Recombination:** needs 10 bought of the highest unlocked band (from the 5th on,
  `10 + 15·(R−4)` Planck, R the Recombination count). Resets Light and the bands; keeps the
  dial's notches and the shop; unlocks the next band; every band ×2 per Recombination.
- **The Dawn shop:** 12 one-time upgrades.

| id | name | cost (ℓ) | effect |
| --- | --- | ---: | --- |
| D1 | First Photon | 1e3 | Radio ×(1 + log10(Light)) |
| D2 | Overtone | 1e8 | Harmonics' power 1.5 → 1.7 |
| D3 | Clear Sky | 1e12 | Recombination needs 2 fewer |
| D4 | Standing Wave | 1e16 | Resonance needs a factor of 30, not 10 |
| D5 | Warm Background | 1e20 | every band ×(1 + bought/100) |
| D6 | Decoupling | 1e24 | Recombination ×2.2 instead of ×2 |
| D7 | Hubble Flow | 1e28 | Expansion drains 1.5% per notch, not 2% |
| D8 | Cascade | 1e32 | each band ×1.05 per band bought of the one above |
| D9 | Afterglow | 1e36 | bands 1–4 ×(Recombinations + 1) |
| D10 | Fine Structure | 1e40 | Harmonics every 20 bought, not 25 |
| D11 | Brightness | 1e44 | Light ×(1 + log10(Light))^0.5 |
| D12 | Protostar | 1e48 | +50% Stellar Mass |

- **The goal:** at 1e60 Light, Ignite opens. Target: the first Ignite after 60–90 minutes.

### 2.4 Layer 2: Stars

- **Ignite** resets Dawn (Light, bands, Recombinations, the dial's setting; the shop only
  until Stars milestone 6) and gives Stellar Mass `floor(10^((log10 ℓ − 60) / 30))` (*tuned*).
  Unspent Mass multiplies every band by ×(1 + M)^0.3.
- **Stars.** Mass is spent to form stars in star slots (3 at first, up to 12 by upgrades).
  Stars live through Ignites: an Ignite resets Dawn, not the sky. Each class:

| class | mass cost | lifetime (game time) | while it shines | when it dies |
| --- | ---: | ---: | --- | --- |
| Red dwarf | 1 | 6 h | Light ×2, fuses H → He | a white dwarf |
| Sunlike | 10 | 1 h | Light ×10, fuses up to O (8) | a white dwarf, and C, N, O |
| Blue giant | 1e3 | 10 min | Light ×1e3, fuses up to Si (14) | a neutron star, and Na to Si |
| Supergiant | 1e6 | 2 min | Light ×1e6, fuses up to Fe (26) | a supernova: Co to Sr (27–38), and a neutron star |
| Hypergiant | 1e10 | 30 s | Light ×1e10 | a black hole, and a burst of every element up to Fe |

  (*tuned*.) The class multipliers of the stars shining multiply together. A star's yield of
  each element grows with its mass cost paid (a later upgrade lets you overpay a star for
  more yield, at a shorter life). Stars can be set to re-form on death (from Probe 3).
- **Remnants** stay after their star dies, and no reset below Galaxies clears them:
  white dwarfs (each Light ×1.1, capped by slots ×10); neutron stars (each +1% game speed;
  two can be **merged**, which spends them and makes elements 39–92 by the r-process);
  black holes (kept for the Entropy layer; until then each band ×1.5).
- **The Periodic Table:** 92 elements, each with an amount. An element's effect grows with
  `log10(1 + amount)`. Elements of one family (a column of the table) share a kind of effect:

| family | effect kind |
| --- | --- |
| alkali metals (H counts here) | Light × |
| alkaline earths | band costs grow slower |
| transition metals | star slots, lifetimes, yields |
| post-transition and metalloids | Probe speed |
| nonmetals | Resonance strength |
| halogens | Expansion's drain lowered |
| noble gases | Stellar Mass × |
| lanthanides | the Observatory's speed |
| actinides | Chronons × |

  Twelve **keystones** also open a system the first time one is made: He (the Stars shop),
  C (star re-forming), O (the 4th star slot), Si (**Probes**, the autobuyers), Fe (the way to
  Galaxies), Ni (neutron-star merging), Ag (Strange Skies 6–10), Au (the Galaxies shop),
  Pt (Probe scripts become scripts), Pb (white dwarfs uncapped), U (the way to Chrono),
  Pu (the last Paradox). The table is drawn as a periodic table; an element not yet made is
  dark, with its source in the tooltip.
- **Probes** (autobuyers): one per band, plus Recombination, the dial, star forming and
  Ignite. Each starts at one action every 3 s, halved by silicon thresholds and Mass
  upgrades, down to one tick. Each can be off, buy one, or buy max; the Ignite Probe takes an
  amount, a time, or "×N of the last Ignite's gain".
- **Stars milestones** by Ignite count: 1 band Probes 1–2; 2 bands 3–4; 3 bands 5–8;
  4 the dial Probe; 5 the Recombination Probe; 6 the Dawn shop is kept; 8 start with 4
  Recombinations; 12 the Ignite Probe; 20 Recombinations are kept.
- **The Stars shop** (opened by He): 16 one-time upgrades and 4 repeatables, bought with Mass
  (*tuned*).
- **Strange Skies** (10 challenges): an Ignite run under a changed constant, ending at 1e60
  Light; the reward is permanent.

| id | name | the change | reward |
| --- | --- | --- | --- |
| K1 | Opaque | no Resonance | Resonance ×3 instead of ×2 |
| K2 | Frozen | the dial is stuck at 0 | Expansion's Light ×1.6 per notch |
| K3 | Young | only bands 1–4 | bands 1–4 ×10 |
| K4 | Heavy | band k costs ×1e3^k more | cost growth −0.02 for every band |
| K5 | Dim | Light production ^0.75 | Stellar Mass ^1.05 |
| K6 | Lonely | stars give no Light while shining | each shining star also ×(slots used) |
| K7 | Fast | game speed ×5, and every band loses 10%/s | lifetimes ×2 |
| K8 | Silent | no Harmonics | Harmonics every 15 bought |
| K9 | Uphill | Recombination resets the dial's notches too | notches cost ×10 instead of ×100 |
| K10 | Inverse | band 8 makes Light and band 1 makes nothing | each band ×(the band above's harmonic + 1) |

- **The goal:** 1e600 Light in one run, with iron made, opens Galaxies. Target: 18 h.

### 2.5 Layer 3: Galaxies

- **Coalesce** resets Dawn and Stars (bands, stars, remnants except black holes, elements
  except what milestones keep) and gives Dark Matter `floor(10^((log10 ℓ − 600) / 250))`
  (*tuned*). Unspent Dark Matter multiplies Stellar Mass by ×(1 + DM)^0.25.
- **Galaxies milestones** by Coalesce count: 1 keep the Stars milestones; 2 keep the Strange
  Skies; 3 keep the Stars shop; 5 keep H to Fe; 8 the Coalesce Probe; 12 keep remnants;
  20 keep every element at 1%; 30 Stellar Mass is gained each second at 1% of an Ignite.
- **The Cosmic Web:** a map of 84 galaxies in 12 named clusters, laid out once from a fixed
  seed in `content/web.py`, the same for every player. Filaments join near neighbours.
  - You start at the Home galaxy. Linking a galaxy costs Dark Matter by the filament's length
    and the galaxy's distance in hops from Home (*tuned*).
  - Each galaxy's type gives its effect: **spiral** (Light ×), **elliptical** (Stellar Mass
    ×), **lenticular** (star lifetimes ×), **irregular** (a strong effect and a cost, such as
    "Light ×1e20, but Resonance off"), **dwarf** (cheap, small, and a cheaper next link),
    **starburst** (stars form at half cost), **quasar** (×2 to every linked neighbour's
    effect), **void edge** (nothing, but it opens a far cluster).
  - Owning every galaxy of a cluster gives the cluster's bonus.
  - Unlinking is free on the next Coalesce. 5 presets, named; a preset exports as `web1:` and
    the galaxy ids.
- **Constellations.** Each Coalesce shows a new night sky, rolled from the save's generator
  (G3): 12 to 20 stars, each with a brightness and a colour (red, white, blue). You draw a
  constellation by linking up to 7 of its stars with lines. Its effects come from its shape:

| what is read | from | gives |
| --- | --- | --- |
| size | the number of stars (2–7) | the strength of every effect |
| brightness | the sum of the stars' brightness | a Light multiplier |
| line | how straight its longest path is | a Stellar Mass multiplier |
| loop | whether it closes, and the loop's area | a Dark Matter multiplier |
| branch | the number of stars with 3 or more lines | star slots |
| colour | the mix of red, white and blue | which of three secondary effects (lifetimes, Resonance, Probe speed) |

  The rules are exact formulas in `model/skies.py`, written out in `docs/layers/galaxies.md`.
  A drawn constellation is named by you and kept in the **Sky Book** (30 entries). Three are
  set in the sky at once (up to 6 from the Galaxies shop); a newly set one takes a full
  Coalesce to shine. A constellation can be released for **Stardust** (a material of the
  Galaxies shop), by its size and brightness.
- **The Observatory** (opened at Coalesce 3): six Studies, formula mini-games each with its
  own currency ρ and a few variables bought with ρ. Studies run all the time, offline too,
  and no reset touches them. A study's best ρ (its record r) boosts the main game. One study
  is active at a time and runs at full speed; the others run at 10%. Each is opened with Dark
  Matter.

| id | name | ρ grows by | the choice it asks for | its record boosts |
| --- | --- | --- | --- | --- |
| S1 | Orbit | `ρ̇ = a1 · a2 · (1 + b·|sin t|)` | a "kick" at the orbit's far point doubles b for one orbit, by hand until an upgrade does it | bands ×(1 + log10 r)² |
| S2 | Accretion | `ρ̇ = c1 · R²`, `Ṙ = c2 / (1 + R)` | c2 grows the disc R fast early; c1 pays only once R is large | Stellar Mass ×(1 + log10 r) |
| S3 | Decay chain | a 3-step chain `x3 → x2 → x1 → ρ`, each step losing 1%/s | the top step is slow to pay, and the decay punishes waiting | Recombination ×+log10(r)/10 |
| S4 | Tides | `ρ̇ = d1 · (1 + cos(2π t / T))` | T is a variable: short tides are steady, long ones high | Dark Matter ×(1 + log10 r)^0.5 |
| S5 | Pulsar timing | `ρ̇ = e1 · p(n)`, p(n) the n-th prime, n a variable | n's cost grows like p(n): a step function to climb | Harmonics ×+log10(r)/20 |
| S6 | Expansion | `ρ ← ρ · (1 + q·dt)`, plus `ρ̇ = f1` | a budget split between f1 (now) and q (later), changed once a minute | every study ×(1 + log10 r)^0.25 |

  A study's variables are *linear* (value = level), *doubling* (2^level) or *step* (a fixed
  amount more every 10 levels), with geometric costs in ρ (*tuned*). Each study has 5
  milestones that add a variable or a term.
- **Anomalies** (8, 3 tiers each): Coalesce runs where a rule of the layers below is bent.
  Each tier raises the goal (1e800, 1e1500, 1e3000 Light, *tuned*) and the reward.

| id | name | the bent rule | reward (per tier) |
| --- | --- | --- | --- |
| A1 | Blueshift | the Spectrum runs upward: band k makes band k+1, band 8 makes Light | each band ×(tier + 1) per harmonic |
| A2 | Missing Mass | Dark Matter and Stellar Mass give no multipliers | Dark Matter ×10^tier |
| A3 | Great Attractor | the dial is stuck at 10 | Expansion drains 0.5% less per notch per tier |
| A4 | Starless | no stars can form | remnants ×(1 + tier) |
| A5 | Lightless Web | the Web's galaxies give nothing | a link costs ×0.8^tier |
| A6 | Noise | every 30 s, a random band (from the generator) makes nothing for 30 s | Resonance ×(1 + tier/2) |
| A7 | Cold Dark | elements give nothing | element effects ×(1 + tier/4) |
| A8 | Short Night | the run must end within 10 minutes of game time | the Coalesce Probe acts every tick, and +1 Sky Book set slot per tier |

- **The Galaxies shop** (opened by gold): 20 upgrades, bought with Dark Matter and Stardust
  (*tuned*).
- **The goal:** 1e15 Dark Matter, with uranium made, opens Chrono. Target: 80 h.

### 2.6 Layer 4: Chrono

- **Rewind** resets Dawn, Stars and Galaxies (except milestones, the Observatory and the Sky
  Book) and gives Chronons `floor(10^((log10 DM − 15) / 6))` (*tuned*).
- **Chrono milestones** (12) by Rewind count: keep the Web's links, keep the Anomalies, keep
  every element at 10%, start with 1e10 Dark Matter, and so on (*tuned*, in
  `content/chrono.py`).
- **Timelines.** Branch the current run into up to 4 parallel timelines (2 at first, more from
  the Chrono shop). Each is a full copy of the layers below, ticking side by side, with its
  own choices: its dial, its Web, its constellations, its Probes or script, its Anomaly.
  - The timeline shown is the *focused* one and runs at full speed; the others run at 50%
    (*tuned*).
  - A Rewind gives Chronons from the best timeline, plus 25% of what the others would give.
  - A timeline can be **pruned** (it ends and gives nothing) or **merged** into another: the
    receiver takes the larger of each element's amount, and the pruned one is gone.
  - Cost: each timeline is ticked by the model, so `model.tick` with 4 timelines is measured
    by its own bench (section 6).
- **Loops.** Record a run: every action you take, with its run time and the state's key
  numbers, is kept as a **Loop**. A Loop replays itself in later runs, acting when the run
  time or the numbers it recorded are reached (whichever the Loop's setting says). A Loop
  converts to a script (2.6, below) for editing. 6 Loop slots.
- **Paradoxes** (8, 3 tiers each): enter with resources borrowed from your future: you start
  the run with a share of your best Light, Mass and Dark Matter (10%, 1%, 0.1% by tier), and
  owe twice it back. The debt is repaid from what the run earns; the run can't end (no Rewind,
  no Coalesce) until it is paid, and an unpaid run can only be abandoned, which costs a tenth
  of your Chronons. Each Paradox adds a rule (for example "your borrowed Light shrinks 1% a
  second", "every band you buy is bought in all timelines"). Rewards: Chrono shop upgrades and
  Loop slots.
- **The Epoch dial.** Set once per run, from 1 (early universe) to 9 (late): early epochs
  multiply Dawn and Stars and divide Galaxies and Chrono, late ones the reverse, by
  `×10^(±(5 − epoch)·k)` per layer (*tuned*). Changing it in a run costs a Rewind.
- **Moments.** One Moment is banked for each real minute, online or offline (offline by the
  setting of G9), up to a cap (1,000, raised by the shop). A **warp** spends Moments to run
  that many minutes of game time at once, in offline-style steps.
- **Scripts.** The automation's last form. Up to 10 scripts of up to 10,000 characters, each
  with a text editor and a block editor that round-trip exactly. Probes and Loops keep
  working beside them.

```text
script  := stmt*
stmt    := command NEWLINE
         | "if" cond "{" stmt* "}" ["else" "{" stmt* "}"]
         | "while" cond "{" stmt* "}"     | "until" cond "{" stmt* "}"
         | "wait" (cond | duration)        | "repeat" "{" stmt* "}"
         | "#" any-text NEWLINE
command := "recombine" | "ignite" | "coalesce" | "rewind" | "crunch"
         | "dial" INT | "epoch" INT | "form" CLASS ["x" INT] | "merge"
         | "sky" ("set" INT+ | "clear") | "web" ("link" ID+ | "preset" INT)
         | "skies" INT | "anomaly" INT INT | "paradox" INT INT | "leave"
         | "study" INT | "timeline" ("branch" | "focus" INT | "prune" INT | "merge" INT INT)
         | "loop" ("play" | "stop") INT | "warp" INT | "probe" NAME ("on" | "off")
         | "notify" STRING
cond    := value OP value ("and" cond | "or" cond)?
value   := NUMBER | duration | currency | element | "pending" "(" reset ")" | "runtime"
         | "debt" | "entropy" | "epoch"
OP      := "<" | "<=" | ">" | ">=" | "==" | "!="
NUMBER  := 12 | 1.5e300 | 1e1e6          duration := 5s | 2m | 1h
```

  A script is parsed when saved; an error names its line and column and the script won't
  run. Running: one statement per tick (10 with a Chrono shop upgrade); `wait` holds; a
  statement that is refused (`Refused`) is skipped with a note in the script's log. A loop
  with no `wait` runs at most 100 statements per tick. The model runs scripts, so the
  simulator runs them too.
- **The Chrono shop:** 24 upgrades, bought with Chronons (*tuned*).
- **The goal:** 1e25 Chronons opens Entropy. Target: 130 h.

### 2.7 Layer 5: Entropy

- **Crunch** resets everything up to Chrono (the Sky Book, the Observatory, Loops and scripts
  are kept) and gives Negentropy `floor(log10(⧗) − 24)` (*tuned*): a slow, countable currency.
- **Entropy.** From now on, every run has an Entropy S, rising by `dS/dt = 1 + log10(1 + run
  time)` (*tuned*), and every currency's gain is ×1/(1 + S/100). Two things lower it:
- **Maxwell's Demons.** A box of particles, hot and cold, drawn moving; a demon at the gate
  lets hot ones one way and cold ones the other. The sorted fraction lowers S and makes
  Negentropy over time. By hand, you click the gate open; upgrades give demons a speed and an
  accuracy, and from the third upgrade a demon plays alone. The model keeps only the numbers
  (sorted fraction, demon speed and accuracy); the moving particles are the UI's drawing of
  them.
- **Black holes.** The black holes from Hypergiants, plus ones you make by feeding Stellar
  Mass. A black hole of mass m radiates with power ∝ 1/m² and lives ∝ m³ (*tuned* units):
  its radiation speeds game time by `×(1 + power)` while it lives. A small one is a bright,
  short burst; a large one a faint, long glow. Feeding a black hole grows it, slowing its
  burst and lengthening its life. Up to 6.
- **Constants.** 12 sliders on a shared budget of points (points from Negentropy milestones);
  each slider runs from −5 to +5 and changes an earlier rule. Changing them needs a Crunch.

| constant | turned up | turned down |
| --- | --- | --- |
| c, light's speed | bands produce faster | Expansion drains less |
| G, gravity | stars form cheaper | stars live longer |
| ħ, the quantum | Harmonics come sooner | Resonance is wider |
| α, fine structure | element effects stronger | elements made faster |
| Λ, dark energy | Dark Matter × | Web links cheaper |
| k, Boltzmann | Demons faster | Entropy rises slower |
| e, the charge | Probes faster | scripts run more per tick |
| m_p, the proton | Stellar Mass × | Ignite needs less |
| H₀, Hubble | the dial's Light per notch × | the dial's drain per notch less |
| t_P, Planck time | game speed × | Moments' cap × |
| θ, the weak angle | Paradox debt lower | Paradox borrowing larger |
| Ω, the curvature | Chronons × | timelines run faster |

  Each step is a factor (`×2^steps` or its inverse, *tuned*). A preset of sliders can be saved
  (3).
- **The Entropy shop:** 20 upgrades, bought with Negentropy.
- **Entropy milestones** (6): budget points, and the Eras' opening.

### 2.8 The Seven Eras

The late game. Each Era is a run of the whole universe under that era's physics, opened in
order with Negentropy. Each has 6 to 8 stages, each a goal reached inside it. Every stage
gives a reward, and finishing an Era opens one new mechanic for the whole game.

| era | its physics | the mechanic it opens |
| --- | --- | --- |
| Inflation | everything ×1e10, but a run lasts 60 s of game time | **the Inflaton:** every run's first minute is ×(stages done) |
| the Dark Age | stars give no Light | **primordial black holes:** each run starts with black holes |
| Reionization | bands 1–4 are locked | **quasar beacons:** a quasar's ×2 reaches two hops |
| the Degenerate era | no new stars; only remnants | **cooling:** white dwarfs grow stronger with their age |
| the Black-hole era | only black holes make anything | **evaporation:** a black hole's last burst gives Moments |
| the Dark era | Entropy rises ×10 | **the third gate:** demons sort three kinds, and lower Entropy faster |
| the Last Light | every currency loses 1% of its logarithm a second | the finale |

- **The Last Light:** opened when the other six Eras are done. Its decay is stopped one
  currency at a time by five **Lamps** (Light, Stellar Mass, Dark Matter, Chronons,
  Negentropy), lit in order by pouring Negentropy in; each lit Lamp also adds a constant's
  slider range by ±2. With the five Lamps lit, the decay is gone, and the goal is 1e(1e9)
  Light inside the Last Light.
- **The finale:** 1e(1e9) Light in the Last Light. The universe relights: an animation of the
  sidebar's layers lighting one by one, the cosmic clock running back to the first second,
  then the run's statistics. Target: 260 h.

### 2.9 The Big Bounce

The endless mode after the finale. A Bounce starts a new universe with everything reset but
the Eras' mechanics, the Sky Book and the records, and with 3 constants rolled from the
generator (each ±1 to ±3, shown before the Bounce, accepted or rerolled once). Echoes are
given by how fast the run reached e1e9 Light; Echo upgrades only speed up the next run. A
local board keeps the 20 best runs with their rolled constants.

### 2.10 What every layer shares

- **Achievements:** 160, in rows of 8 (20 rows), each ×1.02 to every band; a full row gives a
  named reward. 20 are secret (hidden until done). Shown as a grid of tiles with tooltips.
- **Statistics:** play time in total and per layer; fastest of each reset; every currency's
  best; resets counted; Moments banked and spent; the universe's age.
- **Records:** the last 10 of each reset (Ignite, Coalesce, Rewind, Crunch): its time, its gain
  and its gain per minute.
- **The breakdown:** for each currency, the list of multipliers that make its rate (G7).
- **The Codex:** a help page per mechanic (about 45), opened when the mechanic is; a `?`
  beside each panel opens its page. Each page has one line of real cosmology behind the
  mechanic.
- **Hints:** the first 30 minutes show short hints at the next useful action, one at a time,
  off by a setting.
- **The news line:** one line of original flavour text at the bottom, every 20 s, from 300
  lines that grow with the layers reached. UI-only randomness.
- **Notations:** scientific (`1.23e456`), engineering (`123e454`), standard (`1.23 Qa`, up to
  1e303, then scientific), letters (`1.23 ab`), logarithm (`e456.09`), and stellar
  (magnitudes: `m −1140.2`, the astronomers' scale, brighter is lower); past 1e1e6 every
  notation shows `e1.23e6`.
- **Hotkeys:** `1`–`8` buy max of a band, `M` buy max of all, `R` Recombine, `I` Ignite,
  `G` Coalesce, `W` Rewind, `X` Crunch, `[` `]` turn the dial, `P` all Probes on or off,
  `Esc` close a dialog. Remappable.
- **Settings:** notation and decimals, theme (Night, Dawn, Nebula, Redshift, Mono, High
  contrast), autosave interval, a confirmation per reset, offline progress (play, bank as
  Moments, off), refresh rate (10, 20, 30 Hz), hotkeys, hints, saves.
- **Saves:** 3 slots, export to the clipboard or a file, import, backups.

## 3. The architecture

```text
flni/
  pyproject.toml   PySide6-Essentials, msgspec; dev: pytest, ruff; dist: pyinstaller
  Makefile         setup test lint check bench sim pace dist clean
  AGENTS.md        flni's own rules (section 9), beside the repository's ../AGENTS.md
  PLAN.md          this page
  README.md
  docs/            the project's docs (section 8)
  flni/
    __init__.py    the version
    __main__.py    python -m flni: opens the window, or `sim` (section 5)
    num.py         Big, and its maths
    notation.py    Big -> text, per notation
    model/         pure: no Qt, no clock, no I/O; randomness only from state.rng
      state.py     the save's structs (msgspec): every layer's state, timelines, the rng
      game.py      Game: tick(dt), the actions, Refused
      effects.py   every named multiplier, and the breakdown
      universe.py  one universe's layers 1–3: the unit a timeline copies
      dawn.py      the Spectrum, Harmonics, Resonance, the dial, Recombination
      stars.py     star classes, lives, deaths, remnants, merging
      elements.py  the Periodic Table: amounts, families, keystones
      galaxies.py  Coalesce, the Web, Anomalies
      skies.py     night skies, drawing, reading a constellation's shape, the Sky Book
      studies.py   the study engine and the six Studies
      chrono.py    Rewind, timelines, Paradoxes and debt, the Epoch dial, Moments
      loops.py     recording, replaying, converting to a script
      entropy.py   Crunch, Entropy, Demons, black holes, Constants
      eras.py      the Seven Eras, the Lamps, the finale
      bounce.py    the Big Bounce
      time.py      game speed: neutron stars, black holes, Eras, warps
      script/      lexer.py parser.py run.py blocks.py (the block form <-> the text)
      offline.py   the offline steps
      save.py      encode, decode, versions, migrations, export strings
      rng.py       a PCG32 generator: seed, next, kept in the state
    content/       data rows only
      dawn.py stars.py elements.py web.py skies.py studies.py galaxies.py chrono.py
      entropy.py eras.py bounce.py clock.py achievements.py codex.py news.py
    sim/
      policy.py    the scripted players (greedy, steady)
      run.py       the simulator and the pacing report
      scripts/     the flni scripts `steady` loads in the late game
    app/           Qt: imports model/, content/, num.py, notation.py
      main.py      QApplication, the window, the timers
      theme.py     colours, fonts and spacing as tokens; the QSS made from them
      store.py     save files on disk: slots, atomic writes, backups
      window.py    the sidebar, the top bar with the cosmic clock, the news line, the tabs
      widgets.py   BigLabel, BuyButton, Meter, Tile, Tooltip, Dial
      canvas.py    QGraphicsView scenes: the Spectrum's resonance lines, the Web, the night
                   sky, the Periodic Table, the Demons' box, the black holes
      editor.py    the script editors: text (with highlighting and errors) and blocks
      tabs/        one module per tab
  tests/
    test_num.py test_notation.py test_rng.py test_save.py test_rules_*.py test_offline.py
    test_skies.py test_timelines.py test_loops.py test_script_*.py test_content.py
    test_pacing.py test_layers.py test_app_smoke.py test_docs.py
    data/saves/    a saved example of every save version
    bench/         tick, tick with 4 timelines, buy max, offline, script, frame
```

**The rules between parts**, checked by `tests/test_layers.py` reading every file's imports:
- `num.py` and `notation.py` import only the standard library.
- `model/` imports `num.py`, `content/` and the standard library (never `time`, `random` or
  file I/O), plus `msgspec` in `state.py` and `save.py`.
- `content/` imports only `num.py` and the standard library.
- `sim/` imports `model/`, `content/` and `num.py`.
- `app/` imports everything above, plus PySide6. Nothing else imports PySide6.

### 3.1 `Big`

- `Big(m: float, e: int)`, normalised to `1 ≤ m < 10` (or `m = 0, e = 0` for zero).
  Immutable, `__slots__`.
- Operations: `+ − * /` with `Big`, `int` and `float`; `**` with a `float` exponent;
  `log10() -> float`; `sqrt`; `floor`; comparisons; `max`/`min`; `Big.parse(str)`; `str()` as
  `"1.234e5678"` (the save form).
- `a + b` where the exponents differ by more than 17 returns the larger one unchanged.
- `a − b` below zero raises `ValueError` (G13).
- Speed goal (bench `num.ops`): an add or a multiply ≤ 1.5 µs on an Apple M-class CPU.

### 3.2 The model

- `Game(state)` holds the state and offers the actions, one method each: buying (bands, dial
  notches, shop upgrades, stars, Web links, Study variables, black-hole feeding, Demon
  upgrades), resets (`recombine`, `ignite`, `coalesce`, `rewind`, `crunch`, `bounce`),
  entering and leaving (Strange Skies, Anomalies, Paradoxes, Eras), and setting (the dial,
  the Epoch, presets, constellations drawn and set, timelines branched, focused, pruned and
  merged, Loops, scripts, Probes, the active Study, Constants, warps).
  - An action that can't be done raises `Refused(reason)` and changes nothing.
  - The UI, Probes, Loops, scripts and the simulator use only these actions.
- **A universe** (`universe.py`) is the state of layers 1–3; `state.timelines` holds 1 to 4 of
  them, and Chrono and Entropy sit above them. Every rule of layers 1–3 takes the universe it
  acts on.
- `game.tick(dt)`: `dt` is scaled by the game speed (`time.py`), then, in order: each
  timeline's universe (the Spectrum from the top band down, each band fed by the band
  above's amount before this tick; stars; the Web's effects), the Studies, Entropy and the
  Demons, black holes, Probes, Loops, scripts, achievements, the unlock checks.
- `game.rate(currency) -> Big`, `game.breakdown(currency) -> list[(name, factor)]`,
  `game.pending(reset) -> Big`.
- Effects are recomputed only when something they depend on changes; each module marks its
  effects dirty, and `effects.py` caches the products.
- Speed goals: `model.tick` (a late-game state, one timeline) ≤ 1 ms; `model.tick4` (four
  timelines) ≤ 3 ms.

### 3.3 Offline progress and Moments

- The app stores the wall-clock time with every save. On load, `elapsed = now − saved`
  (ignored if negative; capped at 30 days).
- By the setting: *play* runs `offline.run(game, elapsed)`: `n = min(10_000,
  ceil(elapsed / 0.05))` ticks of `elapsed / n`, with the automation running, and shows what
  changed; *bank* turns it into Moments (one per minute, up to the cap; the rest is played);
  *off* drops it. Before Chrono opens, *bank* acts as *play*.
- It runs on a `QThread` behind a progress dialog; the window takes input after it ends.
  Goal: ≤ 0.5 s for 10,000 steps.

### 3.4 Saves

- `state.py` defines the save as msgspec structs, with `Big` encoded as its string and the rng
  as its two integers.
- The file is JSON: `{"v": <version>, "at": <unix seconds>, "state": {...}}`.
- `save.migrate(data)` runs the version's migrations in order; a test loads a saved example of
  every past version from `tests/data/saves/`.
- **On disk** (`app/store.py`): `QStandardPaths.AppDataLocation/save-<slot>.json`; write to
  `.tmp` and `os.replace`; before each write, rotate `save-<slot>.1..3.json`. A file that
  doesn't decode is moved to `save-<slot>.bad-<stamp>.json`, and the newest good backup loads,
  with a message saying so.
- **Export string:** `flni1:` + base64url(zlib(JSON)). Import checks the prefix and version, and
  asks before overwriting the slot.

### 3.5 The app

- One `QMainWindow`. Two `QTimer`s on the main thread: the tick (50 ms; calls `game.tick` with
  the real elapsed time, clamped to 1 s; a longer gap goes through offline progress) and the
  refresh (20 Hz by default).
- **The refresh** asks each visible widget to update; a widget sets its text only when the
  string changed. A hidden tab updates nothing. A minimised window stops the refresh, not the
  tick. Animations (the Demons' box, black holes, the night sky's twinkle) run only while
  their tab is visible, at the refresh rate.
- Speed goals: bench `ui.frame` ≤ 4 ms for the busiest tab; idle on the Dawn tab, ≤ 3% of one
  core.
- **The theme** (`app/theme.py`): colour tokens per theme (Night: a deep indigo sky; each
  layer has its accent from the theme: Dawn the spectrum's colours, Stars gold, Galaxies
  violet, Chrono cyan, Entropy ember red); spacing on a 4 px grid; corners of 6 px; tabular
  figures for numbers. The QSS is built from the tokens; no colour is written anywhere else.
- **The layout:**

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ ℓ 1.23e48 Light (+4.56e46/s)   M☉ 3.2e4   ◉ age 212,000 yr   Ignite +12 M☉│
├────────────┬──────────────────────────────────────────────────────────────┤
│ Dawn     ● │  Spectrum          Expansion ◀ 4 ▶ (Light ×5.1, drain 8%/s) │
│  Spectrum  │  ┌──────────────────────────────────────────────────────────┐│
│  Shop      │  │ 8 Planck   ×12.0    19       h 0  [  3/25   cost 2.2e47 ]││
│ Stars      │  │ 7 Gamma    ×40.1    3.1e3    h 1 ≈ resonating with 6     ││
│  Sky       │  │ …                                                         ││
│  Table     │  │ 1 Radio    ×1.2e9   1.4e40   h 9  [ 25 → h10 cost 1.1e45]││
│  Probes    │  └──────────────────────────────────────────────────────────┘│
│ Galaxies🔒 │  Recombination 6: needs 40 Planck (19)   [Recombine R]       │
│ Codex  Ach │                                                [Buy max M]   │
│ Stats  Set │                                                              │
├────────────┴──────────────────────────────────────────────────────────────┤
│  “Every photon you see is older than the stars that sent it.”             │
└───────────────────────────────────────────────────────────────────────────┘
```

  A locked layer shows a lock and, in its tooltip, what opens it. A new unlock shows a dot on
  its sidebar entry until visited. With several timelines, a row of timeline tabs sits under
  the top bar. The window is resizable down to 1024 × 640.
- **Accessibility:** every action has a keyboard path; contrast ≥ 4.5:1 in every theme (the
  High contrast theme ≥ 7:1); no meaning carried by colour alone (a constellation's star
  colours also show as letters on request).

## 4. Content counts

| kind | count |
| --- | ---: |
| Spectrum bands | 8 |
| Dawn shop | 12 |
| star classes, remnant kinds | 5, 3 |
| elements (families, keystones) | 92 (9, 12) |
| Stars shop | 16 + 4 |
| Strange Skies | 10 |
| galaxies, clusters, galaxy types | 84, 12, 8 |
| constellation shape rules | 6 |
| Studies (each 4–6 variables, 5 milestones) | 6 |
| Anomalies (× 3 tiers) | 8 |
| Galaxies shop | 20 |
| Paradoxes (× 3 tiers) | 8 |
| Chrono shop | 24 |
| Constants | 12 |
| Entropy shop | 20 |
| Eras (stages) | 7 (48) |
| milestones (Stars, Galaxies, Chrono, Entropy) | 9 + 8 + 12 + 6 |
| achievements | 160 |
| Codex pages | ~45 |
| news lines | 300 |

Every row of `content/` has an id, a name, a one-line description (≤ 90 characters), and its
numbers. `test_content.py` checks unique ids, unique names within a kind, that every reference
(a cluster's galaxies, an achievement's condition, a keystone's system) exists, that the Web
is connected from Home, that all 92 elements have a source, and that every mechanic has a
Codex page.

## 5. The simulator and pacing

- `python -m flni sim --policy steady --until finale` plays a new game with the model alone and
  prints each milestone's game time. It steps at `dt = 1 s` up to Galaxies and `dt = 10 s`
  after (*tuned* so Dawn's timings stay within 5% of `dt = 0.05`).
- **Policies** (`sim/policy.py`) are functions `(game) -> None` called every simulated step,
  using only the actions of 3.2. Late in the game, `steady` drives the game by loading flni
  scripts from `sim/scripts/`, the same language players use.
  - `greedy`: buys the best ratio of gain to cost, resets when the pending gain doubles the
    currency.
  - `steady`: a careful player: keeps neighbouring bands resonating, turns the dial to the
    best notch every minute, forms stars by a fixed rota, Ignites at 2× pending, does the
    Strange Skies in order, links the Web by a fixed route, draws its constellation by a fixed
    rule (the brightest 7 stars as a loop), switches Studies every 10 minutes, runs 2
    timelines, sets the obvious Constants, and takes the Eras in order.
- **The pacing table** (game time, `steady`):

| milestone | target | band |
| --- | ---: | ---: |
| first Recombination | 3 min | ±50% |
| first Ignite | 75 min | ±30% |
| silicon (Probes) | 3 h | ±30% |
| all 10 Strange Skies | 10 h | ±30% |
| first Coalesce | 18 h | ±30% |
| all 6 Studies | 45 h | ±30% |
| all Anomalies at tier 3 | 70 h | ±30% |
| first Rewind | 80 h | ±30% |
| scripts | 85 h | ±30% |
| first Crunch | 130 h | ±30% |
| six Eras done | 220 h | ±25% |
| the finale | 260 h | ±25% |

- `tests/test_pacing.py` runs up to the first Ignite (in `make check`, ≤ 10 s of wall time).
  `make pace` runs the whole table (≤ 10 minutes) and fails outside a band; run it in every
  step that adds or changes content, from the layer that step touches.
- A *wall* (more than 2 h of game time before Galaxies, or 6 h after, with no new purchase or
  reset under `steady`) fails `make pace` too.

## 6. Speed

| bench | what | goal |
| --- | --- | --- |
| `num.ops` | 1M adds and 1M multiplies of `Big` | ≤ 1.5 µs each |
| `model.tick` | one tick of a saved late-game state, one timeline | ≤ 1 ms |
| `model.tick4` | the same with four timelines | ≤ 3 ms |
| `model.buy_max` | buy max on all 8 bands, 10,000 times | ≤ 20 µs each |
| `skies.read` | reading the shape of a 7-star constellation | ≤ 50 µs |
| `script.run` | a 200-line script, 10,000 ticks | ≤ 50 µs per tick |
| `offline.10k` | 10,000 offline steps of a late-game state | ≤ 0.5 s |
| `ui.frame` | one refresh of the busiest tab, offscreen | ≤ 4 ms |
| `app.start` | process start to the window shown | ≤ 1.5 s |

`make bench` runs them; `ARGS=--gate` fails on a regression over 15% against
`tests/bench/gates.json`; `ARGS=--record` stores a new baseline.

## 7. Packaging

- `make dist` runs PyInstaller with `--onedir --windowed --name flni` on the OS it runs on:
  `dist/flni.app` on macOS, `dist/flni/` on Windows and Linux, then zips it.
- An icon: a star with eight rays, one per band's colour, drawn as SVG in `flni/app/icons/`
  and exported to `.icns` and `.ico` by a script in `tools/`.
- Size goal: ≤ 90 MB unzipped; the unused Qt modules are excluded in the spec.
- The macOS build is unsigned; the README says how to open it the first time. Signing and
  notarising are out of scope.

## 8. Docs

flni's docs are for the person or agent who handles the project next: why each part exists,
how the parts fit, and how to change one safely. The in-game Codex (2.10) is for players and
is a separate thing; `docs/scripts.md` is the one page written for both.

### 8.1 The pages

```text
docs/
  README.md        the index: where to start, one line per page
  architecture.md  the parts (section 3), one tick traced from the timer to the screen, the
                   import rules and why, what runs on which thread
  model.md         Game, its actions and Refused, universes and timelines, the tick order,
                   effects and their cache, game speed, the rng
  layers/          one page per layer: dawn.md stars.md galaxies.md chrono.md entropy.md
                   eras.md bounce.md. Each gives every mechanic of the layer, its formulas
                   (written out, with the content ids they read), what each reset keeps,
                   and the unlock conditions
  content.md       how to add or change a row of each kind in content/: the fields, the
                   checks test_content.py makes, and a worked example (a new Anomaly)
  balance.md       the simulator, the two policies, the pacing table, how to read
                   `make pace`, how to tune a number and record it, what a wall is
  numbers.md       Big: its form, its limits, where precision is lost; the notations
  saves.md         the file format, versions, how to write a migration and its saved
                   example, backups, the export string
  scripts.md       the script language: the grammar, every command and value, the limits per
                   tick, the errors, examples; for players and developers both
  ui.md            the window, the theme tokens, the widgets, the canvases, the refresh rule,
                   how to add a tab, accessibility
  performance.md   the benches, their goals and last numbers, `make bench ARGS=--gate`, how a
                   speed-up is proven
  packaging.md     `make dist` on each OS, what is excluded, how to test a build by hand
  glossary.md      every word the game and these pages use, one meaning each (band, bought,
                   amount, harmonic, reset, layer, run, universe, timeline, era, stage, …)
```

### 8.2 The rules

- **Docstrings:** a module docstring of 1 to 3 lines names the `docs/` page that explains it.
  A public name has a summary line; then `Args:`, `Returns:` and `Raises:` where they add to
  the name and type. Comments cover only what the code can't say.
- **A behaviour change updates its page in the same commit.** A new mechanic adds its section
  to its layer's page; a new content kind adds its part to `content.md`; a tuned number
  updates the formula's page.
- **The docs describe the code as it is.** A part not built yet is named as not built, never
  described as working.
- Plain English, lines of at most 100 characters, every formula with its symbols defined.

### 8.3 What `tests/test_docs.py` checks

It parses the code and never imports it, so it runs in well under a second.
- every module, and every public class and function, has its docstring, and a module's
  docstring names an existing `docs/` page;
- every relative link in `docs/` and the README resolves, and a `#fragment` names a heading;
- every `python` block in `docs/` runs;
- every layer in `content/` has its page in `docs/layers/`, and every content id a page cites
  exists;
- `docs/scripts.md` names every command and value kind the parser accepts, and every example
  in it parses;
- every bench in `tests/bench/` is listed in `performance.md`;
- every page is in the README's index, and every term in `glossary.md` is used in `docs/`.

A gap fails with one line naming the file and the rule; there is no allowlist.

## 9. flni's own rules (its `AGENTS.md`)

The repository's `../AGENTS.md` holds the shared rules (commits, git, privacy, models, ruff,
line length). flni adds:
- **The model is pure** (G3): no Qt, no `time`, no `random`, no files in `model/`, `content/`,
  `num.py` or `notation.py`; randomness only from `state.rng`. `test_layers.py` checks it.
- **Every number lives in `content/`**, except the formulas' shapes in `model/`. A tuned number
  changes in one place.
- **Every multiplier is a named effect** (G7). No anonymous `*=` in a rule.
- **Content changes run `make pace`**, and the commit quotes the milestones that moved.
- **The UI changes state only through `Game`'s actions.**
- **Saves never break:** a change to `state.py` adds a migration and a saved example in
  `tests/data/saves/`, in the same commit.
- **Scripts are data:** never Python, never `eval`; the parser rejects anything outside the
  grammar.
- **The app runs no subprocess**, and no text from content or a script goes to a shell.
- **Original only:** no name, text, art or exact rule taken from another game. A new mechanic
  is drawn from the theme (light, the universe, time) first.
- **Docs move with the code** (section 8): a behaviour change updates its `docs/` page in the
  same commit, and `test_docs.py` stays green.
- No `print` outside `__main__.py`'s `sim` output.

## 10. The steps

Each step is one commit or a few, with `make check` green after each, and `make pace` green
from step 5 on. Each step that adds a mechanic also writes its section of its layer's page in
`docs/layers/` (8.2), so steps 4 to 15 each grow one of those pages.

0. **The project.** `pyproject.toml`, `uv.lock`, the Makefile, `AGENTS.md`, a README stub,
   `test_layers.py`, `test_docs.py`, `docs/README.md` and `docs/glossary.md`, an empty window
   that opens and closes (`test_app_smoke.py` with `QT_QPA_PLATFORM=offscreen`). Commit
   `chore:start the flni project`.
1. **Numbers and chance.** `num.py`, `notation.py` (with property tests against `int` and
   `fractions` below 1e300, fixed cases above), `rng.py`; the `num.ops` bench;
   `docs/numbers.md`.
2. **The model's core.** `state.py`, `game.py` with ticks and `Refused`, `universe.py` (one
   timeline), `effects.py` and the breakdown, `time.py` (speed only), `save.py` with version 1
   and the export string, `offline.py`; `docs/architecture.md`, `docs/model.md`,
   `docs/saves.md`.
3. **The app's shell.** The window, the themes, the sidebar and tabs, the top bar with the
   cosmic clock, the news line, settings, save slots, autosave, backups, import and export,
   the offline dialog, hotkeys, the Codex viewer, hints; `docs/ui.md`.
4. **Dawn.** The Spectrum, Harmonics, Resonance, the Expansion dial, Recombination, the shop;
   its tab with the resonance lines; `docs/layers/dawn.md`, `docs/content.md`.
5. **The simulator.** `sim/`, both policies, `test_pacing.py` to the first Ignite,
   `make pace`; tune Dawn into its band; `docs/balance.md`.
6. **Stars.** Ignite, star classes and slots, lives and deaths, remnants and merging, the
   Periodic Table (its canvas, families, keystones), Probes, milestones, the Stars shop;
   pace to "silicon".
7. **Strange Skies.** The 10 challenges; pace to "first Coalesce".
8. **Around the game.** Achievements (the first 60), statistics, records, the breakdown panel,
   notations, the Codex pages and news lines so far.
9. **Galaxies.** Coalesce, milestones, the Cosmic Web (its map in `content/web.py`, the
   canvas, presets, strings), the night sky and constellations (drawing, reading, the Sky
   Book), the Galaxies shop.
10. **Galaxies' depth.** The Observatory's six Studies, the 8 Anomalies with 3 tiers; pace to
    "first Rewind".
11. **Chrono.** Rewind, milestones, timelines (branch, focus, prune, merge; the `model.tick4`
    bench), the Epoch dial, Moments and warps, the Chrono shop.
12. **Loops and scripts.** Recording and replaying Loops, the script language (lexer, parser,
    runner), both editors, Loop-to-script conversion, `sim/scripts/`, the 8 Paradoxes;
    `docs/scripts.md`; pace to "first Crunch".
13. **Entropy.** Crunch, Entropy, the Demons (model and the box's animation), black holes,
    Constants, the Entropy shop and milestones.
14. **The Eras.** Inflation, the Dark Age, Reionization, the Degenerate, Black-hole and Dark
    eras, with their stages and mechanics; pace to "six Eras done".
15. **The Last Light and after.** The Last Light and its Lamps, the finale, the Big Bounce; the
    rest of the achievements, Codex pages and news; pace to "the finale".
16. **Balance.** `make pace` over the whole table, both policies, no wall; write the tuned
    numbers back into this page and into the layers' pages.
17. **Speed.** Every bench of section 6 at its goal, or the reason written down;
    `docs/performance.md`.
18. **Packaging and hand-over.** `make dist` on macOS, Windows and Linux (checked by hand on
    each), the icon, the README (how to play, how to build, where saves live),
    `docs/packaging.md`. Last, a fresh agent that didn't build flni reads every `docs/` page
    against the code and fixes what is wrong or missing.

## 11. Open decisions

Each has a recommendation, which the plan follows until the user says otherwise.

- **F1. One active Study, the others at 10%.** Recommended, so switching Studies is a choice.
- **F2. Unfocused timelines at 50%.** Recommended: several timelines pay, but not 4× for free.
- **F3. Offline capped at 30 days.** Recommended; a longer absence gives the same as 30 days.
- **F4. No cloud saves.** Recommended for now: export strings move a save between machines.
- **F5. No sound.** Recommended for now; a later step can add a few soft cues behind a setting.
- **F6. English only.** All text is in `content/`, so a translation can come later.
- **F7. 260 hours to the finale.** Recommended: the depth asked for. The pacing table is the one
  place to make it shorter, and the simulator shows the effect.
