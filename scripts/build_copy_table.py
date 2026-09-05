"""Write copy/approved.json: every message the sender could ever need.

    python -m scripts.build_copy_table [--check]

The send path used to ask an LLM for a message per failed payment. The copy
only varies by failure reason, language and kind of business, so the whole
space is a few hundred strings -- written once, checked once, committed, and
looked up thereafter.

The copy here is hand-authored rather than model-generated. That keeps the
build reproducible and free, and it means what ships is what a person wrote.
The structure a copywriter would actually use is the structure used here: one
core text per (intent, locale, variant), and per-vertical nouns for the thing
being bought, composed into a cell for every combination. Composition is how
the grid stays consistent across 700 cells; the wording in CORE and VERTICALS
is the authored part.

Every cell is validated before it is written -- the four trust rules, the
placeholder allowlist, and brace balance. A cell that fails is not written,
so an unreviewable string cannot reach the table.

--check exits non-zero if the committed file does not match what this script
would produce, which is what stops the table and the writer drifting apart.
"""

import argparse
import datetime
import json
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.copy_cache import (  # noqa: E402
    ALLOWED_PLACEHOLDERS,
    APPROVED,
    LOCALES,
    PROMPT_VERSION,
    VARIANTS,
    VERTICALS,
    key,
)
from app.messages import validate_template  # noqa: E402
from app.rules import RULES  # noqa: E402

# What the customer is buying, per vertical and locale. `thing` is the noun for
# the purchase; `kept` is what is being held for them while they sort the
# payment out. These are the only words a vertical changes.
VERTICAL_WORDS = {
    "generic":       {"en": ("order", "order"),          "hi": ("ऑर्डर", "ऑर्डर"),        "hinglish": ("order", "order")},
    "food_delivery": {"en": ("order", "order"),          "hi": ("ऑर्डर", "ऑर्डर"),        "hinglish": ("order", "order")},
    "ecommerce":     {"en": ("order", "cart"),           "hi": ("ऑर्डर", "कार्ट"),        "hinglish": ("order", "cart")},
    "edtech":        {"en": ("enrolment", "seat"),       "hi": ("एनरोलमेंट", "सीट"),      "hinglish": ("enrolment", "seat")},
    "saas":          {"en": ("subscription", "plan"),    "hi": ("सब्सक्रिप्शन", "प्लान"),  "hinglish": ("subscription", "plan")},
    "travel":        {"en": ("booking", "booking"),      "hi": ("बुकिंग", "बुकिंग"),      "hinglish": ("booking", "booking")},
    "healthcare":    {"en": ("appointment", "slot"),     "hi": ("अपॉइंटमेंट", "स्लॉट"),   "hinglish": ("appointment", "slot")},
    "services":      {"en": ("booking", "booking"),      "hi": ("बुकिंग", "बुकिंग"),      "hinglish": ("booking", "booking")},
}

# Named alternatives, for the rules where retrying the same card cannot work.
ALT_METHOD = {
    "en": "UPI, net banking or a different card",
    "hi": "UPI, नेट बैंकिंग या कोई दूसरा कार्ड",
    "hinglish": "UPI, net banking ya koi dusra card",
}

# One core text per (message intent, locale, variant). {thing} and {kept} are
# filled per vertical; everything in braces after that is a real slot filled at
# send time. Deliberately free of urgency, of any amount other than {amount},
# and of any request for information.
CORE: dict[str, dict[str, list[str]]] = {
    # R1 -- the connection dropped. They are still holding their phone.
    "reassure_and_resume": {
        "en": [
            "Hi {customer_name}, your payment for {thing} {order_id} ({item_names}) did not go through at {attempt_time} because the connection dropped. Nothing was charged, and your {kept} is saved at {amount}. You can finish it here: {resume_url} - {merchant_name}",
            "Hi {customer_name}, the connection dropped while your payment for {order_id} ({item_names}) was going through at {attempt_time}, so it did not complete. Nothing was charged. Your {kept} is still saved at {amount}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, your {thing} {order_id} ({item_names}) is saved at {amount}. The payment at {attempt_time} was interrupted by a network drop and nothing was charged. Pick it up here whenever you like: {resume_url} - {merchant_name}",
        ],
        "hi": [
            "नमस्ते {customer_name}, {attempt_time} पर आपके {thing} {order_id} ({item_names}) का पेमेंट कनेक्शन टूटने की वजह से पूरा नहीं हुआ। कोई राशि नहीं कटी है और आपका {kept} {amount} पर सुरक्षित है। यहाँ से पूरा करें: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, {order_id} ({item_names}) के लिए {attempt_time} पर पेमेंट के दौरान कनेक्शन टूट गया, इसलिए वह पूरा नहीं हुआ। कुछ भी चार्ज नहीं हुआ है। आपका {kept} {amount} पर सुरक्षित है: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, आपका {thing} {order_id} ({item_names}) {amount} पर सुरक्षित रखा है। {attempt_time} पर नेटवर्क टूटने से पेमेंट रुक गया था और कोई राशि नहीं कटी। जब सुविधा हो, यहाँ से पूरा कर लें: {resume_url} - {merchant_name}",
        ],
        "hinglish": [
            "Hi {customer_name}, {attempt_time} par aapke {thing} {order_id} ({item_names}) ka payment connection drop hone ki wajah se complete nahi hua. Kuch bhi charge nahi hua hai aur aapka {kept} {amount} par saved hai. Yahan se finish karein: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {order_id} ({item_names}) ke payment ke waqt {attempt_time} par connection tut gaya, isliye woh complete nahi hua. Kuch charge nahi hua. Aapka {kept} {amount} par saved hai: {resume_url} - {merchant_name}",
            "Hi {customer_name}, aapka {thing} {order_id} ({item_names}) {amount} par saved hai. {attempt_time} ka payment network drop se ruk gaya tha, kuch charge nahi hua. Jab time mile, yahan se complete kar lein: {resume_url} - {merchant_name}",
        ],
    },
    # R2 -- a typo. High intent, trivially fixable.
    "reenter_details": {
        "en": [
            "Hi {customer_name}, the card details entered for {thing} {order_id} ({item_names}) at {attempt_time} did not match your bank's records, so the payment did not go through. Nothing was charged and your {kept} is saved at {amount}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, your payment for {order_id} ({item_names}) at {attempt_time} did not complete because the card details did not match what the bank had on file. Nothing was charged. Your {kept} is saved at {amount}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, the {thing} {order_id} ({item_names}) is still saved at {amount}. The card details at {attempt_time} did not match the bank's records, so nothing was charged. You can put them in again here: {resume_url} - {merchant_name}",
        ],
        "hi": [
            "नमस्ते {customer_name}, {attempt_time} पर {thing} {order_id} ({item_names}) के लिए डाली गई कार्ड डिटेल आपके बैंक के रिकॉर्ड से मेल नहीं खाई, इसलिए पेमेंट पूरा नहीं हुआ। कोई राशि नहीं कटी और आपका {kept} {amount} पर सुरक्षित है: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, {order_id} ({item_names}) का पेमेंट {attempt_time} पर इसलिए पूरा नहीं हुआ क्योंकि कार्ड डिटेल बैंक के रिकॉर्ड से मेल नहीं खाई। कुछ भी चार्ज नहीं हुआ। आपका {kept} {amount} पर सुरक्षित है: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, {thing} {order_id} ({item_names}) {amount} पर सुरक्षित है। {attempt_time} पर दी गई कार्ड डिटेल बैंक रिकॉर्ड से मेल नहीं खाई, इसलिए कुछ नहीं कटा। यहाँ दोबारा डाल सकते हैं: {resume_url} - {merchant_name}",
        ],
        "hinglish": [
            "Hi {customer_name}, {attempt_time} par {thing} {order_id} ({item_names}) ke liye daali gayi card details bank ke record se match nahi hui, isliye payment complete nahi hua. Kuch charge nahi hua aur aapka {kept} {amount} par saved hai: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {order_id} ({item_names}) ka payment {attempt_time} par isliye nahi hua kyunki card details bank ke record se match nahi hui. Kuch charge nahi hua. Aapka {kept} {amount} par saved hai: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {thing} {order_id} ({item_names}) {amount} par saved hai. {attempt_time} par di gayi card details bank record se match nahi hui, isliye kuch charge nahi hua. Yahan dobara daal sakte hain: {resume_url} - {merchant_name}",
        ],
    },
    # R3 -- the bank verification step did not complete. Offer an escape hatch.
    "retry_or_switch_to_upi": {
        "en": [
            "Hi {customer_name}, the bank verification step for {thing} {order_id} ({item_names}) did not complete at {attempt_time}, so the payment was not taken. Your {kept} is saved at {amount}. You can try the card ending {last4} again, or pay by {alt_method}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, your payment for {order_id} ({item_names}) at {attempt_time} did not finish the bank's verification step, so nothing was taken. The {kept} is saved at {amount} -- use the card ending {last4} again or {alt_method}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, the bank check on {thing} {order_id} ({item_names}) did not complete at {attempt_time} and no money was taken. Your {kept} is held at {amount}. The card ending {last4} will work, and so will {alt_method}: {resume_url} - {merchant_name}",
        ],
        "hi": [
            "नमस्ते {customer_name}, {attempt_time} पर {thing} {order_id} ({item_names}) के लिए बैंक का वेरिफिकेशन स्टेप पूरा नहीं हुआ, इसलिए पेमेंट नहीं लिया गया। आपका {kept} {amount} पर सुरक्षित है। {last4} पर खत्म होने वाला कार्ड दोबारा आज़मा सकते हैं, या {alt_method} से भुगतान करें: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, {order_id} ({item_names}) का पेमेंट {attempt_time} पर बैंक के वेरिफिकेशन स्टेप तक पूरा नहीं हो पाया, कोई राशि नहीं ली गई। {kept} {amount} पर सुरक्षित है -- {last4} वाला कार्ड फिर से इस्तेमाल करें या {alt_method}: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, {attempt_time} पर {thing} {order_id} ({item_names}) की बैंक जाँच पूरी नहीं हुई और कोई पैसा नहीं कटा। आपका {kept} {amount} पर रखा है। {last4} वाला कार्ड चलेगा, और {alt_method} भी: {resume_url} - {merchant_name}",
        ],
        "hinglish": [
            "Hi {customer_name}, {attempt_time} par {thing} {order_id} ({item_names}) ke liye bank ka verification step complete nahi hua, isliye payment nahi liya gaya. Aapka {kept} {amount} par saved hai. {last4} wala card dobara try kar sakte hain, ya {alt_method} se pay karein: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {order_id} ({item_names}) ka payment {attempt_time} par bank ke verification step tak complete nahi hua, kuch nahi kata. {kept} {amount} par saved hai -- {last4} wala card phir se use karein ya {alt_method}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {attempt_time} par {thing} {order_id} ({item_names}) ki bank check poori nahi hui aur koi paisa nahi kata. Aapka {kept} {amount} par hai. {last4} wala card chalega, aur {alt_method} bhi: {resume_url} - {merchant_name}",
        ],
    },
    # R4 -- the outage has cleared by the time this sends.
    "bank_was_down_try_now": {
        "en": [
            "Hi {customer_name}, your payment for {thing} {order_id} ({item_names}) could not be processed at {attempt_time} because the bank's payment system was briefly unavailable. That has been resolved. Nothing was charged and your {kept} is saved at {amount}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, the bank was having trouble at {attempt_time}, so the payment for {order_id} ({item_names}) did not go through. It is working again now. Nothing was charged, and the {kept} is saved at {amount}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, a brief problem at the bank stopped your payment for {thing} {order_id} ({item_names}) at {attempt_time}. It has cleared. No money was taken and your {kept} is held at {amount}: {resume_url} - {merchant_name}",
        ],
        "hi": [
            "नमस्ते {customer_name}, {attempt_time} पर बैंक का पेमेंट सिस्टम कुछ देर के लिए उपलब्ध नहीं था, इसलिए {thing} {order_id} ({item_names}) का पेमेंट प्रोसेस नहीं हो सका। अब वह ठीक हो गया है। कोई राशि नहीं कटी और आपका {kept} {amount} पर सुरक्षित है: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, {attempt_time} पर बैंक में दिक्कत थी, इसलिए {order_id} ({item_names}) का पेमेंट नहीं हुआ। अब सब ठीक है। कुछ भी चार्ज नहीं हुआ और {kept} {amount} पर सुरक्षित है: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, {attempt_time} पर बैंक की एक छोटी समस्या ने {thing} {order_id} ({item_names}) का पेमेंट रोक दिया था। वह अब ठीक हो चुकी है। कोई पैसा नहीं लिया गया और आपका {kept} {amount} पर रखा है: {resume_url} - {merchant_name}",
        ],
        "hinglish": [
            "Hi {customer_name}, {attempt_time} par bank ka payment system thodi der ke liye unavailable tha, isliye {thing} {order_id} ({item_names}) ka payment process nahi ho paya. Ab woh theek ho gaya hai. Kuch charge nahi hua aur aapka {kept} {amount} par saved hai: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {attempt_time} par bank mein dikkat thi, isliye {order_id} ({item_names}) ka payment nahi hua. Ab sab theek hai. Kuch charge nahi hua aur {kept} {amount} par saved hai: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {attempt_time} par bank ki ek choti si problem ne {thing} {order_id} ({item_names}) ka payment rok diya tha. Woh ab clear ho chuki hai. Koi paisa nahi liya gaya aur aapka {kept} {amount} par hai: {resume_url} - {merchant_name}",
        ],
    },
    # R5 -- never state the reason. It is embarrassing, and it costs the sale.
    "soft_cart_reminder": {
        "en": [
            "Hi {customer_name}, your cart at {merchant_name} is still saved: {item_names}, {amount} in total, the same price as when you left it. Whenever you are ready: {resume_url}",
            "Hi {customer_name}, just so you know, {item_names} is still in your cart at {merchant_name}, {amount} in total and unchanged. Come back to it whenever suits: {resume_url}",
            "Hi {customer_name}, your cart is waiting at {merchant_name} -- {item_names}, {amount}, at the same price. No rush at all: {resume_url}",
        ],
        "hi": [
            "नमस्ते {customer_name}, {merchant_name} पर आपका कार्ट अब भी सुरक्षित है: {item_names}, कुल {amount}, उसी कीमत पर जिस पर आपने छोड़ा था। जब आप तैयार हों: {resume_url}",
            "नमस्ते {customer_name}, आपके {merchant_name} कार्ट में {item_names} अब भी रखा है, कुल {amount} और कीमत वही है। जब सुविधा हो, लौट आइए: {resume_url}",
            "नमस्ते {customer_name}, {merchant_name} पर आपका कार्ट इंतज़ार कर रहा है -- {item_names}, {amount}, उसी कीमत पर। कोई जल्दी नहीं: {resume_url}",
        ],
        "hinglish": [
            "Hi {customer_name}, {merchant_name} par aapka cart abhi bhi saved hai: {item_names}, total {amount}, usi price par jis par chhoda tha. Jab aap ready hon: {resume_url}",
            "Hi {customer_name}, aapke {merchant_name} cart mein {item_names} abhi bhi rakha hai, total {amount} aur price wahi hai. Jab time mile, wapas aa jaaiye: {resume_url}",
            "Hi {customer_name}, {merchant_name} par aapka cart wait kar raha hai -- {item_names}, {amount}, usi price par. Koi jaldi nahi: {resume_url}",
        ],
    },
    # R6 / R10 -- retrying the same card is structurally impossible.
    "must_use_alternate_method": {
        "en": [
            "Hi {customer_name}, the card ending {last4} is not set up for online payments by your bank, which is why {thing} {order_id} ({item_names}) did not go through at {attempt_time}. Trying the same card again will not work. You can pay the same total, {amount}, by {alt_method}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, your bank does not allow the card ending {last4} to be used online, so the payment for {order_id} ({item_names}) at {attempt_time} could not go through. The same card will keep being refused. {alt_method} will work for the same {amount}: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {thing} {order_id} ({item_names}) is saved at {amount}. The card ending {last4} is blocked for online use by your bank, so it cannot complete this payment however many times it is tried. Use {alt_method} instead: {resume_url} - {merchant_name}",
        ],
        "hi": [
            "नमस्ते {customer_name}, {last4} पर खत्म होने वाला कार्ड आपके बैंक ने ऑनलाइन पेमेंट के लिए चालू नहीं किया है, इसीलिए {attempt_time} पर {thing} {order_id} ({item_names}) का पेमेंट नहीं हुआ। वही कार्ड दोबारा आज़माने से काम नहीं बनेगा। उतनी ही राशि {amount} आप {alt_method} से दे सकते हैं: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, आपका बैंक {last4} वाले कार्ड को ऑनलाइन इस्तेमाल की अनुमति नहीं देता, इसलिए {attempt_time} पर {order_id} ({item_names}) का पेमेंट नहीं हो सका। वही कार्ड बार-बार मना ही करेगा। उसी {amount} के लिए {alt_method} काम करेगा: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, {thing} {order_id} ({item_names}) {amount} पर सुरक्षित है। {last4} वाला कार्ड बैंक ने ऑनलाइन इस्तेमाल के लिए रोका हुआ है, इसलिए कितनी भी बार कोशिश करें यह पेमेंट पूरा नहीं करेगा। इसकी जगह {alt_method} इस्तेमाल करें: {resume_url} - {merchant_name}",
        ],
        "hinglish": [
            "Hi {customer_name}, {last4} par khatam hone wala card aapke bank ne online payments ke liye enable nahi kiya hai, isiliye {attempt_time} par {thing} {order_id} ({item_names}) ka payment nahi hua. Wahi card dobara try karne se kaam nahi banega. Utni hi total {amount} aap {alt_method} se de sakte hain: {resume_url} - {merchant_name}",
            "Hi {customer_name}, aapka bank {last4} wale card ko online use karne ki permission nahi deta, isliye {attempt_time} par {order_id} ({item_names}) ka payment nahi ho paya. Wahi card baar baar mana hi karega. Usi {amount} ke liye {alt_method} chalega: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {thing} {order_id} ({item_names}) {amount} par saved hai. {last4} wala card bank ne online use ke liye rok rakha hai, isliye kitni bhi baar try karein yeh payment complete nahi karega. Iski jagah {alt_method} use karein: {resume_url} - {merchant_name}",
        ],
    },
    # R7 / R9 -- the bank refused and did not say why. One message, suggest
    # something different rather than a repeat.
    "try_different_method": {
        "en": [
            "Hi {customer_name}, your bank declined the payment for {thing} {order_id} ({item_names}) at {attempt_time} and nothing was charged. Your {kept} is saved at {amount}. {alt_method} usually goes through: {resume_url} - {merchant_name}",
            "Hi {customer_name}, the payment for {order_id} ({item_names}) at {attempt_time} was refused by the bank, and no money was taken. The {kept} is still saved at {amount} -- paying by {alt_method} tends to work: {resume_url} - {merchant_name}",
            "Hi {customer_name}, your {thing} {order_id} ({item_names}) is held at {amount}. The bank turned down the payment at {attempt_time} without giving a reason, and nothing was charged. {alt_method} is usually the quicker route: {resume_url} - {merchant_name}",
        ],
        "hi": [
            "नमस्ते {customer_name}, {attempt_time} पर आपके बैंक ने {thing} {order_id} ({item_names}) का पेमेंट अस्वीकार कर दिया और कोई राशि नहीं कटी। आपका {kept} {amount} पर सुरक्षित है। {alt_method} से आमतौर पर हो जाता है: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, {attempt_time} पर {order_id} ({item_names}) का पेमेंट बैंक ने मना कर दिया, कोई पैसा नहीं लिया गया। {kept} {amount} पर सुरक्षित है -- {alt_method} से भुगतान आमतौर पर चल जाता है: {resume_url} - {merchant_name}",
            "नमस्ते {customer_name}, आपका {thing} {order_id} ({item_names}) {amount} पर रखा है। {attempt_time} पर बैंक ने बिना कारण बताए पेमेंट अस्वीकार किया और कुछ भी चार्ज नहीं हुआ। {alt_method} आमतौर पर तेज़ रास्ता है: {resume_url} - {merchant_name}",
        ],
        "hinglish": [
            "Hi {customer_name}, {attempt_time} par aapke bank ne {thing} {order_id} ({item_names}) ka payment decline kar diya aur kuch charge nahi hua. Aapka {kept} {amount} par saved hai. {alt_method} se aam taur par ho jaata hai: {resume_url} - {merchant_name}",
            "Hi {customer_name}, {attempt_time} par {order_id} ({item_names}) ka payment bank ne refuse kar diya, koi paisa nahi liya gaya. {kept} {amount} par saved hai -- {alt_method} se payment aam taur par chal jaata hai: {resume_url} - {merchant_name}",
            "Hi {customer_name}, aapka {thing} {order_id} ({item_names}) {amount} par hai. {attempt_time} par bank ne bina wajah bataye payment decline kiya aur kuch charge nahi hua. {alt_method} aam taur par tez raasta hai: {resume_url} - {merchant_name}",
        ],
    },
    # R8 -- they closed the modal on purpose. One quiet nudge.
    "gentle_cart_reminder": {
        "en": [
            "Hi {customer_name}, you left {item_names} in your cart at {merchant_name}. {order_id} is saved at {amount} if you would like to come back to it: {resume_url}",
            "Hi {customer_name}, {item_names} is still in your cart at {merchant_name} under {order_id}, {amount} in total. It will be there if you want it: {resume_url}",
            "Hi {customer_name}, your cart at {merchant_name} still has {item_names} in it -- {order_id}, {amount}. No need to do anything unless you would like to finish: {resume_url}",
        ],
        "hi": [
            "नमस्ते {customer_name}, आपने {merchant_name} पर अपने कार्ट में {item_names} छोड़ दिया था। {order_id} {amount} पर सुरक्षित है, अगर आप लौटना चाहें: {resume_url}",
            "नमस्ते {customer_name}, {merchant_name} पर {order_id} के तहत आपके कार्ट में {item_names} अब भी है, कुल {amount}। जब चाहें, यह वहीं मिलेगा: {resume_url}",
            "नमस्ते {customer_name}, {merchant_name} पर आपके कार्ट में अब भी {item_names} है -- {order_id}, {amount}। कुछ करने की ज़रूरत नहीं, जब तक आप पूरा न करना चाहें: {resume_url}",
        ],
        "hinglish": [
            "Hi {customer_name}, aapne {merchant_name} par apne cart mein {item_names} chhod diya tha. {order_id} {amount} par saved hai, agar wapas aana chahein: {resume_url}",
            "Hi {customer_name}, {merchant_name} par {order_id} ke under aapke cart mein {item_names} abhi bhi hai, total {amount}. Jab chaahein, wahin milega: {resume_url}",
            "Hi {customer_name}, {merchant_name} par aapke cart mein abhi bhi {item_names} hai -- {order_id}, {amount}. Kuch karne ki zaroorat nahi, jab tak aap complete na karna chaahein: {resume_url}",
        ],
    },
}

# Which core text each rule uses. Two pairs of rules share an intent because
# they call for the same thing to be said: a bank decline is a bank decline
# whether it arrived as card_declined or as Razorpay's catch-all.
RULE_INTENT = {rule.rule_id: rule.message_intent for rule in RULES.values()}


def compose(intent: str, locale: str, vertical: str, variant_index: int) -> str:
    thing, kept = VERTICAL_WORDS[vertical][locale]
    text = CORE[intent][locale][variant_index]
    # Only these two are substituted now; every other brace is a real slot the
    # sender fills, so .format() is deliberately not used here.
    return text.replace("{thing}", thing).replace("{kept}", kept).replace(
        "{alt_method}", ALT_METHOD[locale]
    )


def build() -> list[dict]:
    generated_at = datetime.date.today().isoformat()
    entries, rejected = [], []

    for rule in RULES.values():
        intent = RULE_INTENT[rule.rule_id]
        if intent not in CORE:
            rejected.append(f"{rule.rule_id}: no core text for intent {intent!r}")
            continue
        for locale in LOCALES:
            for vertical in VERTICALS:
                for index, variant in enumerate(VARIANTS):
                    body = compose(intent, locale, vertical, index)
                    problems = validate_template(body, ALLOWED_PLACEHOLDERS)
                    if problems:
                        rejected.append(
                            f"{rule.rule_id}/{locale}/{vertical}/{variant}: {'; '.join(problems)}"
                        )
                        continue
                    entries.append(
                        {
                            "key": key(rule.rule_id, locale, vertical, variant),
                            "rule_id": rule.rule_id,
                            "locale": locale,
                            "vertical": vertical,
                            "variant": variant,
                            "message_intent": intent,
                            "body_template": body,
                            "prompt_version": PROMPT_VERSION,
                            "generated_at": generated_at,
                        }
                    )

    if rejected:
        print(f"{len(rejected)} cell(s) refused:", file=sys.stderr)
        for line in rejected[:20]:
            print(f"  {line}", file=sys.stderr)
        raise SystemExit(1)

    entries.sort(key=lambda e: (e["rule_id"], e["locale"], e["vertical"], e["variant"]))
    return entries


def payload(entries: list[dict]) -> dict:
    return {
        "_comment": (
            "Every message the sender can produce, written ahead of time and "
            "reviewed as a diff. The send path looks a row up and fills the "
            "slots -- it makes no model call and sends no customer data "
            "anywhere. Regenerate with: python -m scripts.build_copy_table"
        ),
        "prompt_version": PROMPT_VERSION,
        "entries": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed table is not what this script produces",
    )
    args = parser.parse_args()

    entries = build()
    rendered = json.dumps(payload(entries), indent=2, ensure_ascii=False) + "\n"

    if args.check:
        current = APPROVED.read_text(encoding="utf-8") if APPROVED.exists() else ""
        if current != rendered:
            print(
                "copy/approved.json is out of date -- run: python -m scripts.build_copy_table",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(f"copy/approved.json matches ({len(entries)} entries)")
        return

    APPROVED.parent.mkdir(parents=True, exist_ok=True)
    APPROVED.write_text(rendered, encoding="utf-8")

    by_locale: dict[str, int] = {}
    for entry in entries:
        by_locale[entry["locale"]] = by_locale.get(entry["locale"], 0) + 1
    print(f"wrote {APPROVED} with {len(entries)} entries")
    print("  rules   :", len({e['rule_id'] for e in entries}))
    print("  locales :", by_locale)
    print("  verticals:", len({e["vertical"] for e in entries}))


if __name__ == "__main__":
    main()
