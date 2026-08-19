"""
Canned boom / reform small-talk threads.

Mix of:
  • A/B (or A/B/C) polls — one-word answers
  • riffs — observations / banter; optional freeform react
  • open questions — answer in your own words

Keep hits away from official tanker words (rejoin, contact, disconnect,
observation). Boom operator is enlisted talking to an F-16 officer —
respectful, not peer-to-peer.
"""

from __future__ import annotations

import re
from typing import Any


def _slug(say: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (say or "").casefold())[:24] or "opt"


def C(
    say: str,
    reply: str,
    *hits: str,
    follow: dict[str, Any] | None = None,
    cid: str | None = None,
) -> dict[str, Any]:
    words = tuple(
        w
        for w in (say or "").casefold().replace("-", " ").replace("'", "").split()
        if len(w) > 1 and w not in {"the", "and", "for", "or", "a"}
    )
    row: dict[str, Any] = {
        "id": cid or _slug(say),
        "say": say,
        "hits": hits or words,
        "reply": reply,
    }
    if follow:
        row["follow"] = follow
    return row


def R(
    say: str,
    reply: str,
    *hits: str,
    follow: dict[str, Any] | None = None,
    cid: str | None = None,
) -> dict[str, Any]:
    """Patterned freeform react for a riff / open bit."""
    return C(say, reply, *hits, follow=follow, cid=cid)


def F(opener: str, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {"id": "follow", "kind": "ab", "opener": opener, "choices": [a, b]}


def T(tid: str, opener: str, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {"id": tid, "kind": "ab", "opener": opener, "choices": [a, b]}


def Riff(
    tid: str,
    opener: str,
    *reacts: dict[str, Any],
) -> dict[str, Any]:
    """Observation / banter — no forced A/B. Optional patterned reacts."""
    row: dict[str, Any] = {
        "id": tid,
        "kind": "riff",
        "opener": opener,
        "choices": [],
    }
    if reacts:
        row["reacts"] = list(reacts)
    return row


def Open(
    tid: str,
    opener: str,
    *reacts: dict[str, Any],
) -> dict[str, Any]:
    """Open question — pilot answers in their own words."""
    row: dict[str, Any] = {
        "id": tid,
        "kind": "open",
        "opener": opener,
        "choices": [],
    }
    if reacts:
        row["reacts"] = list(reacts)
    return row


THREADS: list[dict[str, Any]] = [
    T(
        "dunkin_starbucks",
        "{cs}, {tcs}, you guys drink coffee down there all the time? "
        "Tough to keep it warm though. Dunkin or Starbucks?",
        C(
            "Dunkin",
            "Copy Dunkin. That's the real stuff. We got a Keurig up here, "
            "little shots of coffee, something nice and dark.",
            "dunkin",
            "dunkins",
            "dunkin donuts",
            follow=F(
                "Morning coffee, or you still pounding it in the afternoon?",
                C(
                    "Morning",
                    "Morning, copy. That's for the morning. Cappuccino after noon would blast me.",
                    "morning",
                    "am",
                    "breakfast",
                ),
                C(
                    "Afternoon",
                    "Afternoon, copy. We're not British — you can drink it whenever you want up here.",
                    "afternoon",
                    "pm",
                    "later",
                    "evening",
                ),
            ),
        ),
        C(
            "Starbucks",
            "Starbucks, copy. Fancy. We had a French press up here one time "
            "till somebody broke it. Now it's just the Keurig.",
            "starbucks",
            "star bucks",
            "sbux",
            follow=F(
                "Morning coffee, or you still pounding it in the afternoon?",
                C(
                    "Morning",
                    "Morning, copy. Dark in the morning, lighter later if you can get it.",
                    "morning",
                    "am",
                    "breakfast",
                ),
                C(
                    "Afternoon",
                    "Afternoon, copy. That's a late-day cappuccino — respect.",
                    "afternoon",
                    "pm",
                    "later",
                    "evening",
                ),
            ),
        ),
    ),
    T(
        "keep_warm",
        "{cs}, {tcs}, you able to keep a cup of coffee warm down there, "
        "or does the jet just cook it?",
        C(
            "By the radar",
            "Copy, by the radar. Giant fireball, that tracks. "
            "We stash ours up front by this big radar of our own.",
            "radar",
            "fireball",
            "up front",
            "cockpit",
        ),
        C(
            "It's cold",
            "Copy, ice coffee by accident. That's a Viper problem. "
            "We'd freeze too if this thing ever shut up.",
            "cold",
            "ice",
            "never warm",
        ),
    ),
    T(
        "lunch",
        "{cs}, {tcs}, what are you serving down there today — "
        "Pop-Tarts or peanut butter and jelly?",
        C(
            "Pop-Tarts",
            "Pop-Tarts, copy. Don't crush 'em in the bag — then it's just crumbs. "
            "Pretty tasty crumbs, though.",
            "pop tart",
            "poptart",
            "pop-tarts",
            "toaster",
        ),
        C(
            "PB and J",
            "PB and J, copy. That's a classic. We had chicken nuggets one time "
            "and the whole boom crew got jealous.",
            "pbj",
            "p b and j",
            "peanut butter",
            "jelly",
            "sandwich",
        ),
    ),
    T(
        "quiet",
        "{cs}, {tcs}, how you doing out there? Kind of boring from up here. "
        "Quiet, or you guys busy?",
        C(
            "Quiet",
            "Copy, all quiet on the western front. Good day for gas.",
            "quiet",
            "boring",
            "slow",
            "all quiet",
            "dead",
        ),
        C(
            "Busy",
            "Busy, copy. We'll keep you on the boom and stay out of the way.",
            "busy",
            "working",
            "spiked",
            "interesting",
        ),
    ),
    T(
        "keurig",
        "{cs}, {tcs}, we got a Keurig up here today. You guys running "
        "a French press, or just the gas station stuff?",
        C(
            "Keurig",
            "Keurig, copy. Fancy coffee. Little espresso shot if you can get it.",
            "keurig",
            "k cup",
            "pods",
            "pod",
        ),
        C(
            "French press",
            "French press, copy. Ours broke. No French press, no cold press — "
            "just hot breath and the Keurig.",
            "french press",
            "press",
            "cold brew",
            "cold press",
        ),
    ),
    T(
        "boom_feel",
        "{cs}, {tcs}, how you feeling on the boom? Looking stable from here.",
        C(
            "Looks good",
            "Copy, looking good. Sit there and drink your coffee.",
            "good",
            "stable",
            "fine",
            "great",
            "easy",
            "smooth",
        ),
        C(
            "A little work",
            "Copy, a little work. We'll hold her steady. You're doing fine.",
            "work",
            "rough",
            "bumpy",
            "fighting",
            "heavy",
        ),
    ),
    T(
        "nuggets",
        "{cs}, {tcs}, random question while you're hanging out. "
        "Chicken nuggets or a protein bar?",
        C(
            "Nuggets",
            "Nuggets, copy. That's a boom-crew meal. Don't tell the dietitian.",
            "nugget",
            "nuggets",
            "chicken",
        ),
        C(
            "Protein bar",
            "Protein bar, copy. Responsible. We respect that and then eat the nuggets anyway.",
            "protein",
            "bar",
            "clif",
            "cliff",
            "healthy",
        ),
    ),
    T(
        "tacos_burgers",
        "{cs}, {tcs}, serious boom poll. After this, tacos or a burger?",
        C(
            "Tacos",
            "Tacos, copy. Correct answer. We'll be jealous from twenty-six thousand.",
            "taco",
            "tacos",
        ),
        C(
            "Burger",
            "Burger, copy. That's a recovery meal. Get two. Boom operator's orders.",
            "burger",
            "burgers",
            "cheeseburger",
        ),
    ),
    T(
        "dogs_cats",
        "{cs}, {tcs}, important question. Dogs or cats?",
        C(
            "Dogs",
            "Dogs, copy. They'd love this view. Also they'd steal the left seat.",
            "dog",
            "dogs",
            "puppy",
        ),
        C(
            "Cats",
            "Cats, copy. They'd ignore the whole sortie and sleep on the HUD.",
            "cat",
            "cats",
            "kitten",
        ),
    ),
    T(
        "pizza",
        "{cs}, {tcs}, pizza topping hill to die on. Pepperoni or pineapple?",
        C(
            "Pepperoni",
            "Pepperoni, copy. That's the approved loadout. No notes.",
            "pepperoni",
            "pep",
        ),
        C(
            "Pineapple",
            "Pineapple, copy. Bold. The boom crew is divided. I'm not taking sides on open mic.",
            "pineapple",
            "hawaiian",
            "ham",
        ),
    ),
    T(
        "energy",
        "{cs}, {tcs}, what are you running on down there — energy drink or just spite?",
        C(
            "Energy drink",
            "Energy drink, copy. Don't shake it. We do not want a foam show on the boom.",
            "energy",
            "monster",
            "red bull",
            "redbull",
            "bang",
            "celsius",
        ),
        C(
            "Spite",
            "Spite, copy. That's sustainable fuel. Zero calories, unlimited range.",
            "spite",
            "hate",
            "anger",
            "vibes",
            "nothing",
        ),
    ),
    T(
        "naps",
        "{cs}, {tcs}, honest question. Could you nap in that cockpit, or is it all elbows?",
        C(
            "Could nap",
            "Could nap, copy. Respect. Autopilot and a dream. We'll keep the boom quiet.",
            "nap",
            "sleep",
            "could",
            "yes",
        ),
        C(
            "No chance",
            "No chance, copy. We have a bunk. Not bragging. Okay a little bragging.",
            "no",
            "can't",
            "cannot",
            "elbows",
            "awake",
        ),
    ),
    T(
        "view",
        "{cs}, {tcs}, who has the better view right now — you looking up at us, "
        "or us looking down at the whole desert?",
        C(
            "You do",
            "We do, copy. Big windows, bad coffee, better scenery. Fair trade.",
            "you",
            "yours",
            "up there",
        ),
        C(
            "We do",
            "You do, copy. Looking up at a flying gas station is a vibe. We'll allow it.",
            "we",
            "ours",
            "viper",
            "down here",
        ),
    ),
    T(
        "bathroom",
        "{cs}, {tcs}, awkward boom question. You thinking about the bathroom yet, "
        "or are we still professional?",
        C(
            "Still professional",
            "Still professional, copy. That's a lie and we both know it. Hang in there.",
            "professional",
            "fine",
            "no",
        ),
        C(
            "Thinking about it",
            "Thinking about it, copy. We have a toilet. Not bragging. Definitely bragging.",
            "thinking",
            "bathroom",
            "yes",
            "hurry",
        ),
    ),
    T(
        "music",
        "{cs}, {tcs}, playlist check. Country or rock while you hang on the boom?",
        C(
            "Country",
            "Country, copy. We'll hum something slow so you don't start dancing on the boom.",
            "country",
            "nashville",
            "brooks",
        ),
        C(
            "Rock",
            "Rock, copy. Keep it in your head. If you start headbanging we have to call a breakaway.",
            "rock",
            "metal",
            "guitar",
        ),
    ),
    T(
        "monday_friday",
        "{cs}, {tcs}, does this feel like a Monday sortie or a Friday sortie?",
        C(
            "Monday",
            "Monday, copy. That tracks. Coffee's weaker and the boom feels longer.",
            "monday",
            "monday's",
        ),
        C(
            "Friday",
            "Friday, copy. Get your gas and go home. Boom crew is already mentally at the grill.",
            "friday",
            "weekend",
        ),
    ),
    T(
        "beach_mountains",
        "{cs}, {tcs}, after you land, beach or mountains?",
        C(
            "Beach",
            "Beach, copy. Sand in everything. Still better than a G-suit.",
            "beach",
            "ocean",
            "sand",
        ),
        C(
            "Mountains",
            "Mountains, copy. Thin air, you already trained for that. Smart.",
            "mountain",
            "mountains",
            "hike",
        ),
    ),
    T(
        "wings",
        "{cs}, {tcs}, wings heat check. Mild or you actually want to suffer?",
        C(
            "Mild",
            "Mild, copy. That's the adult choice. We still respect the buffalo.",
            "mild",
            "ranch",
        ),
        C(
            "Hot",
            "Hot, copy. Write that down. If you break away later we're blaming the sauce.",
            "hot",
            "spicy",
            "suicide",
            "extra",
        ),
    ),
    T(
        "ice_cream",
        "{cs}, {tcs}, ice cream diplomacy. Chocolate or vanilla?",
        C(
            "Chocolate",
            "Chocolate, copy. Correct. Vanilla people can still sit with us.",
            "chocolate",
            "choc",
        ),
        C(
            "Vanilla",
            "Vanilla, copy. Don't let them tell you it's boring. It's a classic like a Viper.",
            "vanilla",
        ),
    ),
    T(
        "burrito",
        "{cs}, {tcs}, breakfast of champions. Breakfast burrito or just coffee and bad decisions?",
        C(
            "Burrito",
            "Burrito, copy. That's a preflight. Hope you didn't drop it in the map case.",
            "burrito",
            "breakfast",
        ),
        C(
            "Coffee",
            "Coffee, copy. Bad decisions, copy. That's a full fuel load.",
            "coffee",
            "decisions",
            "just coffee",
        ),
    ),
    T(
        "gatorade",
        "{cs}, {tcs}, hydration check. Gatorade or water like a responsible adult?",
        C(
            "Gatorade",
            "Gatorade, copy. Pick a color that matches the jet. We're watching.",
            "gatorade",
            "gator",
            "electrolyte",
        ),
        C(
            "Water",
            "Water, copy. Boring and correct. Boom crew is drinking something that glows.",
            "water",
            "h2o",
        ),
    ),
    T(
        "truck_car",
        "{cs}, {tcs}, what do you drive to the squadron — a truck, or something that fits in a hangar?",
        C(
            "Truck",
            "Truck, copy. Compensating for the tiny cockpit. We get it.",
            "truck",
            "pickup",
            "f150",
            "f-150",
        ),
        C(
            "Car",
            "Car, copy. Fast on the ground too. Don't get a ticket after a tanker hop, that's embarrassing.",
            "car",
            "sedan",
            "civic",
            "small",
        ),
    ),
    T(
        "early_late",
        "{cs}, {tcs}, are you an early bird or did we just ruin your morning?",
        C(
            "Early bird",
            "Early bird, copy. That's why you're already on the boom and we're still yawning.",
            "early",
            "morning person",
            "bird",
        ),
        C(
            "Night owl",
            "Night owl, copy. Sorry about the sunrise. We'll try to be interesting.",
            "night",
            "owl",
            "late",
            "ruined",
        ),
    ),
    T(
        "books_podcasts",
        "{cs}, {tcs}, if you could listen to something on this boom besides us. Book or podcast?",
        C(
            "Book",
            "Book, copy. Audiobook. Don't turn pages in the cockpit, that's how you lose a checklist.",
            "book",
            "audiobook",
            "novel",
        ),
        C(
            "Podcast",
            "Podcast, copy. Two guys talking about nothing. Wait — that's us.",
            "podcast",
            "show",
        ),
    ),
    T(
        "grill_smoker",
        "{cs}, {tcs}, weekend cooking. Grill or smoker?",
        C(
            "Grill",
            "Grill, copy. Fast and honest. Like a Viper takeoff.",
            "grill",
            "grilling",
            "propane",
        ),
        C(
            "Smoker",
            "Smoker, copy. That's a six-hour tanker track. Respect the low and slow.",
            "smoker",
            "brisket",
            "smoke",
        ),
    ),
    T(
        "fishing_hunting",
        "{cs}, {tcs}, days off. Fishing or hunting?",
        C(
            "Fishing",
            "Fishing, copy. Sitting, waiting, hoping something takes the bait. That's boom work too.",
            "fishing",
            "fish",
            "bass",
        ),
        C(
            "Hunting",
            "Hunting, copy. We'd make a joke about being the prey up here but you're on the boom so we'll behave.",
            "hunting",
            "hunt",
            "deer",
        ),
    ),
    T(
        "socks",
        "{cs}, {tcs}, fashion report. Matching socks today, or fighter-pilot chaos?",
        C(
            "Matching",
            "Matching, copy. Overachiever. The boom crew is impressed and a little scared.",
            "matching",
            "match",
            "same",
        ),
        C(
            "Chaos",
            "Chaos, copy. One olive drab, one mystery. That's a combat loadout.",
            "chaos",
            "mismatch",
            "different",
            "random",
        ),
    ),
    T(
        "sunglasses",
        "{cs}, {tcs}, those sunglasses — high speed, or did you steal them from a gas station?",
        C(
            "High speed",
            "High speed, copy. Don't drop them in the seat. That's a hundred-dollar breakaway.",
            "high speed",
            "cool",
            "aviator",
            "oakley",
        ),
        C(
            "Gas station",
            "Gas station, copy. Honest. They probably work better than ours.",
            "gas station",
            "cheap",
            "stole",
            "walmart",
        ),
    ),
    T(
        "autopilot",
        "{cs}, {tcs}, you hand-flying this whole time or is George doing the work?",
        C(
            "Hand flying",
            "Hand flying, copy. That's why you look so smooth. Or so busy. One of those.",
            "hand",
            "hands",
            "manual",
            "flying",
        ),
        C(
            "George",
            "George, copy. Let the computer suffer. We'll still give you the credit.",
            "george",
            "autopilot",
            "auto",
            "ap",
        ),
    ),
    T(
        "jp8",
        "{cs}, {tcs}, favorite smell. Jet fuel, or the coffee we spilled in the galley?",
        C(
            "Jet fuel",
            "Jet fuel, copy. That's a lifestyle. Don't sniff too hard, we need you conscious.",
            "jet fuel",
            "jp8",
            "jp-8",
            "gas",
            "fuel",
        ),
        C(
            "Coffee",
            "Coffee, copy. Ours smells like regret and a burned K-cup. Still better than the latrine.",
            "coffee",
            "galley",
            "k cup",
        ),
    ),
    T(
        "maps",
        "{cs}, {tcs}, you still carry a paper map for luck, or is it all glass now?",
        C(
            "Paper",
            "Paper, copy. Old school. If the glass dies you can still find Nevada. Probably.",
            "paper",
            "map",
            "chart",
        ),
        C(
            "Glass",
            "Glass, copy. High speed. Don't draw on it with a grease pencil. We've seen that.",
            "glass",
            "mfd",
            "digital",
            "ipad",
        ),
    ),
    T(
        "sunset",
        "{cs}, {tcs}, that sunset out there — you seeing it, or are you staring at our belly?",
        C(
            "Sunset",
            "Sunset, copy. Pretty. Don't fly into it. That's how ballads start.",
            "sunset",
            "pretty",
            "seeing it",
        ),
        C(
            "Your belly",
            "Our belly, copy. That's a lot of gray. We'll try to be interesting.",
            "belly",
            "gray",
            "grey",
        ),
    ),
    T(
        "lottery",
        "{cs}, {tcs}, if we all won the lottery tomorrow, would you still show up for this, "
        "or buy an island?",
        C(
            "Still show",
            "Still show, copy. That's the sickness. We'd buy a nicer tanker and do this for fun.",
            "show",
            "still",
            "fly",
        ),
        C(
            "Island",
            "Island, copy. Send coordinates. Boom crew is requesting a TDY.",
            "island",
            "beach",
            "quit",
        ),
    ),
    T(
        "video_games",
        "{cs}, {tcs}, after you shut down. Video games, or are you too high-speed for that?",
        C(
            "Games",
            "Games, copy. Don't fly the Viper in the game tonight. Give it a rest.",
            "games",
            "game",
            "xbox",
            "playstation",
            "pc",
        ),
        C(
            "Too high speed",
            "Too high speed, copy. You'll be asleep in the truck. We believe you.",
            "high speed",
            "sleep",
            "no",
            "adult",
        ),
    ),
    T(
        "rain",
        "{cs}, {tcs}, you hoping it rains when you recover, or do you want a dry runway and a cold one?",
        C(
            "Rain",
            "Rain, copy. Dramatic. Don't hydroplane. We're not coming back for you.",
            "rain",
            "wet",
        ),
        C(
            "Dry",
            "Dry, copy. Professional. Cold one after, also professional. Boom approved.",
            "dry",
            "cold one",
            "beer",
        ),
    ),
    T(
        "bacon",
        "{cs}, {tcs}, breakfast ethics. Bacon on everything, or are you one of those yogurt people?",
        C(
            "Bacon",
            "Bacon, copy. That's a valid religion. Amen from the boom.",
            "bacon",
        ),
        C(
            "Yogurt",
            "Yogurt, copy. We don't trust it, but we admire the discipline.",
            "yogurt",
            "yoghurt",
            "healthy",
            "granola",
        ),
    ),
    T(
        "hot_sauce",
        "{cs}, {tcs}, you keep hot sauce in the jet, or is that a myth?",
        C(
            "Keep it",
            "Keep it, copy. Legend. If you drop the bottle we're writing a mishap report.",
            "keep",
            "yes",
            "tabasco",
            "sauce",
        ),
        C(
            "Myth",
            "Myth, copy. Disappointing. Boom crew believed in you.",
            "myth",
            "no",
            "don't",
        ),
    ),
    T(
        "left_right",
        "{cs}, {tcs}, in the Viper, you ever wish you had a copilot to complain to, "
        "or is solo the whole point?",
        C(
            "Want a copilot",
            "Want a copilot, copy. We have like eight. They all talk. It's not better.",
            "copilot",
            "want",
            "someone",
        ),
        C(
            "Solo",
            "Solo, copy. That's the point. We'll stop talking in a minute. Probably.",
            "solo",
            "alone",
            "point",
        ),
    ),
    T(
        "stick_shift",
        "{cs}, {tcs}, cars. Stick shift, or you let the car think for you too?",
        C(
            "Stick",
            "Stick, copy. Old school. Don't stall it in the parking lot after a tanker hop.",
            "stick",
            "manual",
            "clutch",
        ),
        C(
            "Automatic",
            "Automatic, copy. Save the skills for the jet. We support this.",
            "automatic",
            "auto",
            "slushbox",
        ),
    ),
    T(
        "camping",
        "{cs}, {tcs}, time off. Tent camping, or a hotel with actual water pressure?",
        C(
            "Camping",
            "Camping, copy. You already sit in a tiny seat for hours. Glutton for punishment.",
            "camping",
            "tent",
            "camp",
        ),
        C(
            "Hotel",
            "Hotel, copy. Water pressure. Boom crew is jealous. Our shower is a rumor.",
            "hotel",
            "motel",
            "water",
        ),
    ),
    T(
        "ufo",
        "{cs}, {tcs}, you seeing anything weird out there besides us, or is the sky behaving?",
        C(
            "Sky's fine",
            "Sky's fine, copy. Boring. We'll be your unidentified flying object for today.",
            "fine",
            "behaving",
            "nothing",
            "quiet",
        ),
        C(
            "Something weird",
            "Something weird, copy. If it's not us, don't chase it. That's how documentaries start.",
            "weird",
            "ufo",
            "lights",
            "something",
        ),
    ),
    T(
        "superstition",
        "{cs}, {tcs}, preflight superstition. Lucky patch, or you fly dirty?",
        C(
            "Lucky patch",
            "Lucky patch, copy. Don't tell the ops officer it's load-bearing.",
            "lucky",
            "patch",
            "ritual",
            "charm",
        ),
        C(
            "Fly dirty",
            "Fly dirty, copy. Confidence. Or you forgot the patch in the truck. We'll allow either.",
            "dirty",
            "nothing",
            "nope",
            "forgot",
        ),
    ),
    T(
        "callsign",
        "{cs}, {tcs}, you like your callsign, or are you still mad about how you got it?",
        C(
            "I like it",
            "You like it, copy. That's rare. Don't tell the naming committee or they'll change it.",
            "like",
            "love",
            "good",
        ),
        C(
            "Still mad",
            "Still mad, copy. That's how you know it's a real callsign. Ours is Texaco. We lost that fight.",
            "mad",
            "hate",
            "stupid",
            "story",
        ),
    ),
    T(
        "holding_hands",
        "{cs}, {tcs}, you realize we're basically holding hands at two hundred knots, right?",
        C(
            "Don't say that",
            "Don't say that, copy. Too late. It's on the tape. Stay stable, sweetheart.",
            "don't",
            "dont",
            "stop",
            "no",
        ),
        C(
            "Copy",
            "Copy, copy. Look at us. Two professionals. One hose. Beautiful.",
            "copy",
            "yeah",
            "true",
            "roger",
        ),
    ),
    T(
        "altitude",
        "{cs}, {tcs}, you happier down in the weeds, or is the high thirties growing on you?",
        C(
            "Weeds",
            "Weeds, copy. Fast and low. We'll stay up here with the coffee and the lawsuits.",
            "weeds",
            "low",
            "down",
            "nap",
        ),
        C(
            "High",
            "High, copy. Thin air, fat gas. That's the life. Don't get used to our speed.",
            "high",
            "thirties",
            "up",
            "cruise",
        ),
    ),
    T(
        "snacks_share",
        "{cs}, {tcs}, if we could lower a bag of chips down the boom, would you take it, "
        "or is that a FOD hazard?",
        C(
            "I'd take it",
            "You'd take it, copy. Noted. Legal says no. Morale says maybe. Legal wins.",
            "take",
            "chips",
            "yes",
        ),
        C(
            "FOD hazard",
            "FOD hazard, copy. Responsible. That's why you're on the boom and not in the snack pile.",
            "fod",
            "hazard",
            "no",
        ),
    ),
    T(
        "sports",
        "{cs}, {tcs}, you following any sports, or is that a peacetime hobby?",
        C(
            "Following",
            "Following, copy. Don't tell us the score. We have a guy who gets violent.",
            "following",
            "sports",
            "football",
            "yes",
        ),
        C(
            "Peacetime",
            "Peacetime, copy. Flying is the sport. We're the bench. Get your gas.",
            "peacetime",
            "no",
            "flying",
        ),
    ),
    T(
        "movies",
        "{cs}, {tcs}, tanker movie night. Top Gun, or something where the tanker actually lands the girl?",
        C(
            "Top Gun",
            "Top Gun, copy. They never show the boom. That's how you know it's fiction.",
            "top gun",
            "maverick",
            "gun",
        ),
        C(
            "Tanker movie",
            "Tanker movie, copy. Hasn't been made. Hollywood's scared of how cool we are.",
            "hollywood",
            "other",
            "real",
        ),
    ),
    T(
        "donuts",
        "{cs}, {tcs}, donut diplomacy. Glazed, or are you a filled-donut person?",
        C(
            "Glazed",
            "Glazed, copy. Simple. Effective. Like a good rejoin.",
            "glazed",
            "glaze",
        ),
        C(
            "Filled",
            "Filled, copy. High risk, high reward. Don't wear it. We can see your visor.",
            "filled",
            "cream",
            "jelly",
        ),
    ),
    T(
        "water_pressure",
        "{cs}, {tcs}, after you land, first stop. Shower, or the snack bar?",
        C(
            "Shower",
            "Shower, copy. You've earned it. We smell like JP-8 and burnt coffee. Same plan.",
            "shower",
            "bath",
        ),
        C(
            "Snack bar",
            "Snack bar, copy. Priorities. Gas the jet, gas the pilot. We support this doctrine.",
            "snack",
            "food",
            "bar",
        ),
    ),
    T(
        "window_aisle",
        "{cs}, {tcs}, if this tanker was an airliner, window or aisle?",
        C(
            "Window",
            "Window, copy. That's us. Big windows, no pretzels, occasional fighter hanging off the wing.",
            "window",
        ),
        C(
            "Aisle",
            "Aisle, copy. Bathroom access. That's the real luxury up here.",
            "aisle",
            "isle",
        ),
    ),
    T(
        "coffee_black",
        "{cs}, {tcs}, coffee. Black, or you putting enough cream in it to make it a dessert?",
        C(
            "Black",
            "Black, copy. That's a briefing. No notes. Carry on.",
            "black",
            "straight",
        ),
        C(
            "Cream",
            "Cream, copy. Dessert coffee. We won't tell the other Vipers.",
            "cream",
            "sugar",
            "sweet",
            "dessert",
        ),
    ),
    T(
        "last_name",
        "{cs}, {tcs}, you want us to keep using the callsign, or you got a first name we can ruin?",
        C(
            "Callsign",
            "Callsign, copy. Professional. We'll still invent a worse one in the debrief.",
            "callsign",
            "keep",
        ),
        C(
            "First name",
            "First name, copy. Dangerous. We'll forget it immediately and go back to Fleece.",
            "first",
            "name",
            "steve",
        ),
    ),
    T(
        "boredom",
        "{cs}, {tcs}, rank this. More boring — waiting on the boom, or waiting in the hold?",
        C(
            "The boom",
            "The boom, copy. Ouch. We'll try a dance. Don't join us.",
            "boom",
            "this",
        ),
        C(
            "The hold",
            "The hold, copy. Correct. At least here you get free gas and a conversation.",
            "hold",
            "holding",
            "orbit",
        ),
    ),
    T(
        "selfie",
        "{cs}, {tcs}, if GoPros were allowed, would you film this, or is it too embarrassing?",
        C(
            "I'd film it",
            "You'd film it, copy. Tag us. Boom crew wants residuals.",
            "film",
            "gopro",
            "yes",
        ),
        C(
            "Embarrassing",
            "Embarrassing, copy. Fair. You look like a little fish on a big hook. It's cute.",
            "embarrassing",
            "no",
            "don't",
        ),
    ),
    T(
        "lucky_gas",
        "{cs}, {tcs}, you treat this gas as lucky, or is it all just numbers on a tape?",
        C(
            "Lucky",
            "Lucky, copy. We'll put extra wishes in the hose. That's not a real procedure. Or is it.",
            "lucky",
            "luck",
        ),
        C(
            "Numbers",
            "Numbers, copy. Cold. Accurate. We'll still say good luck because we're nice.",
            "numbers",
            "tape",
            "math",
        ),
    ),
    T(
        "complain",
        "{cs}, {tcs}, you allowed to complain on this freq, or is that a debrief item?",
        C(
            "I'll complain",
            "You'll complain, copy. Hit us. The boom can take it. The tape cannot. Wait.",
            "complain",
            "yes",
            "allowed",
        ),
        C(
            "Debrief item",
            "Debrief item, copy. Professional. We'll complain for you. The coffee's cold.",
            "debrief",
            "no",
            "professional",
        ),
    ),
    T(
        "cookies",
        "{cs}, {tcs}, cookie crisis. Chocolate chip, or you one of those oatmeal people?",
        C(
            "Chocolate chip",
            "Chocolate chip, copy. That's a valid life. Don't crumble them in the G-suit.",
            "chocolate",
            "chip",
            "chips",
        ),
        C(
            "Oatmeal",
            "Oatmeal, copy. We respect the fiber. Boom crew is still stealing the chocolate ones.",
            "oatmeal",
            "raisin",
        ),
    ),
    T(
        "left_seat",
        "{cs}, {tcs}, in a two-seat jet, you grabbing left seat, or you like the maps?",
        C(
            "Left seat",
            "Left seat, copy. Front office. Don't make us ride in the back, we get airsick in fighters.",
            "left",
            "front",
            "pilot",
        ),
        C(
            "Maps",
            "Maps, copy. That's the smart seat. Somebody's got to know where Nevada went.",
            "maps",
            "wso",
            "back",
            "right",
        ),
    ),
    T(
        "ketchup_mustard",
        "{cs}, {tcs}, burger toppings. Ketchup, or mustard like a chaotic good?",
        C(
            "Ketchup",
            "Ketchup, copy. Classic. Don't get it on the mask. That's a visor emergency.",
            "ketchup",
            "catsup",
        ),
        C(
            "Mustard",
            "Mustard, copy. Bold yellow energy. We can work with that.",
            "mustard",
            "yellow",
        ),
    ),
    T(
        "winter_summer",
        "{cs}, {tcs}, you a winter flyer or a summer flyer? Be honest.",
        C(
            "Winter",
            "Winter, copy. Long johns and a dream. The boom likes the smooth air though.",
            "winter",
            "cold",
            "snow",
        ),
        C(
            "Summer",
            "Summer, copy. Cook yourself in the cockpit, then come ask us for gas. Fair.",
            "summer",
            "hot",
            "heat",
        ),
    ),
    T(
        "alarm",
        "{cs}, {tcs}, this morning. Did the alarm win, or did you bargain with snooze?",
        C(
            "Alarm won",
            "Alarm won, copy. That's why you're already on the boom. Overachiever.",
            "alarm",
            "won",
            "up",
        ),
        C(
            "Snooze",
            "Snooze, copy. Same. We bargained. The snooze lost on round four.",
            "snooze",
            "slept",
            "late",
        ),
    ),
    T(
        "pickle",
        "{cs}, {tcs}, pickle on the burger — yes, or are you a coward?",
        C(
            "Pickle",
            "Pickle, copy. Correct. That's a complete weapon system.",
            "pickle",
            "pickles",
            "yes",
        ),
        C(
            "No pickle",
            "No pickle, copy. We'll allow it. Quietly judging from the boom pod.",
            "no",
            "without",
            "coward",
        ),
    ),
    T(
        "radio",
        "{cs}, {tcs}, you actually like talking on the radio, or is this painful?",
        C(
            "I like it",
            "You like it, copy. Dangerous. We'll keep feeding you questions.",
            "like",
            "love",
            "yes",
        ),
        C(
            "Painful",
            "Painful, copy. Same. We'll shut up after this. No we won't.",
            "painful",
            "hate",
            "no",
        ),
    ),
    T(
        "halloween",
        "{cs}, {tcs}, Halloween. You dressing up, or is the flight suit already a costume?",
        C(
            "Dressing up",
            "Dressing up, copy. Send photos. Boom crew votes. We are not kind.",
            "dressing",
            "costume",
            "yes",
        ),
        C(
            "Flight suit",
            "Flight suit, copy. That's a year-round costume. High speed, low effort.",
            "flight suit",
            "already",
            "suit",
        ),
    ),
    T(
        "coffee_size",
        "{cs}, {tcs}, coffee size. Normal cup, or one of those buckets they sell now?",
        C(
            "Normal",
            "Normal, copy. That's a professional. We respect the twelve-ounce life.",
            "normal",
            "small",
            "cup",
        ),
        C(
            "Bucket",
            "Bucket, copy. That's a tanker of coffee. We see you. Kindred spirits.",
            "bucket",
            "venti",
            "large",
            "huge",
        ),
    ),
    T(
        "left_or_right",
        "{cs}, {tcs}, random. You hang the mask on the left, or the right, or is it chaos?",
        C(
            "Left",
            "Left, copy. We'll log that. For science. And gossip.",
            "left",
        ),
        C(
            "Right",
            "Right, copy. The other half of the squadron just lost a bet.",
            "right",
        ),
    ),
    T(
        "smooth_air",
        "{cs}, {tcs}, air feel. Smooth, or are we all riding a dirt road up here?",
        C(
            "Smooth",
            "Smooth, copy. Don't jinx it. We just jinxed it. Sorry.",
            "smooth",
            "fine",
            "good",
        ),
        C(
            "Dirt road",
            "Dirt road, copy. We'll hold her as still as this old jet allows.",
            "dirt",
            "rough",
            "bumpy",
            "chop",
        ),
    ),
    T(
        "mascot",
        "{cs}, {tcs}, if this tanker needed a mascot. Bear, or a slightly judgmental eagle?",
        C(
            "Bear",
            "Bear, copy. Big, slow, full of gas. That's on the nose and we accept it.",
            "bear",
        ),
        C(
            "Eagle",
            "Eagle, copy. Judgmental. That's the boom operator. Hi.",
            "eagle",
            "bird",
        ),
    ),
    T(
        "leftovers",
        "{cs}, {tcs}, dinner last night. Leftovers, or you cooked like a civilian?",
        C(
            "Leftovers",
            "Leftovers, copy. That's a fighter-pilot meal plan. Efficient and mysterious.",
            "leftovers",
            "left overs",
            "leftover",
        ),
        C(
            "I cooked",
            "You cooked, copy. Show-off. What was it, eggs and optimism?",
            "cooked",
            "cook",
            "civilian",
        ),
    ),
    T(
        "window_scratch",
        "{cs}, {tcs}, your canopy. Crystal, or you flying around with a bug cemetery up front?",
        C(
            "Crystal",
            "Crystal, copy. Overachiever. Don't let us sneeze on it.",
            "crystal",
            "clean",
            "clear",
        ),
        C(
            "Bugs",
            "Bugs, copy. That's a war record. Leave it. It's character.",
            "bugs",
            "bug",
            "dirty",
            "cemetery",
        ),
    ),
    T(
        "call_mom",
        "{cs}, {tcs}, after you land, you calling home, or going straight to the snack bar?",
        C(
            "Calling home",
            "Calling home, copy. That's a good human. Tell them the boom says hi.",
            "home",
            "mom",
            "calling",
            "call",
        ),
        C(
            "Snack bar",
            "Snack bar, copy. Honest. Fuel the pilot first. We support this doctrine.",
            "snack",
            "food",
            "bar",
        ),
    ),
    # ---- Riffs / open small talk (not every bit is an A/B poll) --------------
    Riff(
        "desert_glow",
        "Man, the desert looks like a glowing parking lot from up here. "
        "Whole valley just sitting there like it paid rent.",
        R(
            "Pretty",
            "Pretty, copy. Big windows, bad coffee, better scenery. Fair trade.",
            "pretty",
            "beautiful",
            "nice",
            "wow",
        ),
        R(
            "Boring",
            "Boring, copy. Yeah, after the tenth orbit it's just brown. "
            "Still beats the paperwork.",
            "boring",
            "meh",
            "whatever",
            "same",
        ),
    ),
    Riff(
        "boom_coffee_story",
        "Random boom fact while you're hanging out — somebody brought a French press "
        "up here once. Lasted three sorties. Then gravity won. Now it's Keurig and denial.",
        R(
            "Classic",
            "Classic, copy. We still talk about that press like it was a fallen hero.",
            "classic",
            "lol",
            "haha",
            "ha",
            "funny",
        ),
    ),
    Riff(
        "quiet_night",
        "Kinda quiet on the frequency tonight. Just us, the boom, and whatever song "
        "the navigator is humming that is definitely not in key.",
        R(
            "I hear it",
            "You hear it, copy. Tell him we said stop. He will not stop.",
            "hear",
            "hearing",
            "song",
            "music",
        ),
        R(
            "Peaceful",
            "Peaceful, copy. Good day for gas. Don't jinx it.",
            "peaceful",
            "quiet",
            "nice",
            "calm",
        ),
    ),
    Riff(
        "stable_looking",
        "You're looking real stable from here. Sit there, drink whatever counts as coffee "
        "in a Viper, and let us do the boring part.",
    ),
    Riff(
        "snack_jealousy",
        "Not gonna lie — if you've got a Pop-Tart down there, the boom crew is jealous. "
        "We can smell crumbs through the radio. Science.",
        R(
            "No crumbs",
            "No crumbs, copy. Responsible. We respect that and then eat yours in our heads.",
            "no",
            "none",
            "empty",
            "out",
        ),
        R(
            "Got one",
            "Got one, copy. Protect it. Do not crush it in the bag. That's a war crime.",
            "got",
            "have",
            "yes",
            "pop",
            "tart",
        ),
    ),
    Riff(
        "orbit_thoughts",
        "We've been drawing circles up here so long the autopilot filed for overtime. "
        "You're the entertainment. Try not to make it too interesting.",
    ),
    Riff(
        "window_seat",
        "Best seat on the jet is the boom — terrible coffee, excellent gossip, "
        "and a free show every time somebody joins. You're doing great, by the way.",
        R(
            "Thanks",
            "You bet. Keep it easy. We'll keep her steady.",
            "thanks",
            "thank",
            "appreciate",
            "cheers",
        ),
    ),
    Open(
        "weirdest_cockpit",
        "Open question while the gas is flowing — weirdest thing currently in your cockpit. "
        "Go.",
        R(
            "Snacks",
            "Snacks, copy. That's not weird, that's survival. Approved loadout.",
            "snack",
            "snacks",
            "food",
            "bar",
            "tart",
            "nugget",
        ),
        R(
            "Nothing",
            "Nothing, copy. Clean cockpit energy. Respect. Also suspicious.",
            "nothing",
            "empty",
            "clean",
            "none",
        ),
        R(
            "Phone",
            "Phone, copy. Don't drop it. We are not diving for a phone.",
            "phone",
            "cell",
            "iphone",
        ),
    ),
    Open(
        "after_this",
        "After this, what's the move — food, sleep, or pretending you still have hobbies?",
        R(
            "Food",
            "Food, copy. Correct priority. Tell the snack bar the boom sent you.",
            "food",
            "eat",
            "hungry",
            "taco",
            "burger",
            "pizza",
        ),
        R(
            "Sleep",
            "Sleep, copy. Autopilot and a dream. We'll keep the boom quiet.",
            "sleep",
            "nap",
            "bed",
            "rack",
            "tired",
        ),
        R(
            "Hobbies",
            "Hobbies, copy. Bold claim at this hour. We believe in you for about ten minutes.",
            "hobby",
            "hobbies",
            "pretend",
            "gaming",
            "gym",
        ),
    ),
    Open(
        "song_stuck",
        "What's the song stuck in your head right now? Be honest. The boom already knows if it's bad.",
        R(
            "Nothing",
            "Nothing, copy. Lucky. Ours is the navigator's off-key humming on loop.",
            "nothing",
            "none",
            "blank",
            "empty",
        ),
        R(
            "Country",
            "Country, copy. That's a long orbit song. Approved.",
            "country",
            "nashville",
        ),
        R(
            "Metal",
            "Metal, copy. Keep the volume where we can't hear the headbanging on the boom.",
            "metal",
            "rock",
            "heavy",
        ),
    ),
    Open(
        "best_gas_station",
        "Serious research question — best gas station snack of all time. We need data.",
        R(
            "Jerky",
            "Jerky, copy. Classic boom fuel. Chewy. Dependable. Judgmental.",
            "jerky",
            "beef",
        ),
        R(
            "Candy",
            "Candy, copy. Sugar and spite. Sustainable.",
            "candy",
            "chocolate",
            "skittles",
            "sour",
        ),
        R(
            "Chips",
            "Chips, copy. Loud bag, zero stealth. We still support this.",
            "chips",
            "chip",
            "doritos",
            "lays",
        ),
    ),
    Riff(
        "weather_report",
        "Unofficial weather from twenty-six thousand — clear, a little bumpy on the edges, "
        "and one hundred percent chance of somebody asking about coffee again.",
    ),
    Riff(
        "almost_done",
        "You're almost topped off from what we can see. Hang out, stay boring, "
        "and we'll get you back to collecting stories for the debrief.",
        R(
            "Copy",
            "Copy. Looking good. Almost there.",
            "copy",
            "roger",
            "wilco",
        ),
    ),
    # ---- Community rivalry (Navy / Mudhen / Eagle) — boom-crew approved -----
    T(
        "navy_or_air_force",
        "{cs}, {tcs}, serious boom poll. Who's harder to gas — Navy guys, or you lot?",
        C(
            "Navy",
            "Navy, copy. Correct. They show up like it's a boat and we're a floating pier. "
            "You're doing fine. Don't tell them we said that.",
            "navy",
            "naval",
            "boat",
            "carrier",
            "hornet",
            "super hornet",
        ),
        C(
            "Us",
            "Us, copy. Honesty. Rare on this freq. Still easier than a Mudhen "
            "who brought the whole jet and half the squadron's feelings.",
            "us",
            "vipers",
            "viper",
            "air force",
            "af",
            "me",
        ),
    ),
    T(
        "mudhen_or_eagle",
        "{cs}, {tcs}, boom crew argument. Who complains more on the radio — "
        "Mudhens or Eagles?",
        C(
            "Mudhens",
            "Mudhens, copy. Two seats, twice the opinions. We measured. "
            "Science is settled.",
            "mudhen",
            "mudhens",
            "strike eagle",
            "f fifteen e",
            "fifteen e",
        ),
        C(
            "Eagles",
            "Eagles, copy. One seat, infinite superiority. They gas like the "
            "boom owes them rent.",
            "eagle",
            "eagles",
            "f fifteen",
            "fifteen c",
            "fast eagle",
        ),
    ),
    T(
        "boat_vs_boom",
        "{cs}, {tcs}, if you had to pick — trap on a boat in the weather, "
        "or hang on our boom with this coffee?",
        C(
            "Boat",
            "Boat, copy. Bold. Tell the Navy we said hi and that their "
            "wiring diagrams still look like spaghetti.",
            "boat",
            "trap",
            "carrier",
            "boat trap",
            "deck",
        ),
        C(
            "Boom",
            "Boom, copy. Smart. Warm gas, bad jokes, no salt water. "
            "You're among friends.",
            "boom",
            "here",
            "yours",
            "tanker",
            "this",
        ),
    ),
    Riff(
        "navy_join",
        "Had a Navy guy on the boom last week. Asked if we had a 'ready deck.' "
        "Buddy, this is a KC-135. The deck is wherever the coffee spills.",
        R(
            "Classic",
            "Classic, copy. We still tell that story. They still don't get it.",
            "classic",
            "lol",
            "haha",
            "ha",
            "funny",
        ),
        R(
            "Heard worse",
            "Heard worse, copy. Hornet drivers invent new radio procedures mid-join. "
            "Keeps us young.",
            "worse",
            "heard",
            "hornet",
            "navy",
        ),
    ),
    Riff(
        "mudhen_opinions",
        "Unpopular boom opinion — Mudhens don't need two radios. They need one "
        "radio and a mute switch for the back seat. Don't quote me. Quote me.",
        R(
            "Facts",
            "Facts, copy. WSO's got the map, the jet, and the lecture. "
            "Pilot's just driving the gas station.",
            "facts",
            "true",
            "fact",
            "yes",
        ),
        R(
            "Harsh",
            "Harsh, copy. Fair. We'll still gas 'em. Slowly. With commentary.",
            "harsh",
            "mean",
            "ouch",
            "rude",
        ),
    ),
    Riff(
        "eagle_superiority",
        "Eagle pilots gas like they're doing us a favor. Big jet energy. "
        "Tiny patience. We smile, push gas, and save the jokes for after they're gone.",
        R(
            "Accurate",
            "Accurate, copy. Fast jet, faster ego. Still family. Distant family.",
            "accurate",
            "true",
            "yes",
            "facts",
        ),
        R(
            "Be nice",
            "Be nice, copy. Fine. They're pretty. The jet, I mean. Mostly the jet.",
            "nice",
            "be nice",
            "kind",
            "easy",
        ),
    ),
    Riff(
        "viper_favorite",
        "Between us — Vipers are our favorite. Quiet-ish, don't call the boom a "
        "'probe,' and you actually say thanks sometimes. Navy files that under optional.",
        R(
            "Thanks",
            "See? There it is. Written up. Boom crew morale just went up one percent.",
            "thanks",
            "thank",
            "appreciate",
            "cheers",
        ),
        R(
            "Don't tell Navy",
            "Don't tell Navy, copy. Too late. They're already writing a NATOPS change.",
            "navy",
            "don't",
            "secret",
            "quiet",
        ),
    ),
    Open(
        "worst_join_story",
        "Open mic — worst community join you've seen. Navy, Mudhen, Eagle, or "
        "somebody who shall remain nameless. Boom is taking notes.",
        R(
            "Navy",
            "Navy, copy. Filed under 'boat procedures, land edition.' "
            "We still laugh about it on the long orbits.",
            "navy",
            "boat",
            "hornet",
            "carrier",
        ),
        R(
            "Mudhen",
            "Mudhen, copy. Two voices, three directions, one confused boom operator. "
            "Classic Strike Eagle theater.",
            "mudhen",
            "mudhens",
            "strike",
            "wso",
        ),
        R(
            "Eagle",
            "Eagle, copy. Showed up already superior. Left still superior. "
            "Gas was the only thing that changed.",
            "eagle",
            "eagles",
            "fifteen",
        ),
    ),
    Open(
        "who_youd_gas_last",
        "Honest boom question — if fuel was short, who are you gassing last: "
        "Navy, Mudhen, or Eagle?",
        R(
            "Navy",
            "Navy last, copy. They can hold. Or find a boat. Not our problem. "
            "Mostly kidding. Mostly.",
            "navy",
            "boat",
            "hornet",
        ),
        R(
            "Mudhen",
            "Mudhen last, copy. They'll discuss it as a crew for twenty minutes "
            "anyway. Buys us time.",
            "mudhen",
            "mudhens",
            "strike",
        ),
        R(
            "Eagle",
            "Eagle last, copy. They'll claim they didn't need it. "
            "Then ask for more. Then claim they didn't need it.",
            "eagle",
            "eagles",
        ),
    ),
    Riff(
        "navy_callsigns",
        "Navy callsigns sound like a bar fight and a boat manual had a baby. "
        "Meanwhile you're over here just trying to drink cold coffee and not hit us. Respect.",
    ),
    Riff(
        "eagle_paint",
        "Saw an Eagle yesterday with paint so clean it looked offended to be on our boom. "
        "We gave him gas anyway. Charity work.",
        R(
            "Ha",
            "Ha, copy. Pretty jet, fragile ego, full tanks. Everybody wins.",
            "ha",
            "haha",
            "lol",
            "funny",
        ),
    ),
    Riff(
        "mudhen_map",
        "Mudhen back-seater once asked if we could 'hold the boom a second' while "
        "they folded a map. Sir. This is aviation. The map lost.",
        R(
            "WSO",
            "WSO, copy. Loves the jet, loves the fight, mildly confused by gas stations "
            "that fly. We get it.",
            "wso",
            "back seat",
            "backseater",
            "guy in back",
        ),
    ),
    Open(
        "riddle_offer",
        "Got a short riddle while the gas flows — want it, or you too busy flying?",
        R(
            "want",
            "Alright. I have cities but no houses, forests but no trees, "
            "water but no fish — what am I?",
            "want",
            "sure",
            "hit me",
            "riddle",
            "yes",
        ),
        R(
            "busy",
            "Fair. I'll just sit here and look professional. Boring.",
            "busy",
            "later",
            "no",
            "flying",
        ),
    ),
    T(
        "food_from_home",
        "First stop when you get home — Chick-fil-A, Whataburger, or you pretending salad?",
        C(
            "Chick-fil-A",
            "Copy. That's the one. We talk about it up here like it's a religion.",
            "chick",
            "fil-a",
            "fila",
            "cfa",
        ),
        C(
            "Whataburger",
            "Whataburger, copy. That's a Texas problem and I respect it.",
            "whataburger",
            "whata",
        ),
    ),
    Open(
        "worse_seat",
        "Be honest — Viper seat for an hour, or the boom pad staring at your intake. Who's more uncomfortable?",
        R(
            "viper",
            "Viper, copy. At least you can see where you're going. I get a close-up of your paint.",
            "viper",
            "fighter",
            "jet",
            "ejection",
        ),
        R(
            "boom",
            "Boom pad, copy. We lie on our stomachs and call it a career. Respect.",
            "boom",
            "pad",
            "pod",
            "tanker",
        ),
    ),
]


def library_size() -> int:
    return len(THREADS)


def thread_by_id(tid: str) -> dict[str, Any] | None:
    want = str(tid or "").strip()
    for row in THREADS:
        if str(row.get("id") or "") == want:
            return row
    return None


def pick_thread(avoid: set[str] | None = None) -> dict[str, Any]:
    skip = avoid or set()
    pool = [t for t in THREADS if str(t.get("id") or "") not in skip]
    if not pool:
        pool = list(THREADS)
    import random

    return random.choice(pool)
