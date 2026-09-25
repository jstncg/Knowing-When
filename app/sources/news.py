"""Company news from Google News' public RSS search: the headlines that name a company, for its funding rounds,
acquisitions and new leaders. Free, one request per company. The feed's shape (RSS 2.0 items with title, link and
pubDate) is from its public output as remembered, not yet checked against a live response: the cloud blocks it.

A headline counts only when the company is what it is about: the company comes right before the verb ("Acme raises
$50M", "Acme, the robot maker, to buy Birch", "Acme names a new head of autonomy", or "Max Example joins Acme as its
chief scientist"). A round needs a sum or a round's own word; a price, a stock, a list page or an estimate never counts,
nor does a plant or a site bought, nor a deal that fell through, and once one headline says a deal did, no headline of
that company's deals counts.

For the timing engine, ``employer`` reads the same feed for what happened to a watched person's employer: it was
bought (agreed or done, never talks or a rumor), or it is laying people off, as the employer's own events for
journey's news adapter.
"""

import re
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from ..models import iso
from .records import fetch_text

FEED = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
# Between the company and its verb: an aside in commas, and small words like "reportedly", "to" or "has".
BRIDGE = r"(?:\s*\([^)]*\))?(?:,[^,]{1,60},)?(?:\s+(?:\w+ly|to|be|has|have|is|will|just|now|also|said))*\s+"
LEGAL = {"ai", "inc", "co", "corp", "corporation", "ltd", "limited", "llc", "gmbh", "sa", "plc"}
SUFFIX = (r"(?:\s+(?:ai|inc|co|corp|corporation|ltd|limited|llc|gmbh|sa|plc|labs?|robotics|technologies|games|"
          r"studios|industries)\b\.?){0,2}")  # "Skild AI" is the account Skild AI or Skild; "Example Robotics Co." is Example Robotics
ORDER = (r"(?!\s+(?:\d|(?:thousands|hundreds|more|up to|back|shares|stock|robots?|robotaxis|vehicles|cars|chips|gpus|"
         r"drones|units|trucks)\b))")  # never an order or a buyback: "buys 2,000 robotaxis", "buys back shares"
# A sum that is not a round: the noun after it (at most two words on, none of them "in", "led", "to"...) is a deal
# for chips or compute, a prize, a fund of its own or a grant. "$100M led by Founders Fund" is a round.
NOT_ROUND = (r"(?!(?:\s+(?!(?:in|from|led|by|to|for|at|as|after|and|with)\b)[\w'-]+){0,2}\s+(?:deal|agreement|"
             r"partnership|gpus?|chips?|compute|prize|pool|fund|grants?|awards?)\b)")
# A round: its verb, then a sum or a round's own word within a few words ("raises nearly $500M", "closes its Series B").
ROUND = (r"(?:raises|raised|raising|closes|closed|secures|secured|lands|landed|nabs|bags|gets|receives|received|"
         r"announces|completes|picks up|snags)\b(?:\s+[^\s$€£¥]+){0,4}?\s+(?:(?:us|a|c)?[$€£¥]\s?\d[\d.,]*"
         r"(?:\s?(?:[mbk]n?|million|billion))?\b(?![.,]?\d)" + NOT_ROUND + r"|(?:funding|financing|round|series [a-f]|seed|investment)\b)")
# Property, not a company: what a buyer buys when it buys a plant, a factory or a site.
PROPERTY = (r"(?:plants?|factor(?:y|ies)|facilit(?:y|ies)|sites?|campus(?:es)?|buildings?|land|propert(?:y|ies)|"
            r"real estate|offices?|headquarters|warehouses?|fabs?|mines?|shipyards?|hangars?|data cent(?:er|re)s?)")
# A purchase of property, as the last word of what is bought ("in talks to buy Nissan's shuttered Oppama car plant -
# report"; "office software maker Birch" and "Birch to automate factories" are companies).
ASSET = (r"(?!(?:\s+(?!(?:and|with|plus|including|to|for|in|into|at|from|of|by|on|as|after|under|near)\b)[\w'’.-]+)"
         r"{0,4}?\s+" + PROPERTY + r"(?:\s+(?:for|in|to|from|at|near|on|as|with|and|of|after|by|"
         r"under|amid|over|worth)\b|\s*[^\w\s'’-]|\s+[-–—|(]|\s*$))")
BUY = (r"(?:(?:completes|completed|announces|announced|closes|closed)\s+(?:its\s+|the\s+|a\s+)?(?:definitive\s+)?"
       r"(?:acquisition of|agreement to acquire|deal to acquire)|acquires|acquired|acquire|acquiring|buys|buy|buying|"
       r"bought|snaps up|agree[sd]? to (?:buy|acquire))\b" + ORDER + ASSET)
# Reported talks count too: the headline's own words say they are talks ("Acme in talks to raise at a $2B valuation").
TALKS = r"in\s+(?:early\s+|advanced\s+|final\s+)?(?:talks|discussions)\s+(?:with\s+\w+\s+)?to\s+"
RAISE_TALKS = TALKS + r"raise\b"
DEAL_TALKS = TALKS + r"(?:buy|acquire|be\s+acquired|sell\s+itself)\b" + ORDER + ASSET
# The account bought: the buyer's verb right before its name, with at most a few words saying what it is; never its
# shares, its products or a part of it ("buys Acme robots", "acquires Acme's gaming unit").
BOUGHT = (r"\b(?:acquires|acquired|acquire|acquiring|buys|buy|buying|bought|snaps\s+up|(?:acquisition|purchase|"
          r"takeover)\s+of)\s+(?:(?:the|rival|ai|robotics|gaming|game|startup|firm|company|platform|developer|maker|"
          r"studio|open-source)\s+){0,3}")
PART = (r"(?!\s*['’]|\s+(?:shares|stock|robots?|humanoids?|units?|stake|assets|products?|devices?|drones?|vehicles|"
        r"systems?|gpus?|chips?|division|business|arm|" + PROPERTY + r")\b)" + ASSET)
NAMES = r"(?:appoints|appointed|names|named|hires|hired|taps|tapped|promotes|promoted)\b"
ROLE = r".*\b(?:head|chief|vp|vice president|director|lead|cto|cso)\b"
NOISE = re.compile(r"\b(?:how to|prices?|market cap|earnings|estimates?|forecasts?|analysts?|rating|fraud|lawsuit|"
                   r"sues|sued|list of|funding rounds|top \d+|contract|orders?|deal with|buy ?backs?|ipo|tokens?|"
                   r"crypto|prize|tournament|hackathon)\b", re.I)
# A deal that fell through, whoever's it was: a verb that says so a few words before the deal ("Anthropic cancels
# $6B acquisition of Decart", "FTC blocks $6B deal to buy Acme", "Birch rules out buying Acme"), or the deal a few words
# before words that say so ("Birch's bid to buy Acme fails", "the deal is off"). Without a deal in it, "won't",
# "cancels" or "pulls out" is other news ("Acme pulls out of CES").
DEAL = (r"(?:deals?|bids?|talks|negotiations|acquisitions?|takeovers?|mergers?|purchases?|buyouts?|pursuit|tie-up|"
        r"buy|buying|acquire|acquiring)")
DEAL_NOUN = r"(?:deal|bid|talks|negotiations|acquisition|takeover|merger|purchase|buyout|tie-up)s?"
# The few words between, never ones that tie the news to a deal that happened ("CEO to exit after Birch acquisition").
WORDS = (r"(?:\s+(?!(?:after|following|post|since|amid|despite|as|with|in|into|during|ahead)\b)[\w'’$€£¥~.,-]+)"
         r"{0,5}?\s+")
FELL = re.compile(
    r"\b(?:(?<!refuses to )(?<!refused to )(?<!declines to )(?<!declined to )(?:walk(?:s|ed|ing)? away|scrap(?:s|ped|ping)?|abandon(?:s|ed|ing)?|call(?:s|ed|ing)? off|"
    r"cancel(?:s|led|ed|ling|ing)?|cancellation|pull(?:s|ed|ing)? (?:out|(?:the )?plug)|back(?:s|ed|ing)? (?:out|away|off)|"
    r"decid(?:e|es|ed|ing) against|rul(?:e|es|ed|ing) out|gives? up|gave up|u-turn|withdr(?:aws?|awn|awing|ew)|"
    r"no longer|reject(?:s|ed|ing)?|terminat(?:e|es|ed|ing)|drop(?:s|ped|ping)?|halt(?:s|ed|ing)?|"
    r"shelv(?:e|es|ed|ing)|nix(?:es|ed)?|kill(?:s|ed)?|ax(?:e|es|ed)?|end(?:s|ed)?|block(?:s|ed|ing)?|"
    r"paus(?:e|es|ed)|suspend(?:s|ed)?|fail(?:s|ed)?|los(?:e|es|t|ing)|den(?:y|ies|ied)|exit(?:s|ed)?|"
    r"prohibit(?:s|ed)?|veto(?:es|ed)?|torpedo(?:es|ed)?|derail(?:s|ed)?)"
    r"(?!\s+(?:its\s+|the\s+|a\s+)?(?:challenge|opposition|objections?|probe|investigation|review|case)\b)"  # a regulator's
    + WORDS + DEAL + r"|"
    r"(?:won'?t|will not|opts? not to|decid(?:e|es|ed) not to)\s+(?:\w+\s+)?(?:buy|acquire|pursue|proceed)|"
    r"not (?:buy|buying|acquire|acquiring)|(?:no longer|not) in talks|" + DEAL_NOUN + WORDS + r"(?:f[ae]ll(?:s|en|ing)? (?:through|apart)|collaps(?:e|es|ed|ing)|"
    r"br(?:eak|eaks|oke|oken) down|fail(?:s|ed)?|dies|died|dead|unravel(?:s|ed|led)?|derailed|fizzl(?:e|es|ed)|"
    r"stall(?:s|ed)?|laps(?:e|es|ed)|off the table|(?:is|are|now) off|scrapped|cancell?ed|called off|blocked|terminated|abandoned)|"
    r"deal['’]s off)\b", re.I)


def _name(company):
    """The company's name as a pattern: whole words, with or without a legal or company suffix."""
    words = company.split()
    while len(words) > 1 and words[-1].lower() in LEGAL:  # "placeholder autonomy inc" is also written "Placeholder Autonomy"
        words.pop()
    return rf"(?<!\w){re.escape(' '.join(words))}{SUFFIX}(?![\w-])"


def deal(headline, company):
    """Whether ``headline`` says the company buys a company or is bought (never property, never a deal that fell
    through): a "funding" ``kind`` that need not be a round."""
    name = _name(company)
    return not (NOISE.search(headline) or FELL.search(headline)) and bool(
        re.search(rf"{name}{BRIDGE}(?:{BUY}|{DEAL_TALKS})|{BOUGHT}{name}{PART}", headline, re.I))


def kind(headline, company):
    """"funding" (a round, reported talks for one, or an acquisition by or of the company, ``deal``), "new_lead", or
    None: what ``headline`` says happened to ``company``, with the company as its subject, or as what a buyer buys. A
    company name followed by a hyphen ("Scale AI-Powered") is not the company. A deal that fell through voids only
    the deal: "Acme raises $50M after Birch deal falls through" is still a round."""
    if NOISE.search(headline):
        return None
    name = _name(company)
    if re.search(rf"{name}{BRIDGE}(?:{ROUND}|{RAISE_TALKS})", headline, re.I) or deal(headline, company):
        return "funding"
    if re.search(rf"{name}{BRIDGE}{NAMES}{ROLE}|\b(?:joins|joined|to join)\s+{name}{ROLE}", headline, re.I):
        return "new_lead"
    return None


def called_off(headline, company):
    """Whether ``headline`` names the company and says a deal fell through: then every deal headline about it is
    void, however it was worded before ("Anthropic in talks to buy Decart", then "Anthropic walks away from Decart")."""
    return bool(FELL.search(headline) and re.search(_name(company), headline, re.I))


def headlines(fetch, company, days=30):
    """``read``'s headlines alone."""
    return read(fetch, company, days)[0]


def _feed(fetch, query):
    """[(headline, link, published)] from one search, published an ISO time in UTC; the feed appends " - Publisher" to
    each title, which goes. A link that is not https is "", and a date that won't read is None: a caller leaves such an
    item out, since it can't open a window or be checked."""
    text, _ = fetch_text(fetch, FEED.format(query=quote(query)))
    out = []
    for item in ET.fromstring(text).iter("item"):
        link = item.findtext("link", "")
        try:
            at = parsedate_to_datetime(item.findtext("pubDate") or "")
            at = iso(at if at.tzinfo else at.replace(tzinfo=timezone.utc))
        except (TypeError, ValueError):
            at = None
        out.append((item.findtext("title", "").rsplit(" - ", 1)[0], link if link.startswith("https://") else "", at))
    return out


def read(fetch, company, days=30):
    """Headlines of the last ``days`` about ``company``, each {kind, day, quote, source_url}: kind is funding or
    new_lead by the headline's own words (``kind``); other news is left out, and so is every deal headline when one
    in the feed says the deal fell through (``called_off``). Returns them and that headline, or None."""
    items = _feed(fetch, f'"{company}" when:{days}d')
    off = next((headline for headline, *_ in items if called_off(headline, company)), None)
    out = [{"kind": what, "day": at[:10], "quote": headline, "source_url": link} for headline, link, at in items
           if (what := kind(headline, company)) and link and at and not (off and deal(headline, company))]
    return out, off


# A rumor or a plan under thought is no moment: "said to", "nears a deal", "weighs layoffs".
RUMOR = re.compile(r"\b(?:said to|reportedly|reports?|rumou?rs?|nears?|nearing|eyes|eyeing|weighs|weighing|considers|"
                   r"considering|explores|exploring|mulls|mulling|could|might|bids?|offers? for|approach(?:es|ed)|"
                   r"in (?:early |advanced |final )?(?:talks|discussions))\b"
                   # A question, a fact-check or a joke is no report: "Did Birch buy Discord? Fact-checking...".
                   r"|\?|^\W*(?:did|does|do|is|are|was|were|will|would|can|could|should|why|how|what|who)\b|"
                   r"\b(?:fact[- ]?check\w*|viral|claims?|claimed|hoax|fake|false|debunk\w*|satire|april fools?)\b", re.I)
# The name ends what is bought: "Birch acquires Apple supplier Luxshare" is not Apple bought, "Meta Materials" not Meta.
END = (r"(?=\s*$|\s*[,;:.!?()\[\]|\"“”]|\s+[-–—]|\s+(?:for|in|from|after|to|at|amid|as|and|with|over|worth|on|by|"
       r"via)\b)")
# The company sold: its name, then the passive or the sale ("Acme to be acquired by Birch", "Acme sells itself to Birch").
SOLD = (r"(?:(?:to be|is being|agrees? to be|was|has been|being)\s+)?(?:acquired|bought|snapped up|taken over)\s+by\b|"
        r"(?:agrees? to sell|to sell|sells|sold)\s+(?:itself\s+)?to\b")
DONE = r"\b(?:completes|completed|closes|closed|finalizes|finalises|finalized|finalised)\s+(?:its\s+|the\s+)?"
# Its own layoffs: "lays off" whatever follows; a cut only of jobs or people ("cuts 10% of staff", not "cuts prices").
PEOPLE = r"(?:jobs|staff|staffers|workers|employees|roles|positions|workforce|headcount|people|engineers|percent|%)"
CUT = (r"(?:lays off|laid off|laying off|to lay off|will lay off|layoffs|announces layoffs|begins layoffs|"
       r"(?:cuts|to cut|will cut|cutting|slashes|to slash|sheds|to shed|axes|to axe|eliminates|to eliminate)"
       r"(?:\s+[^\s]+){0,5}?\s*" + PEOPLE + r")(?!\w)")
NO_CUT = re.compile(r"\b(?:den(?:y|ies|ied)|no (?:plans?|more)|rules? out|won'?t|will not|not (?:lay|cut|planning)|"
                    r"avoids?|averts?|reverses?|halts?)\b", re.I)
FORMAL = LEGAL - {"ai"}  # "Scale AI" is searched as written
DEAL_DAYS = 730  # a sale called off voids the deal headlines of the two years before it (Adobe and Figma: 15 months)


def _plain(company):
    """The company as headlines write it: "Acme Robotics, Inc." is "Acme Robotics"."""
    words = company.replace(",", " ").split()
    while len(words) > 1 and words[-1].lower().rstrip(".") in FORMAL:
        words.pop()
    return " ".join(words)


def bought(headline, company):
    """Whether ``headline`` says ``company`` itself is bought, agreed or done ("Birch to acquire Acme", "Acme to be
    acquired by Birch", "Birch completes Acme acquisition"): never talks or a rumor, never its shares, a unit, its
    property or a longer name that starts with it, never a deal called off."""
    if NOISE.search(headline) or FELL.search(headline) or RUMOR.search(headline):
        return False
    name = _name(_plain(company))
    return bool(re.search(rf"{BOUGHT}{name}{END}|{name}{BRIDGE}(?:{SOLD})|{DONE}{name}\s+(?:acquisition|takeover|purchase)\b",
                          headline, re.I))


def layoffs(headline, company):
    """Whether ``headline`` says ``company`` is laying people off, with the company as its subject ("Acme lays off 200",
    "Acme to cut 10% of staff", "Layoffs at Acme"): never a denial, a rumor or someone else's cuts."""
    if NOISE.search(headline) or NO_CUT.search(headline) or RUMOR.search(headline):
        return False
    name = _name(_plain(company))
    return bool(re.search(rf"{name}{BRIDGE}{CUT}|{name}['’]s\s+(?:layoffs|job cuts)\b|\blayoffs (?:at|hit|rock) {name}{END}",
                          headline, re.I))


def sale_called_off(headline, company):
    """Whether ``headline`` says a deal to buy ``company`` fell through ("Birch walks away from Acme deal", "Adobe
    abandons $20 billion Figma deal"): never the company's own bid ("Acme drops bid to buy Birch"), never other news."""
    name = _name(_plain(company))
    return bool(FELL.search(headline) and not NOISE.search(headline) and re.search(name, headline, re.I)
                and not re.search(rf"{name}{BRIDGE}.*\b(?:buy|buying|acquire|acquiring|(?:acquisition|purchase|takeover) "
                                  r"of|bid for)\b", headline, re.I))


def employer(fetch, company):
    """What the news says happened to a person's employer, as its own events: each {event_type, day, at (when it was
    published), quote, source_url}, event_type "acquisition" (``bought``), "layoffs_reported" (``layoffs``) or
    "acquisition_called_off" (``sale_called_off``: a deal to buy it fell through, which voids the deal headlines of
    the two years before it, DEAL_DAYS, from the day it was published: journey._news). Two searches, for those words
    and for a deal falling through, each with no time limit, so the feed's own cap on items decides how far back each
    reaches and call-offs never crowd out the rest."""
    company = _plain(company)
    items = [(headline, link, at) for query in (
                 f'"{company}" (acquired OR acquire OR acquisition OR "lays off" OR layoffs OR "job cuts")',
                 f'"{company}" ("called off" OR "walks away" OR scraps OR abandons OR cancels OR terminates) '
                 '(deal OR acquisition OR takeover OR merger)')
             for headline, link, at in _feed(fetch, query) if link and at]
    items = list(dict.fromkeys(items))  # a headline both searches found, once
    out = []
    for headline, link, at in items:
        if layoffs(headline, company):
            event_type = "layoffs_reported"
        elif sale_called_off(headline, company):
            event_type = "acquisition_called_off"
        elif bought(headline, company):
            event_type = "acquisition"
        else:
            continue
        out.append({"event_type": event_type, "day": at[:10], "at": at, "quote": headline, "source_url": link})
    return out
