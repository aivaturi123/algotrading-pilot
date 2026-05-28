"""
Curated watchlists for the trading pipeline.
Edit these lists freely — no other files need to change.

Set WATCH_SYMBOLS="volatile" in .env to use the VOLATILE list below.
Set WATCH_SYMBOLS="AAPL,TSLA" to use a custom comma-separated list instead.
"""

# 50 high-volatility, high-volume stocks across sectors
# Chosen for frequent sigma breaches and enough daily volume for real fills
VOLATILE = [
    # --- Crypto proxies (most reactive to sentiment) ---
    "MSTR",   # MicroStrategy — direct Bitcoin proxy, swings 10-20% daily
    "COIN",   # Coinbase — tracks crypto volume and sentiment
    "MARA",   # Marathon Digital — Bitcoin miner
    "RIOT",   # Riot Platforms — Bitcoin miner
    "CLSK",   # CleanSpark — Bitcoin miner
    "HUT",    # Hut 8 — Bitcoin miner

    # --- AI & quantum computing (thin float, spiky) ---
    "SOUN",   # SoundHound AI — voice AI, low float
    "BBAI",   # BigBear.ai — small AI defence play
    "IONQ",   # IonQ — quantum computing
    "RGTI",   # Rigetti Computing — quantum computing
    "PLTR",   # Palantir — AI/defence, momentum-driven
    "AI",     # C3.ai — enterprise AI, volatile earnings reactions

    # --- High-beta fintech ---
    "SMCI",   # Super Micro Computer — AI server infrastructure
    "UPST",   # Upstart — AI lending, rate-sensitive
    "AFRM",   # Affirm — BNPL, reacts hard to consumer data
    "SOFI",   # SoFi Technologies — digital bank
    "HOOD",   # Robinhood — retail brokerage, moves with meme activity
    "LC",     # LendingClub — consumer lending

    # --- EV & clean energy ---
    "RIVN",   # Rivian — EV startup, production-sensitive
    "LCID",   # Lucid Group — EV startup, thin margins
    "CHPT",   # ChargePoint — EV charging, policy-driven
    "BLNK",   # Blink Charging — EV charging
    "PLUG",   # Plug Power — hydrogen fuel cells
    "FCEL",   # FuelCell Energy — clean energy
    "RUN",    # Sunrun — residential solar

    # --- Space, aviation & defence ---
    "RKLB",   # Rocket Lab — small launch vehicles
    "JOBY",   # Joby Aviation — air taxi
    "ACHR",   # Archer Aviation — air taxi
    "LUNR",   # Intuitive Machines — lunar lander
    "ASTS",   # AST SpaceMobile — satellite broadband

    # --- High-growth SaaS (momentum darlings) ---
    "PATH",   # UiPath — RPA automation
    "DDOG",   # Datadog — cloud monitoring
    "SNOW",   # Snowflake — cloud data
    "NET",    # Cloudflare — network security
    "CRWD",   # CrowdStrike — cybersecurity
    "ZS",     # Zscaler — cloud security
    "S",      # SentinelOne — cybersecurity

    # --- Semiconductors (high beta) ---
    "AMD",    # Advanced Micro Devices
    "MRVL",   # Marvell Technology
    "WOLF",   # Wolfspeed — silicon carbide chips

    # --- Meme / retail favourites ---
    "GME",    # GameStop — still moves on social sentiment
    "AMC",    # AMC Entertainment
    "BB",     # BlackBerry

    # --- Other high-beta plays ---
    "CVNA",   # Carvana — used car marketplace, extreme moves
    "OPEN",   # Opendoor — iBuyer, rate-sensitive
    "LAZR",   # Luminar Technologies — lidar
    "ASTS",   # already above — skip duplicate in practice
    "TSLA",   # Tesla — still very volatile despite blue-chip status
    "NVDA",   # Nvidia — high volume, moves on AI news
    "AAPL",   # Apple — included so original symbols still work
]

# Deduplicate while preserving order
VOLATILE = list(dict.fromkeys(VOLATILE))


# Smaller focused lists for quieter sessions
CRYPTO_PROXIES = ["MSTR", "COIN", "MARA", "RIOT", "CLSK"]
AI_PLAYS       = ["SOUN", "BBAI", "IONQ", "RGTI", "PLTR", "SMCI"]
EV_ENERGY      = ["RIVN", "LCID", "CHPT", "PLUG", "RKLB"]
