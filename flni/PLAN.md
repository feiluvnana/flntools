# PLAN: flni, an incremental game

Written 2026-11-18. flni is a desktop incremental ("idle") game in Python, for Windows, macOS
and Linux. It aims at the depth of the deepest games of the genre: five prestige layers, each
with its own currencies, producer chains, challenges, trees and automation, a late game of
seven themed realms, and an endless mode. The first click to the finale takes about 260 hours
of game time.

The user asked for "everything that Antimatter Dimensions has". So flni has a system of every
*kind* that game has (section 2.1 lists them side by side), and each one is designed afresh:
the theme, the names, the numbers, the rules, the text and the look are flni's own. No name,
text, art, layout or exact rule is taken from that game or any other.

Paths are relative to this folder, `flni/`, a project of its own in the monorepo beside
`flnd/`. Run every command here.

This page is the specification. A builder makes no design choice that isn't written here. When
this page is silent on a point, the builder picks the plainest option and says so in the
commit message. Numbers marked *tuned* are starting values: the balance step may change them,
through the simulator, and writes the new values back into this page.

## 1. Decisions

| id | decision | why |
| --- | --- | --- |
| G1 | **GUI: PySide6 (Qt 6) Widgets**, through the `PySide6-Essentials` wheel | native windows on all three systems, mature, fast text and painting, a canvas (`QGraphicsView`) for the trees and the grid; Essentials leaves out the Qt add-ons we don't use. pygame needs a UI toolkit written by hand; Kivy and Dear PyGui look foreign; Flet downloads a Flutter runtime; Textual is a terminal |
| G2 | **Numbers: our own `Big`**, a float mantissa in [1, 10) and a Python `int` exponent | floats stop at 1.8e308, and the game goes to 1e(1e9). Python ints get slow past a few thousand digits. A pair stays O(1) per operation. No tetration: the finale sits at e1e9, well inside a float `log10` |
| G3 | **The model is pure Python with no Qt**, and deterministic: no clock, no I/O, and randomness only from a seeded generator kept in the save | it can be tested, simulated at 1,000× speed, balanced by a bot, and a crystal drop can't be re-rolled by reloading |
| G4 | **One fixed tick of 50 ms** (20 per second) for the model; the screen refreshes at 20 Hz and only redraws text that changed | the genre's usual feel; ≤ 1 ms of model time per tick leaves the CPU idle |
| G5 | **Balance through a simulator.** `flni sim` plays the game with a scripted policy and prints the time to each milestone. The pacing table (section 5) is a test | the only honest way to tune 260 hours of content |
| G6 | **Content is data.** Every producer, upgrade, node, trial, study, crystal effect, reagent, perk, law, realm stage and achievement is a row in `content/`. The rules read the rows | adding content is adding a row; the UI lists come from the same rows |
| G7 | **Every multiplier is named.** Each source of a multiplier is one entry in `model/effects.py`, and the breakdown panel lists them | players want to see where every × comes from, and it makes balance bugs visible |
| G8 | **Saves are JSON**, versioned, with migrations; written atomically every 30 s and on quit, with 3 rolling backups; export and import as a text string | a player never loses a save, and can move one between machines |
| G9 | **Offline progress** is simulated on load: the time away, in at most 10,000 steps, with the automation running; or, by a setting, stored in the Reservoir (2.6) | the game keeps going when closed |
| G10 | **The automator is a small language of our own**, parsed and run by the model, one instruction per tick; never Python, never `eval` | a player's script can't do anything but play the game, and it runs the same in the simulator |
| G11 | **Packaging: PyInstaller `--onedir --windowed`**, built on each OS (a `.app` on macOS, a zipped folder on Windows and Linux) | the same tool `flnd` uses; a one-file Qt binary unpacks 80 MB on every start |
| G12 | **Python ≥ 3.11, ruff as the one checker, `make check` green at every commit** | the repository's rules (`../AGENTS.md`) |
| G13 | **No sign, no negatives:** `Big` is ≥ 0, and a subtraction below 0 raises | currencies never go negative; a negative is always a bug |
| G14 | **Docs for whoever takes the project over**, in `docs/`, kept in step with the code by `tests/test_docs.py` (section 8), as `flnd/docs` is for flnd | a new owner, person or agent, can read why each part exists and how to change it without reading every module |

## 2. The game

### 2.1 Every kind of system, and flni's own version of it

| kind of system (as the genre knows it) | flni's version | layer |
| --- | --- | --- |
| a chain of 8 producers, each making the one below | the Lights | Glow |
| a global speed you buy | Tempo | Glow |
| a small reset that unlocks the next producer | Kindle | Glow |
| a larger reset that makes the global speed stronger | Flare | Glow |
| spending lower producers to boost the top one | Offering | Glow |
| the first prestige, with a forced cap before it is broken | Nova, and the Ceiling | Nova |
| a prestige shop of one-time and repeatable upgrades | the Stardust grid | Nova |
| challenges with restrictions | 12 Trials | Nova |
| breaking the cap, with its own upgrades | Breach | Nova |
| harder challenges after the cap is broken | 8 Breach Trials | Nova |
| a second producer chain fed by the prestige currency | the Forges, making Radiance | Nova |
| a resource that copies itself | Bloom (Petals and Seeds) | Nova |
| autobuyers for every purchase and reset | autobuyers, with intervals and modes | Nova on |
| milestones that keep things through resets | milestones on every layer | all |
| the second prestige | Rift | Rift |
| a big tree with exclusive paths, bought with a currency of its own | the Chart, bought with Theorems | Rift |
| a third producer chain giving free speed | the Rift Engines, making Drift | Rift |
| challenges with several completion tiers | 12 Rift Trials, 5 tiers each | Rift |
| a weakened run that earns a special currency | Haze, earning Mist and Silt | Rift |
| formula mini-games, each with its own currency | the Observatory's 6 Studies | Rift |
| the third prestige, with random equipment | Collapse, dropping Crystals | Lattice |
| equipment slots with interactions | the 7 × 7 Lattice, with neighbour rules | Lattice |
| breaking equipment down, and crafting from the parts | Essence and Distilling (12 reagents) | Lattice |
| a perk tree of quality-of-life | 40 Lattice perks | Lattice |
| periodic bursts of game speed | the Pulsar | Lattice |
| storing time to spend later | the Reservoir | Lattice |
| a scripting automator with a text and a block editor | scripts | Lattice |
| the fourth prestige | Ascend | Zenith |
| rule-changing modifiers | 12 Laws | Zenith |
| themed late-game realms, each with its own mechanic | 7 Wardens | Zenith |
| a final, hardest realm that ends the game | the Hollow and its 5 Anchors | Zenith |
| achievements with small rewards, some secret | 160 achievements | all |
| statistics and the last runs of each reset | Statistics and Records | all |
| a line of jokes and lore | the news line, 300 lines | all |
| an in-game help for every mechanic | the Codex | all |
| number notations, themes, hotkeys, confirmations | Settings | all |
| an endless mode after the end | Afterglow | — |

### 2.2 The theme

A dark, empty sky. You make the first mote of light, and light makes more light. Each layer is
a larger scale of light: from motes to stars, from stars to the rifts between them, from rifts
to the lattice that holds the sky, and at last to the zenith, where you write the laws light
obeys and face the wardens who keep the dark. The finale is the moment the whole sky is lit.

### 2.3 The layers at a glance

| # | layer | main currency | reset that earns it | opens at |
| --- | --- | --- | --- | --- |
| 1 | Glow | Lumen (L) | — | the start |
| 2 | Nova | Stardust (SD) | Nova | 1e50 L (the Ceiling) |
| 3 | Rift | Void (V) | Rift | 1e1000 L in one run |
| 4 | Lattice | Shards (Sh) | Collapse | 1e15 V |
| 5 | Zenith | Aether (Ae) | Ascend | 1e30 Sh |
| — | Afterglow | Glimmer | Glimmer | the finale |

A reset of layer n resets every layer below it, except what that layer's milestones, perks or
laws keep. Each layer's tab has sub-tabs; the sidebar shows only what is unlocked.

### 2.4 Layer 1: Glow

**The Lights.** Light 1 makes Lumen; Light k makes Light k−1. Each Light has `bought` (drives
cost and Facets) and `amount` (drives production; it grows from the tier above).

| k | name | base cost | cost growth per purchase | unlocked |
| ---: | --- | ---: | ---: | --- |
| 1 | Mote | 10 | ×1.15 | start |
| 2 | Wisp | 100 | ×1.20 | start |
| 3 | Candle | 1e4 | ×1.25 | start |
| 4 | Lamp | 1e6 | ×1.30 | start |
| 5 | Lantern | 1e9 | ×1.35 | Kindle 1 |
| 6 | Beacon | 1e13 | ×1.40 | Kindle 2 |
| 7 | Pharos | 1e18 | ×1.45 | Kindle 3 |
| 8 | Halo | 1e24 | ×1.50 | Kindle 4 |

- **Cost:** `base · growth^bought`; past 150 bought, a further `growth^((bought−150)²/50)`
  (*tuned*); past 1e308 the growth itself rises ×1.01 per purchase (the Strain). "Buy max"
  solves the cost in closed form, with no loop.
- **Production:** each unit of Light k makes `mult_k · tempo` per second of the tier below.
- **Facets:** every 25 bought of a Light multiplies that Light by ×3.
- **Tempo:** costs 1e3 L, ×10 per purchase (*tuned*). Each purchase multiplies Tempo by the
  Tempo step, 1.125 at the start. Tempo multiplies every Light's production.
- **Focus:** points `F = floor(log10(best Lumen this Nova) / 4)`. Each point on a Light
  multiplies it by ×1.4. Points can be moved at most once every 30 s. 3 presets.
- **Kindle:** needs 10 bought of the highest unlocked Light (from Kindle 5 on,
  `10 + 15·(K−4)` Halos). Resets Lumen to 10, the Lights and Tempo; keeps Focus, Flares and
  the shop; unlocks the next Light; every Light ×2 per Kindle.
- **Flare:** needs `80 + 60·Fl` Halos bought (Fl = Flares; past 50 Flares the step grows
  ×1.02 per Flare, *tuned*). Resets Kindles too. Each Flare adds +0.02 to the Tempo step
  (with diminishing returns past 20 Flares: `+0.02 / (1 + (Fl−20)/40)`).
- **Offering:** opens with the Halo. It sets the Lights 1–7 `amount` to 0 (`bought` stays) and
  raises the Halo's Offering multiplier to `(1 + log10(Mote amount) / 12)^2.5` if that is
  higher than the current one. Reset by Kindle and Flare.
- **The Lumen shop:** 12 one-time upgrades.

| id | name | cost (L) | effect |
| --- | --- | ---: | --- |
| U1 | Steady Hands | 1e3 | Motes ×(1 + log10(Lumen)) |
| U2 | Cut Glass | 1e8 | Facets ×3.5 instead of ×3 |
| U3 | Dry Tinder | 1e12 | Kindle needs 2 fewer |
| U4 | Clear Mind | 1e16 | a Focus point every 3.5 orders of magnitude, not 4 |
| U5 | Warm Air | 1e20 | every Light ×(1 + bought/100) |
| U6 | Long Wick | 1e24 | Kindle ×2.2 instead of ×2 |
| U7 | Bright Edge | 1e28 | Focus ×1.5 per point instead of ×1.4 |
| U8 | Kinship | 1e32 | each Light ×1.05 per Light bought of the tier above |
| U9 | Afterimage | 1e36 | Lights 1–4 ×(Kindle count + 1) |
| U10 | Lens Array | 1e40 | Facets every 20 bought instead of 25 |
| U11 | Gravity | 1e44 | Lumen ×(1 + log10(Lumen))^0.5 |
| U12 | Spark of Nova | 1e48 | +50% Stardust |

- **The Ceiling:** before Breach, Lumen stops at 1e50, and the only way on is a Nova. Target:
  the first Nova after 60–90 minutes.

### 2.5 Layer 2: Nova

- **Nova** resets Glow (Lumen, Lights, Tempo, Kindles, Flares, Focus, the Offering, the shop)
  and gives Stardust: 1 per Nova before Breach (×multipliers); after Breach,
  `floor(10^((log10 L − 50) / 25))` (*tuned*).
- **Starlight:** unspent Stardust multiplies every Light by ×(1 + SD)^0.4.
- **The Stardust grid:** 16 one-time upgrades in a 4 × 4 grid, each costing 1 to 1e9 SD; a full
  row gives a bonus (row 1: Tempo step +0.01; row 2: Kindles cost 5 fewer; row 3: Stardust ×2;
  row 4: start each Nova with 1e10 Lumen). Then 5 repeatables: Stardust ×2 (cost ×10 each),
  Starlight exponent +0.05 (5 times), autobuyer speed ×2 (8 times), Offering exponent +0.1
  (10 times), Focus points +1 (10 times).
- **The Constellation:** a tree of 36 nodes bought with Stardust (costs 1 to 1e40, *tuned*), in
  three branches of 10 and 6 capstones: **Flow** (production), **Forge** (costs and resets),
  **Fate** (Stardust, offline speed, autobuyers). Of each branch's fourth row only one node in
  all three branches can be owned (two after Rift milestone 6, three after a Lattice perk).
  Respec on the next Nova, for free. 3 presets.
- **Milestones** by Nova count: 1 Mote and Wisp autobuyers; 2 Candle and Lamp; 3 Lights 5–8;
  4 Tempo autobuyer; 5 Kindle autobuyer; 7 Flare autobuyer; 8 the shop is kept; 10 Offering
  autobuyer; 12 start with 4 Kindles; 15 the Nova autobuyer; 25 Kindles are kept; 40 Flares
  are kept.
- **Autobuyers:** each starts at one action every 3 s; each upgrade halves it, down to one tick.
  Each can be off, buy one, or buy max. The Kindle and Flare autobuyers take a limit ("only up
  to N"); the Nova autobuyer takes an amount, a time, or "×N of the last Nova's gain".
- **Trials** (12): a Nova run with a restriction, ending at 1e50 Lumen. Rewards are permanent.

| id | name | restriction | reward |
| --- | --- | --- | --- |
| T1 | Dim | no Facets | Facets every 20 bought (with U10, every 16) |
| T2 | Short Wick | only Lights 1–4 | Lights 1–4 ×10 |
| T3 | Drift | production ×1/(1 + run time / 60 s) | Lights ×(1 + log10(run time in s)) |
| T4 | Tithe | each purchase raises every other Light's cost ×1.1 | cost growth −0.02 for every Light |
| T5 | Silence | no Focus | Focus points ×1.5 |
| T6 | Still Air | no Kindle | Kindle ×3 instead of ×2 |
| T7 | Eclipse | Motes make nothing for 10 s after any purchase | Motes ×(Halo bought + 1) |
| T8 | Mirror | Lumen production ^0.75 | Stardust ^1.05 |
| T9 | Ballast | Tempo does nothing | Tempo step +0.015 |
| T10 | Alternate | the even Lights make nothing | the even Lights ×(odd Lights bought)^0.5 |
| T11 | Brittle | buying a Light empties the Lights below it | Offering exponent ×1.3 |
| T12 | Ration | at most 600 purchases in the run | the Nova autobuyer acts every tick |

- **Breach:** opens when all 12 Trials are done. It lifts the Ceiling: Lumen grows past 1e50 and
  Stardust follows the formula above. **Breach upgrades:** 8 repeatables bought with Stardust
  (Stardust gain ×2, Tempo cost growth −1 down to ×4, Forge multiplier ×2, and so on, *tuned*).
- **Breach Trials** (8): harder runs with goals from 1e300 to 1e1000 Lumen, opened one at a time
  by Lumen thresholds. Rewards are new effects, such as "Kindle multiplier ^1.1" or "Tempo
  step is raised by Forge 8's amount^0.01".
- **The Forges:** 8 tiers bought with Stardust. Forge 1 makes Radiance; Forge k makes Forge k−1.
  Tier k unlocks at Lumen 1e60, 1e120, 1e200, 1e300, 1e450, 1e650, 1e900, 1e1250. Radiance
  multiplies every Light by Radiance^6 (*tuned*; Chart nodes raise the exponent). A Nova
  resets Radiance and the Forges' amounts, not their bought counts.
- **Bloom:** bought for 1e140 SD. Petals start at 1 and grow by
  `petals ← petals · (1 + chance)^(dt / interval)` up to the cap of 1e308 (deterministic: the
  expected value, not a dice roll). Upgrades, with Stardust: chance (1% → 100%), interval
  (1 s → 0.05 s), cap (raised by Chart nodes past 1e308). Petals multiply Radiance by
  `(1 + log10 petals)^2`. At the cap a **Seed** can be made: Petals go back to 1 and each Seed
  counts as a free Flare.
- **The goal:** 1e1000 Lumen in one run opens the Rift. Target: 18 h.

### 2.6 Layer 3: Rift

- **Rift** resets Glow and Nova (except what milestones keep) and gives Void
  `floor(10^((log10 L − 1000) / 400))` (*tuned*). Void unspent multiplies Radiance by
  `(1 + V)^0.3`.
- **Rift milestones** by Rift count: 1 keep Nova milestones; 2 keep the Trials; 3 keep the
  Stardust grid; 4 keep Breach; 5 start with 1e10 SD; 6 the second capstone slot; 8 the rule
  automator (below); 10 keep the Breach Trials; 15 keep the Constellation; 20 Stardust is
  gained each second as if a Nova happened (at 1%); 30 keep the Forges' bought counts;
  50 Bloom is kept.
- **Void upgrades:** 20 (12 one-time, 8 repeatable, *tuned*).
- **The Chart:** a tree of 60 nodes bought with Theorems (θ), drawn on the canvas.
  - **Theorems** are bought with Lumen (1e1000, ×1e500 each), Stardust (1e20, ×1e10 each) or
    Void (1, ×2 each); a Chart node makes them over time.
  - **The shape:** row 1, 4 common nodes. Row 2, a three-way split (**Light**, **Forge**,
    **Drift**): one path, two after Rift Trial 11. Row 3, a three-way split (**Active**:
    rewards clicking and short runs; **Passive**: rewards long runs; **Idle**: rewards time
    away): one path only. Rows 4–8: 30 nodes in paths that join. Row 9: 12 key nodes, one per
    Rift Trial; one key at a time. Row 10: 4 capstones.
  - Respec on the next Rift, refunding every Theorem. 6 presets, named; a preset exports as
    `chart1:` and the node ids, and imports from that string.
- **The Rift Engines:** 8 tiers bought with Void. Engine 1 makes Drift; Engine k makes Engine
  k−1. Drift gives free Tempo purchases: `floor(log(Drift) / log(1.33))` (the 1.33 lowered by
  Chart nodes to 1.25). A Rift resets Drift, not the Engines' bought counts.
- **Rift Trials** (12, each with 5 tiers): entered through its key node. Each tier raises the
  goal (for example 1e1500, 1e2500, 1e4000, 1e6000, 1e9000 Lumen) and the reward's strength.
  Each restriction is new: Rift Trial 1 "Every Light costs Lumen and Stardust"; 4 "Tempo is
  replaced by Drift alone"; 7 "Lights 1–4 produce Light 8"; 12 "the Chart's paths are all
  forbidden". The full list of 12 restrictions and rewards is written in step 10 into
  `content/rift.py`, following one rule: a restriction removes or bends exactly one earlier
  system.
- **Haze:** opened for 5,000 θ. A Haze run is a Rift run in which every multiplier m becomes
  `10^(log10(m)^0.75)` and Tempo's effect is halved. A Rift from a Haze run gives Mist:
  `(log10(best Lumen) / 400)^1.5`, if higher than the Mist owned (Mist only rises).
  - Mist makes Silt every second, `Silt += Mist · dt`; each Silt threshold (×1.35 each) is a
    free Flare.
  - **Mist upgrades:** 12, bought with Silt; 4 repeatable (Mist ×3, Silt threshold ×0.99,
    Theorems ×2, Haze penalty from 0.75 to 0.8), 8 one-time (for example "Lumen ^1.01",
    "Theorems are made from Mist").
- **The Observatory** (opened at Rift 3): six Studies, formula mini-games each with its own
  currency ρ and a few variables bought with ρ. Studies run all the time, offline too, and no
  reset touches them. A study's best ρ (its record r) boosts the main game. One study is
  active at a time and runs at full speed; the others run at 10%. Each is opened with Void.

| id | name | ρ grows by | the choice it asks for | its record boosts |
| --- | --- | --- | --- | --- |
| S1 | Pendulum | `ρ̇ = a1 · a2 · (1 + b·|sin t|)` | a "push" at the swing's peak doubles b for one swing, by hand until a Void upgrade does it | Lights ×(1 + log10 r)² |
| S2 | Spiral | `ρ̇ = c1 · r_s²`, `ṙ_s = c2 / (1 + r_s)` | c2 grows r_s fast early; c1 pays only once r_s is large | Stardust ×(1 + log10 r) |
| S3 | Cascade | a 3-tier chain `x3 → x2 → x1 → ρ`, each tier losing 1%/s | the top tier is slow to pay and the decay punishes waiting | Kindle ×+log10(r)/10 |
| S4 | Tide | `ρ̇ = d1 · (1 + cos(2π t / T))` | T is a variable: short tides are steady, long ones high | Void ×(1 + log10 r)^0.5 |
| S5 | Sieve | `ρ̇ = e1 · p(n)`, p(n) the n-th prime, n a variable | n's cost grows like p(n): a step function to climb | Facets ×+log10(r)/20 |
| S6 | Fold | `ρ ← ρ · (1 + q·dt)`, plus `ρ̇ = f1` | a budget split between f1 (now) and q (later), changed once a minute | every study ×(1 + log10 r)^0.25 |

  A study's variables are *linear* (value = level), *doubling* (2^level) or *step* (a fixed
  amount more every 10 levels), with geometric costs in ρ (*tuned*). Each study has 5
  milestones that add a variable or a term.
- **The rule automator** (Rift milestone 8): up to 24 rules, `WHEN <condition> [AND
  <condition>] DO <action>`, edited as a table of drop-downs. It is replaced by scripts (2.7),
  and its rules convert into a script on that day.
- **The goal:** 1e15 Void opens the Lattice. Target: 80 h.

### 2.7 Layer 4: Lattice

- **Collapse** resets Glow, Nova and Rift (not the Studies, not Mist) and gives Shards
  `floor(10^((log10 V − 15) / 5))` (*tuned*), one perk point, and a Crystal drop.
- **Crystals.** A drop is rolled from the save's seeded generator (G3):
  - its **kind**: Amber, Cobalt, Verdant, Rose, Onyx or Pearl; later Ghost (perk 30) and
    Prism (Warden of Glass);
  - its **rarity**, 0–100%, biased low (a square of a uniform roll, raised by perks);
  - its **level**, from the Collapse's Void: `floor(log10(V)^0.6)` (*tuned*);
  - **1 to 4 effects**, drawn from its kind's pool of 5 or 6 (for example Amber: Lights ×,
    Tempo step +, Facets every −1, Offering ^; Cobalt: Stardust ×, Forges ×, Breach upgrades
    cheaper). An effect's strength grows with level and rarity.
  - With perk 12, a Collapse offers 3 drops and you keep one; with perk 25, a filter keeps or
    dissolves drops on its own (rules per kind: minimum rarity, wanted effects, a score).
  - Each crystal is drawn by `QPainter` from its kind (shape and colour) and rarity (facets
    and glow): no image files.
- **The Lattice:** a 7 × 7 grid. It opens as the 3 × 3 centre; perks open rings. Placed
  crystals give their effects; the four beside a crystal change them:

| crystal | on its neighbours |
| --- | --- |
| Amber | a Cobalt beside it: both ×1.5 |
| Cobalt | a Verdant beside it: +1 level to the Verdant |
| Verdant | each Verdant beside it: ×1.2 |
| Rose | an Amber beside it: the Amber's strongest effect ^1.25 |
| Onyx | its own effects ×3; the crystals beside it give nothing |
| Pearl | copies its best neighbour's effects at half strength |
| Ghost | counts as every kind for its neighbours' rules, gives nothing itself |
| Prism | its neighbours' effects ×(1 + 0.1 · distinct kinds around it) |

  A crystal moves freely between Collapses; during a run, a move costs 10% of the run's
  Shards. 3 layout presets. Inventory: 120 crystals.
- **Essence:** dissolving a crystal gives Essence of its kind, `level · (1 + rarity)²`.
- **Distilling:** 12 reagents, made from Essences or from other reagents, each with a cap
  raised by Shards; a reagent's amount gives an effect. Reactions run once per Collapse, each
  using up its inputs.

| reagent | made from | effect |
| --- | --- | --- |
| Ashwater | Amber + Rose | Lights ×(1 + amount)^2 |
| Coldfire | Cobalt + Amber | Tempo step +amount/1e4 |
| Greenglass | Verdant + Pearl | study speed ×(1 + amount/100) |
| Nightsalt | Onyx + Cobalt | Void ×(1 + amount/50) |
| Moonmilk | Pearl + Rose | Essence ×(1 + amount/100) |
| Thornsap | Verdant + Onyx | Rift Trial goals ^0.99 per 100 |
| Embersilk | Ashwater + Coldfire | Flares ×(1 + amount/1000) |
| Stillwater | Greenglass + Nightsalt | the Haze penalty eases by amount/1e5 |
| Starmortar | Moonmilk + Ashwater | Theorems ×(1 + amount/100) |
| Duskwine | Thornsap + Nightsalt | Shards ×(1 + amount/200) |
| Quickmercury | Embersilk + Starmortar | the Pulsar's strength ×(1 + amount/500) |
| Lucent | every other reagent | every Crystal effect ×(1 + amount/1e4) |

- **Lattice perks:** 40 nodes bought with perk points: grid rings, the 3-drop choice, the drop
  filter, starting Theorems, keeping Rift Trials, Ghost crystals, auto-buying the Chart from a
  preset on each Rift, and the like.
- **The Pulsar:** opened with Shards. Every P seconds (start 100 s), it runs D seconds (start
  10 s) at game speed ×S (start ×10). Shard upgrades raise S, lengthen D and shorten P. It can
  be paused; a second Pulsar (perk 35) multiplies with the first.
- **The Reservoir:** while *charging*, the game runs at 1% speed and the other 99% is stored as
  time. *Releasing* runs the stored time at once, in offline-style steps (at most 10,000). A
  setting sends offline time into the Reservoir instead of playing it. Cap: 1 hour of stored
  time, raised by perks and the Warden of Tide.
- **Scripts:** replace the rule automator. Up to 10 scripts of up to 10,000 characters, each
  with a text editor and a block editor that round-trip exactly.

```text
script  := stmt*
stmt    := command NEWLINE
         | "if" cond "{" stmt* "}" ["else" "{" stmt* "}"]
         | "while" cond "{" stmt* "}"     | "until" cond "{" stmt* "}"
         | "wait" (cond | duration)        | "repeat" "{" stmt* "}"
         | "#" any-text NEWLINE
command := "kindle" | "flare" | "offer" | "nova" | "rift" | "collapse" | "ascend"
         | "trial" ("nova" | "breach" | "rift") INT | "trial" "leave"
         | "chart" ("buy" ID+ | "preset" INT | "respec") | "haze"
         | "study" INT | "focus" INT | "layout" INT | "pulsar" ("on" | "off")
         | "auto" NAME ("on" | "off") | "theorems" "max" | "notify" STRING
cond    := value OP value ("and" cond | "or" cond)?
value   := NUMBER | duration | currency | "pending" "(" reset ")" | "runtime"
         | "trial" | "tier" "(" INT ")"
OP      := "<" | "<=" | ">" | ">=" | "==" | "!="
NUMBER  := 12 | 1.5e300 | 1e1e6          duration := 5s | 2m | 1h
```

  A script is parsed when saved; an error names its line and column and the script won't
  run. Running: one statement per tick (10 with perk 38); `wait` holds; a statement that is
  refused (`Refused`) is skipped with a note in the script's log. A loop with no `wait` runs
  at most 100 statements per tick. The model runs scripts, so the simulator runs them too.
- **The goal:** 1e30 Shards opens the Zenith. Target: 130 h.

### 2.8 Layer 5: Zenith

- **Ascend** resets everything up to the Lattice (the layout, the inventory and the reagents
  are kept) and gives Aether `floor(log10(Sh) − 29)` (*tuned*): a slow, countable currency.
- **Zenith milestones** (6) and **Zenith upgrades** (20, bought with Aether).
- **Laws:** 12 rules that rewrite earlier ones, in 1 to 4 slots (from Zenith milestones).
  Changing laws needs an Ascend.

| law | it rewrites |
| --- | --- |
| Plenty | Facets every 10 bought |
| Haste | a Kindle and a Flare reset nothing |
| Echo | a Nova gives Stardust as if Lumen were ×1e10 |
| Stillness | offline and Reservoir time count double |
| Gravity | Light k also makes Light k−2, at 1% |
| Tides | every study is active |
| Lens | Focus points ×3 |
| Hunger | Void ^1.1, and Lights ^0.9 |
| Order | the Lattice counts diagonal neighbours |
| Fervor | autobuyers act 10 times per tick |
| Memory | a Rift keeps Lumen at ^0.5 |
| Unity | every crystal counts as every kind for its neighbours' rules |

- **The Wardens:** seven realms, opened one by one with Aether. Entering a Warden's realm is
  an Ascend-run under the realm's rule. Each realm has 6 to 8 stages, each a goal reached
  inside it. Every stage gives a reward, and the realm opens one new mechanic for the whole
  game.

| warden | the realm's rule | the mechanic it opens |
| --- | --- | --- |
| Cinder | Tempo is frozen at ×1 | **the Kiln:** pour Shards in to heat it; heat multiplies Shards on Collapse, and cools 1%/min |
| Glass | only crystals of one kind (chosen on entry) count | **Prism crystals**, and **cutting:** once per Ascend, make a crystal with effects you pick, from Essences |
| Tide | game speed swings between ×0.01 and ×1 every 60 s | **the Surge:** Reservoir time released during a Pulsar burst counts ×the burst's speed; a higher Reservoir cap |
| Thorn | the 12 Trials' restrictions rotate, one every 5 minutes | **Grafts:** 10 permanent bonuses levelled by the realm's best Lumen |
| Hush | autobuyers and scripts are off; Light costs grow 10× faster | **Echoes:** each placed crystal also counts once more, at 25%, for every Hush stage done |
| Lode | only Void counts: Lumen and Stardust give no multipliers | **the Aether Engines:** a fourth chain of 8, bought with Aether, making Quintessence, which multiplies every other chain's output |
| Hollow | the final realm (below) | the finale |

- **The Hollow:** opened when the other six Wardens are fully done. Inside it, every currency
  loses 1% of its logarithm each second (so 1e100 decays toward 1e99 in a second), and every
  exponent bonus counts at ^0.5. Five **Anchors** stop the decay, one currency each (Lumen,
  Stardust, Void, Shards, Aether), in order; an Anchor is filled by pouring Aether into it,
  and each filled one also opens a final law slot holding one of 5 Hollow laws. With the
  five Anchors filled, the decay is gone, and the goal is 1e(1e9) Lumen inside the Hollow.
- **Zenith Trials** (4): runs with every Trial at once and one law forced, rewarding Aether
  multipliers.
- **The finale:** 1e(1e9) Lumen in the Hollow. The sky fills (an animation of the sidebar's
  layers lighting one by one), the run's statistics are shown, and the Afterglow opens.
  Target: 260 h.

### 2.9 Afterglow

The endless mode after the finale. A Glimmer resets everything but the Laws, the Wardens'
mechanics and the records, and gives Glimmer from how fast the run reached e1e9 Lumen. Glimmer
upgrades only speed up the next run. A local board keeps the player's 20 best runs.

### 2.10 What every layer shares

- **Achievements:** 160, in rows of 8 (20 rows), each ×1.02 to every Light; a full row gives a
  named reward. 20 are secret (hidden until done). Shown as a grid of tiles with tooltips.
- **Statistics:** play time in total and per layer; fastest of each reset; every currency's
  best; resets counted; offline and Reservoir time.
- **Records:** the last 10 of each reset (Nova, Rift, Collapse, Ascend): its time, its gain, and
  its gain per minute.
- **The breakdown:** for each currency, the list of multipliers that make its rate (G7).
- **The Codex:** a help page per mechanic (about 40), opened when the mechanic is; a `?` beside
  each panel opens its page.
- **Hints:** the first 30 minutes show short hints at the next useful action, one at a time,
  off by a setting.
- **The news line:** one line of original flavour text at the bottom, every 20 s, from 300
  lines that grow with the layers reached. UI-only randomness.
- **Notations:** scientific (`1.23e456`), engineering (`123e454`), standard (`1.23 Qa`, up to
  1e303, then scientific), letters (`1.23 ab`), logarithm (`e456.09`), and starlit (`✦4.56`,
  the log written with flni's glyphs); past 1e1e6 every notation shows `e1.23e6`.
- **Hotkeys:** `1`–`8` buy max of a Light, `M` buy max of all, `T` Tempo max, `K` Kindle,
  `F` Flare, `O` Offering, `N` Nova, `R` Rift, `C` Collapse, `A` all autobuyers on or off,
  `Esc` close a dialog. Remappable.
- **Settings:** notation and decimals, theme (Night, Dawn, Ink, Ember, Mono, High contrast),
  autosave interval, a confirmation per reset, offline progress (play, store, off), refresh
  rate (10, 20, 30 Hz), hotkeys, hints, saves.
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
      state.py     the save's structs (msgspec): every layer's state, and the rng
      game.py      Game: tick(dt), the actions, Refused
      effects.py   every named multiplier, and the breakdown
      glow.py nova.py rift.py lattice.py zenith.py wardens.py afterglow.py
      chains.py    one producer-chain engine for Lights, Forges, Engines and Aether Engines
      studies.py   the study engine and the six Studies
      crystals.py  drops, effects, neighbour rules, Essence, Distilling
      time.py      game speed: Pulsar, Reservoir, Haze, realm speeds
      script/      lexer.py parser.py run.py blocks.py (the block form <-> the text)
      offline.py   the offline steps
      save.py      encode, decode, versions, migrations, export strings
      rng.py       a PCG32 generator: seed, next, kept in the state
    content/       data rows only
      glow.py nova.py rift.py lattice.py zenith.py wardens.py achievements.py
      codex.py news.py
    sim/
      policy.py    the scripted players (greedy, steady)
      run.py       the simulator and the pacing report
    app/           Qt: imports model/, content/, num.py, notation.py
      main.py      QApplication, the window, the timers
      theme.py     colours, fonts and spacing as tokens; the QSS made from them
      store.py     save files on disk: slots, atomic writes, backups
      window.py    the sidebar, the top bar, the news line, the tab stack
      widgets.py   BigLabel, BuyButton, Meter, Tile, Tooltip, CrystalIcon
      canvas.py    QGraphicsView scenes: the Constellation, the Chart, the Lattice
      editor.py    the script editors: text (with highlighting and errors) and blocks
      tabs/        one module per tab
  tests/
    test_num.py test_notation.py test_rng.py test_save.py test_rules_*.py test_offline.py
    test_script_*.py test_content.py test_pacing.py test_layers.py test_app_smoke.py
    test_docs.py
    data/saves/    a saved example of every save version
    bench/         tick, buy max, offline, script, frame
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

- `Game(state)` holds the state and offers the actions, one method each: buying (Lights,
  Tempo, Forges, Engines, Aether Engines, Theorems, upgrades, nodes, perks, Bloom upgrades,
  Study variables), resets (`kindle`, `flare`, `offer`, `nova`, `rift`, `collapse`,
  `ascend`, `glimmer`, `seed`), entering and leaving (Trials, Haze, realms), and setting
  (Focus, presets, layouts, crystal moves, the drop filter, laws, autobuyers, scripts, the
  active Study, Pulsar and Reservoir).
  - An action that can't be done raises `Refused(reason)` and changes nothing.
  - The UI, the autobuyers, scripts and the simulator use only these actions.
- `game.tick(dt)`: `dt` is scaled by the game speed (Pulsar, realm rules, Reservoir), then, in
  order: the chains from the top tier down (each fed by the tier above's amount before this
  tick), Bloom, Silt, the Studies, autobuyers, scripts, achievements, the unlock checks.
- `game.rate(currency) -> Big`, `game.breakdown(currency) -> list[(name, factor)]`,
  `game.pending(reset) -> Big`.
- **One chain engine** (`chains.py`) serves the Lights, the Forges, the Rift Engines and the
  Aether Engines: tiers, costs in closed form, buy max, production.
- Effects are recomputed only when something they depend on changes; each module marks its
  effects dirty, and `effects.py` caches the products.
- Speed goal (bench `model.tick`): a tick of a late-game state ≤ 1 ms.

### 3.3 Offline progress and stored time

- The app stores the wall-clock time with every save. On load, `elapsed = now − saved`
  (ignored if negative; capped at 30 days).
- By the setting: *play* runs `offline.run(game, elapsed)`: `n = min(10_000,
  ceil(elapsed / 0.05))` ticks of `elapsed / n`, with the automation running, and shows what
  changed; *store* adds it to the Reservoir (up to its cap, the rest played); *off* drops it.
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
  tick.
- Speed goals: bench `ui.frame` ≤ 4 ms for the busiest tab; idle on the Glow tab, ≤ 3% of one
  core.
- **The theme** (`app/theme.py`): colour tokens per theme (Night: a deep blue sky, warm light
  for currencies, one accent per layer: amber, violet, teal, rose, gold); spacing on a 4 px
  grid; corners of 6 px; tabular figures for numbers. The QSS is built from the tokens; no
  colour is written anywhere else.
- **The layout:**

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ ✦ 1.23e48 Lumen (+4.56e46/s)  ✧ 3.2e4 SD  ◈ 12 V    Tempo ×4.1e9  Nova +12│
├────────────┬──────────────────────────────────────────────────────────────┤
│ Glow     ● │  Lights     Focus 11 (3 free)   [Tempo max T] [Buy max all M]│
│  Lights    │  ┌──────────────────────────────────────────────────────────┐│
│  Shop      │  │ 1 Mote     ×1.2e9    1.4e40   [ 25 → ×3   cost 1.1e45 ] ││
│ Nova       │  │ 2 Wisp     ×3.4e8    2.0e35   [ 11/25     cost 3.0e44 ] ││
│  Grid      │  │ …                                                         ││
│  Tree      │  │ 8 Halo     ×12.0     19       [  3/25     cost 2.2e47 ] ││
│  Trials    │  └──────────────────────────────────────────────────────────┘│
│  Forges    │  Kindle 6: needs 40 Halo (19)   [Kindle K]  [Flare F] [Offer]│
│  Auto      │                                                              │
│ Rift    🔒 │                                                              │
│ Codex  Ach │                                                              │
│ Stats  Set │                                                              │
├────────────┴──────────────────────────────────────────────────────────────┤
│  “A lamp is just a mote with ambition.”                                   │
└───────────────────────────────────────────────────────────────────────────┘
```

  A locked layer shows a lock and, in its tooltip, what opens it. A new unlock shows a dot on
  its sidebar entry until visited. The window is resizable down to 1024 × 640.
- **Accessibility:** every action has a keyboard path; contrast ≥ 4.5:1 in every theme (the
  High contrast theme ≥ 7:1); no meaning carried by colour alone.

## 4. Content counts

| kind | count |
| --- | ---: |
| producer chains (Lights, Forges, Rift Engines, Aether Engines) | 4 × 8 tiers |
| Lumen shop | 12 |
| Stardust grid and repeatables | 16 + 5 |
| Constellation nodes | 36 |
| Trials (Nova, Breach, Rift ×5 tiers, Zenith) | 12 + 8 + 12 + 4 |
| Breach upgrades | 8 |
| Void upgrades | 20 |
| Chart nodes | 60 |
| Mist upgrades | 12 |
| Studies (each 4–6 variables, 5 milestones) | 6 |
| crystal kinds, with effect pools of 5–6 | 8 |
| reagents | 12 |
| Lattice perks | 40 |
| Zenith upgrades | 20 |
| Laws (and Hollow laws) | 12 + 5 |
| Warden stages | 7 realms, 48 stages |
| milestones (Nova, Rift, Zenith) | 12 + 12 + 6 |
| achievements | 160 |
| Codex pages | ~40 |
| news lines | 300 |

Every row of `content/` has an id, a name, a one-line description (≤ 90 characters), and its
numbers. `test_content.py` checks unique ids, unique names within a kind, that every reference
(a node's parents, an achievement's condition, a reagent's inputs) exists, and that every
mechanic has a Codex page.

## 5. The simulator and pacing

- `python -m flni sim --policy steady --until finale` plays a new game with the model alone and
  prints each milestone's game time. It steps at `dt = 1 s` up to the Rift and `dt = 10 s`
  after (*tuned* so layer 1's timings stay within 5% of `dt = 0.05`).
- **Policies** (`sim/policy.py`) are functions `(game) -> None` called every simulated step,
  using only the actions of 3.2. Late in the game, `steady` drives the game by loading flni
  scripts from `sim/scripts/`, the same language players use.
  - `greedy`: buys the best ratio of gain to cost, resets when the pending gain doubles the
    currency.
  - `steady`: a careful player: Focus on the best Light, Kindles and Flares at once, Nova at 2×
    pending, Trials in order, studies switched every 10 minutes, a fixed good Lattice layout,
    the obvious laws, Wardens in order.
- **The pacing table** (game time, `steady`):

| milestone | target | band |
| --- | ---: | ---: |
| first Kindle | 3 min | ±50% |
| first Nova | 75 min | ±30% |
| all 12 Trials, Breach | 6 h | ±30% |
| all 8 Breach Trials | 14 h | ±30% |
| first Rift | 18 h | ±30% |
| Haze opened | 35 h | ±30% |
| all 6 Studies | 45 h | ±30% |
| all Rift Trials at tier 5 | 70 h | ±30% |
| first Collapse | 80 h | ±30% |
| scripts | 85 h | ±30% |
| first Ascend | 130 h | ±30% |
| six Wardens done | 220 h | ±25% |
| the finale | 260 h | ±25% |

- `tests/test_pacing.py` runs up to the first Nova (in `make check`, ≤ 10 s of wall time).
  `make pace` runs the whole table (≤ 10 minutes) and fails outside a band; run it in every
  step that adds or changes content, from the layer that step touches.
- A *wall* (more than 2 h of game time before the Rift, or 6 h after, with no new purchase or
  reset under `steady`) fails `make pace` too.

## 6. Speed

| bench | what | goal |
| --- | --- | --- |
| `num.ops` | 1M adds and 1M multiplies of `Big` | ≤ 1.5 µs each |
| `model.tick` | one tick of a saved late-game state | ≤ 1 ms |
| `model.buy_max` | buy max on all 8 Lights, 10,000 times | ≤ 20 µs each |
| `script.run` | a 200-line script, 10,000 ticks | ≤ 50 µs per tick |
| `offline.10k` | 10,000 offline steps of a late-game state | ≤ 0.5 s |
| `ui.frame` | one refresh of the busiest tab, offscreen | ≤ 4 ms |
| `app.start` | process start to the window shown | ≤ 1.5 s |

`make bench` runs them; `ARGS=--gate` fails on a regression over 15% against
`tests/bench/gates.json`; `ARGS=--record` stores a new baseline.

## 7. Packaging

- `make dist` runs PyInstaller with `--onedir --windowed --name flni` on the OS it runs on:
  `dist/flni.app` on macOS, `dist/flni/` on Windows and Linux, then zips it.
- An icon: a single star, drawn as SVG in `flni/app/icons/`, exported to `.icns` and `.ico`
  by a script in `tools/`.
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
  model.md         Game, its actions and Refused, the tick order, effects and their cache,
                   the chain engine, game speed, the rng
  layers/          one page per layer: glow.md nova.md rift.md lattice.md zenith.md
                   wardens.md afterglow.md. Each gives every mechanic of the layer, its
                   formulas (written out, with the content ids they read), what each reset
                   keeps, and the unlock conditions
  content.md       how to add or change a row of each kind in content/: the fields, the
                   checks test_content.py makes, and a worked example (a new Trial)
  balance.md       the simulator, the two policies, the pacing table, how to read
                   `make pace`, how to tune a number and record it, what a wall is
  numbers.md       Big: its form, its limits, where precision is lost; the notations
  saves.md         the file format, versions, how to write a migration and its saved
                   example, backups, the export string
  scripts.md       the script language: the grammar, every command and value, the limits per
                   tick, the errors, examples; for players and developers both
  ui.md            the window, the theme tokens, the widgets, the refresh rule, how to add a
                   tab, accessibility
  performance.md   the benches, their goals and last numbers, `make bench ARGS=--gate`, how a
                   speed-up is proven
  packaging.md     `make dist` on each OS, what is excluded, how to test a build by hand
  glossary.md      every word the game and these pages use, one meaning each (Light, tier,
                   bought, amount, reset, layer, run, realm, stage, effect, …)
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
- **Original only:** no name, text, art or exact rule taken from another game.
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
2. **The model's core.** `state.py`, `game.py` with ticks and `Refused`, `effects.py` and the
   breakdown, `chains.py`, `time.py` (speed only), `save.py` with version 1 and the export
   string, `offline.py`; `docs/architecture.md`, `docs/model.md`, `docs/saves.md`.
3. **The app's shell.** The window, the themes, the sidebar and tab stack, the top bar, the
   news line, settings, save slots, autosave, backups, import and export, the offline dialog,
   hotkeys, the Codex viewer, hints; `docs/ui.md`.
4. **Glow.** Lights, Facets, Tempo, Focus, Kindle, Flare, Offering, the shop; its tab;
   `docs/layers/glow.md`, `docs/content.md`.
5. **The simulator.** `sim/`, both policies, `test_pacing.py` to the first Nova, `make pace`;
   tune layer 1 into its band; `docs/balance.md`.
6. **Nova.** Stardust, Starlight, the grid, the Constellation (canvas), milestones,
   autobuyers, the 12 Trials; pace to "all 12 Trials".
7. **Breach.** Breach and its upgrades, Breach Trials, the Forges, Bloom and Seeds; pace to
   "first Rift".
8. **Around the game.** Achievements (the first 60), statistics, records, the breakdown panel,
   notations, the Codex pages and news lines so far.
9. **Rift.** Void, milestones, Void upgrades, Theorems and the Chart (canvas, presets, strings),
   the Rift Engines, the rule automator; pace to "Haze opened".
10. **Rift's depth.** The 12 Rift Trials with 5 tiers, Haze with Mist, Silt and its upgrades,
    the Observatory's six Studies; pace to "first Collapse".
11. **Lattice.** Collapse, crystals (drops, effects, drawing, the filter), the grid and its
    rules, Essence, Distilling, the perks; pace to "scripts".
12. **Time and scripts.** The Pulsar, the Reservoir, the script language (lexer, parser,
    runner), both editors, the rules' conversion; the `sim/scripts/`; `docs/scripts.md`; pace
    to "first Ascend".
13. **Zenith.** Ascend, milestones, upgrades, Laws, Zenith Trials, the Wardens Cinder, Glass
    and Tide.
14. **The other Wardens.** Thorn, Hush, Lode (with the Aether Engines); pace to "six Wardens
    done".
15. **The Hollow and after.** The Hollow, the Anchors and Hollow laws, the finale, the
    Afterglow; the rest of the achievements, Codex pages and news; pace to "the finale".
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

- **F1. One active study, the others at 10%.** Recommended, so switching studies is a choice;
  the Tides law then has a point.
- **F2. Offline capped at 30 days.** Recommended; a longer absence gives the same as 30 days.
- **F3. No cloud saves.** Recommended for now: export strings move a save between machines.
- **F4. No sound.** Recommended for now; a later step can add a few soft cues behind a setting.
- **F5. English only.** All text is in `content/`, so a translation can come later.
- **F6. 260 hours to the finale.** Recommended: the depth asked for. The pacing table is the one
  place to make it shorter, and the simulator shows the effect.
